"""SQLite budget admission independent of model SDKs, credentials, and HTTP.

Reservation is intentionally conservative. A provider may still bill more than
the quote; settle records the actual charge even if that exceeds a cap.
Unknown dispatched calls hold their full reservation across restarts until an
authoritative usage result is settled. No elapsed timeout frees a reservation.
"""

from datetime import datetime, timedelta, timezone
from contextlib import closing
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from contracts.budget import (BudgetLimits, BudgetReservation, BudgetSnapshot,
                              TokenQuote)


# Competition-era Korea Standard Time is UTC+09:00. A fixed offset avoids
# depending on host tzdata, which is absent in the supported Windows runtime.
_KST = timezone(timedelta(hours=9), name="Asia/Seoul")
_MAX_ACTUAL_TOKENS = 20_000_000
_SCHEMA_VERSION = 3


class BudgetError(ValueError):
    """Admission or state transition denied without changing ledger state."""


def _day(now_utc):
    if (not isinstance(now_utc, datetime) or now_utc.tzinfo is None
            or now_utc.utcoffset() != timedelta(0)):
        raise ValueError("now_utc must be UTC-aware")
    return now_utc.astimezone(_KST).date().isoformat()


def _cost(input_tokens, output_tokens, quote):
    rate = quote.pricing
    value = ((Decimal(input_tokens) * Decimal(rate.input_krw_per_million)
              + Decimal(output_tokens) * Decimal(rate.output_krw_per_million))
             / Decimal(1_000_000))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _fingerprint(quote, limits):
    value = {"quote": quote.model_dump(mode="json"),
             "limits": limits.model_dump(mode="json")}
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def _reservation(row):
    return BudgetReservation(
        request_key=row["request_key"], provider=row["provider"],
        model=row["model"], day_kst=row["day_kst"], state=row["state"],
        reserved_krw=row["reserved_krw"], actual_krw=row["actual_krw"],
        actual_input_tokens=row["actual_input_tokens"],
        actual_output_tokens=row["actual_output_tokens"])


class BudgetLedger:
    """Each method opens and closes its own connection for cross-worker safety."""

    def __init__(self, path):
        db = Path(path)
        if not db.is_absolute() or str(db) == ":memory:" or not db.parent.is_dir():
            raise ValueError("Use a local absolute SQLite path with an existing parent")
        self.path = str(db)
        self._existed_at_open = db.exists()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0:
                if self._existed_at_open or tables:
                    raise BudgetError("Existing unversioned database cannot be adopted")
                db.execute("""CREATE TABLE budget_requests (
                    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    day_kst TEXT NOT NULL,
                    input_rate TEXT NOT NULL, output_rate TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK(state IN ('reserved','dispatched','unknown','settled','cancelled')),
                    reserved_krw INTEGER NOT NULL CHECK(reserved_krw >= 0),
                    actual_krw INTEGER CHECK(actual_krw >= 0),
                    actual_input_tokens INTEGER CHECK(actual_input_tokens >= 0),
                    actual_output_tokens INTEGER CHECK(actual_output_tokens >= 0)
                )""")
                db.execute("CREATE INDEX budget_requests_day ON budget_requests(day_kst)")
                db.execute("""CREATE TABLE budget_policy (
                    policy_id INTEGER PRIMARY KEY CHECK(policy_id=1),
                    total_krw INTEGER CHECK(total_krw > 0),
                    daily_krw INTEGER CHECK(daily_krw > 0)
                )""")
                db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            elif version not in (1, 2, _SCHEMA_VERSION) or tables != {"budget_requests", "budget_policy"}:
                raise BudgetError("Unsupported budget database schema")
            columns = {row[1] for row in db.execute("PRAGMA table_info(budget_requests)")}
            if columns != {"request_key", "fingerprint", "provider", "model", "day_kst",
                           "input_rate", "output_rate", "state", "reserved_krw", "actual_krw", "actual_input_tokens",
                           "actual_output_tokens"}:
                raise BudgetError("Budget database columns differ from expected schema")
            policy_columns = {row[1] for row in db.execute("PRAGMA table_info(budget_policy)")}
            if policy_columns != {"policy_id", "total_krw", "daily_krw"}:
                raise BudgetError("Budget policy columns differ from expected schema")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BudgetError("Budget database failed integrity check")
            if version in (1, 2):
                # Schema migration preserves both the pinned policy and every
                # request. Removing a cap requires a separate explicit update.
                db.execute("ALTER TABLE budget_policy RENAME TO budget_policy_previous")
                db.execute("""CREATE TABLE budget_policy (
                    policy_id INTEGER PRIMARY KEY CHECK(policy_id=1),
                    total_krw INTEGER CHECK(total_krw > 0),
                    daily_krw INTEGER CHECK(daily_krw > 0))""")
                db.execute("INSERT INTO budget_policy SELECT * FROM budget_policy_previous")
                db.execute("DROP TABLE budget_policy_previous")
                db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            db.commit()

    def _totals(self, db, day):
        row = db.execute("""SELECT
            COALESCE(SUM(CASE WHEN state='settled' THEN actual_krw ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN state IN ('reserved','dispatched','unknown')
                THEN reserved_krw ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN day_kst=? AND state='settled'
                THEN actual_krw ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN day_kst=? AND state IN
                ('reserved','dispatched','unknown') THEN reserved_krw ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0)
            FROM budget_requests""", (day, day)).fetchone()
        return tuple(row)

    def _check_policy(self, db, limits, *, pin):
        rows = db.execute("SELECT total_krw,daily_krw FROM budget_policy").fetchall()
        if len(rows) > 1:
            raise BudgetError("Ambiguous budget policy")
        if not rows:
            if db.execute("SELECT COUNT(*) FROM budget_requests").fetchone()[0]:
                raise BudgetError("Existing reservations have no pinned policy")
            if pin:
                db.execute("INSERT INTO budget_policy VALUES (1,?,?)",
                           (limits.total_krw, limits.daily_krw))
            return
        if (rows[0]["total_krw"], rows[0]["daily_krw"]) != (limits.total_krw, limits.daily_krw):
            raise BudgetError("Budget limits differ from the ledger's pinned policy")

    def reserve(self, quote, limits, now_utc):
        quote = TokenQuote.model_validate(quote)
        limits = BudgetLimits.model_validate(limits)
        day = _day(now_utc)
        fingerprint = _fingerprint(quote, limits)
        amount = _cost(quote.input_tokens, quote.max_output_tokens, quote)
        if amount <= 0:
            raise BudgetError("Cannot reserve a zero-cost quote")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policy(db, limits, pin=True)
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (quote.request_key,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise BudgetError("Request key reused with different quote or limits")
                db.commit()
                return _reservation(row)
            total_spent, total_pending, daily_spent, daily_pending, _ = self._totals(db, day)
            if ((limits.total_krw is not None and total_spent + total_pending + amount > limits.total_krw)
                    or (limits.daily_krw is not None
                        and daily_spent + daily_pending + amount > limits.daily_krw)):
                raise BudgetError("Budget limit would be exceeded")
            db.execute("""INSERT INTO budget_requests
                (request_key,fingerprint,provider,model,day_kst,input_rate,output_rate,state,reserved_krw)
                VALUES (?,?,?,?,?,?,?,'reserved',?)""",
                (quote.request_key, fingerprint, quote.pricing.provider,
                 quote.pricing.model, day, quote.pricing.input_krw_per_million,
                 quote.pricing.output_krw_per_million, amount))
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (quote.request_key,)).fetchone()
            db.commit()
            return _reservation(row)

    def _transition(self, request_key, allowed, target):
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("request_key required")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            if row is None:
                raise BudgetError("Unknown request key")
            if row["state"] == target:
                db.commit()
                return _reservation(row)
            if row["state"] not in allowed:
                raise BudgetError(f"Cannot move {row['state']} to {target}")
            db.execute("UPDATE budget_requests SET state=? WHERE request_key=?",
                       (target, request_key))
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            db.commit()
            return _reservation(row)

    def mark_dispatched(self, request_key):
        if not isinstance(request_key, str) or not request_key:
            raise ValueError("request_key required")
        with closing(self._connect()) as db, db:
            # Reserved quotes already count against the shared daily cap.
            # An unrelated unknown call keeps its reservation, not a global lock.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            if row is None:
                raise BudgetError("Unknown request key")
            if row["state"] == "dispatched":
                db.commit()
                return _reservation(row)
            if row["state"] != "reserved":
                raise BudgetError(f"Cannot move {row['state']} to dispatched")
            db.execute("UPDATE budget_requests SET state='dispatched' WHERE request_key=?", (request_key,))
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            db.commit()
            return _reservation(row)

    def mark_unknown(self, request_key):
        return self._transition(request_key, {"dispatched"}, "unknown")

    def recover_dispatched(self):
        """Single-runtime startup: interrupted sent requests need reconciliation."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("UPDATE budget_requests SET state='unknown' WHERE state='dispatched'").rowcount
            db.commit()
        return count

    def cancel_unstarted(self, request_key):
        return self._transition(request_key, {"reserved"}, "cancelled")

    def settle(self, request_key, actual_input_tokens, actual_output_tokens, now_utc):
        _day(now_utc)  # Validate caller clock; reservation day remains immutable.
        for value in (actual_input_tokens, actual_output_tokens):
            if type(value) is not int or not 0 <= value <= _MAX_ACTUAL_TOKENS:
                raise ValueError("Actual usage must be bounded nonnegative integer tokens")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            if row is None:
                raise BudgetError("Unknown request key")
            if row["state"] == "settled":
                if (row["actual_input_tokens"], row["actual_output_tokens"]) != (actual_input_tokens, actual_output_tokens):
                    raise BudgetError("Settled usage differs from existing result")
                db.commit()
                return _reservation(row)
            if row["state"] not in ("dispatched", "unknown"):
                raise BudgetError("Only dispatched calls may be settled")
            value = ((Decimal(actual_input_tokens) * Decimal(row["input_rate"])
                      + Decimal(actual_output_tokens) * Decimal(row["output_rate"]))
                     / Decimal(1_000_000))
            amount = int(value.to_integral_value(rounding=ROUND_CEILING))
            db.execute("""UPDATE budget_requests SET state='settled', actual_krw=?,
                       actual_input_tokens=?, actual_output_tokens=? WHERE request_key=?""",
                       (amount, actual_input_tokens, actual_output_tokens, request_key))
            row = db.execute("SELECT * FROM budget_requests WHERE request_key=?",
                             (request_key,)).fetchone()
            db.commit()
            return _reservation(row)

    def snapshot(self, limits, now_utc):
        limits = BudgetLimits.model_validate(limits)
        day = _day(now_utc)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policy(db, limits, pin=False)
            spent, pending, day_spent, day_pending, unknown = self._totals(db, day)
            db.commit()
        return BudgetSnapshot(day_kst=day, total_limit_krw=limits.total_krw,
                              daily_limit_krw=limits.daily_krw,
                              total_spent_krw=spent, total_pending_krw=pending,
                              daily_spent_krw=day_spent,
                              daily_pending_krw=day_pending,
                              unknown_count=unknown)

    def update_policy(self, expected, replacement):
        """Explicit offline policy change; never alters requests or usage."""
        expected = BudgetLimits.model_validate(expected)
        replacement = BudgetLimits.model_validate(replacement)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check_policy(db, expected, pin=True)
            if db.execute("SELECT 1 FROM budget_requests WHERE state IN ('reserved','dispatched') LIMIT 1").fetchone():
                raise BudgetError("Stop active calls before changing budget policy")
            db.execute("UPDATE budget_policy SET total_krw=?,daily_krw=? WHERE policy_id=1",
                       (replacement.total_krw, replacement.daily_krw))
            db.commit()
