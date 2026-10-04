"""No SDK, API key, host-clock, or network dependency in budget admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from agent.budget import BudgetError, BudgetLedger
from contracts.budget import BudgetLimits, TokenPricing, TokenQuote


UTC = datetime(2026, 10, 1, 14, 59, tzinfo=timezone.utc)
PRICING = TokenPricing(provider="demo-provider", model="model-1",
                       input_krw_per_million="1000", output_krw_per_million="1000")
LIMITS = BudgetLimits(total_krw=3000, daily_krw=2000)


def quote(key, **changes):
    data = dict(request_key=key, input_tokens=500_000,
                max_output_tokens=500_000, pricing=PRICING)
    data.update(changes)
    return TokenQuote(**data)


def ledger(tmp_path):
    return BudgetLedger(tmp_path / "budget.sqlite3")


def test_atomic_reservations_across_distinct_connections(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    BudgetLedger(path)

    def attempt(i):
        try:
            return BudgetLedger(path).reserve(quote(f"req-{i}"), LIMITS, UTC)
        except BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(10)))
    assert sum(item is not None for item in outcomes) == 2
    snap = BudgetLedger(path).snapshot(LIMITS, UTC)
    assert snap.total_pending_krw == snap.daily_pending_krw == 2000


def test_idempotent_identical_quote_and_changed_collision(tmp_path):
    budget = ledger(tmp_path)
    first = budget.reserve(quote("same"), LIMITS, UTC)
    assert first.reserved_krw == 1000
    assert budget.reserve(quote("same"), LIMITS, UTC+timedelta(days=1)) == first
    with pytest.raises(BudgetError):
        budget.reserve(quote("same", max_output_tokens=600_000), LIMITS, UTC)
    with pytest.raises(BudgetError):
        budget.reserve(quote("same"), BudgetLimits(total_krw=4000, daily_krw=2000), UTC)
    with pytest.raises(BudgetError):
        budget.reserve(quote("new-key"), BudgetLimits(total_krw=4000, daily_krw=2000), UTC)
    assert budget.snapshot(LIMITS, UTC).total_pending_krw == 1000


def test_policy_persists_and_changed_limits_fail_across_instances(tmp_path):
    path = tmp_path / "pinned.sqlite3"
    BudgetLedger(path).reserve(quote("initial"), LIMITS, UTC)
    reopened = BudgetLedger(path)
    changed = BudgetLimits(total_krw=10_000, daily_krw=10_000)
    with pytest.raises(BudgetError):
        reopened.reserve(quote("later"), changed, UTC)
    with pytest.raises(BudgetError):
        reopened.snapshot(changed, UTC)
    assert reopened.reserve(quote("later"), LIMITS, UTC).reserved_krw == 1000


def test_initial_policy_race_accepts_one_limit_only(tmp_path):
    path = tmp_path / "racing-policy.sqlite3"
    BudgetLedger(path)
    low = BudgetLimits(total_krw=2000, daily_krw=2000)
    high = BudgetLimits(total_krw=10_000, daily_krw=10_000)

    def attempt(args):
        key, caps = args
        try:
            return BudgetLedger(path).reserve(quote(key), caps, UTC)
        except BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, [("low", low), ("high", high)]))
    assert sum(item is not None for item in result) == 1
    accepted = low if result[0] is not None else high
    rejected = high if result[0] is not None else low
    assert BudgetLedger(path).snapshot(accepted, UTC).total_pending_krw == 1000
    with pytest.raises(BudgetError):
        BudgetLedger(path).snapshot(rejected, UTC)


def test_kst_day_rollover_retains_total_and_original_day_charge(tmp_path):
    budget = ledger(tmp_path)
    first = budget.reserve(quote("before-midnight"), LIMITS, UTC)
    assert first.day_kst == "2026-10-01"
    after = UTC+timedelta(minutes=2)
    assert budget.snapshot(LIMITS, after).day_kst == "2026-10-02"
    second = budget.reserve(quote("after-midnight"), LIMITS, after)
    assert second.day_kst == "2026-10-02"
    budget.mark_dispatched("before-midnight")
    budget.settle("before-midnight", 500_000, 500_000, after)
    current = budget.snapshot(LIMITS, after)
    assert (current.daily_spent_krw, current.daily_pending_krw) == (0, 1000)
    assert (current.total_spent_krw, current.total_pending_krw) == (1000, 1000)
    previous = budget.snapshot(LIMITS, UTC)
    assert (previous.daily_spent_krw, previous.daily_pending_krw) == (1000, 0)


def test_unknown_remains_reserved_on_restart_and_cannot_cancel(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    budget = BudgetLedger(path)
    budget.reserve(quote("lost-reply"), LIMITS, UTC)
    budget.mark_dispatched("lost-reply")
    assert budget.mark_unknown("lost-reply").state == "unknown"
    reopened = BudgetLedger(path)
    assert reopened.snapshot(LIMITS, UTC).unknown_count == 1
    assert reopened.snapshot(LIMITS, UTC).total_pending_krw == 1000
    with pytest.raises(BudgetError):
        reopened.cancel_unstarted("lost-reply")
    with pytest.raises(BudgetError):
        reopened.mark_dispatched("lost-reply")
    settled = reopened.settle("lost-reply", 400_000, 200_000, UTC)
    assert settled.actual_krw == 600
    assert reopened.snapshot(LIMITS, UTC).total_pending_krw == 0


def test_actual_overage_charged_and_blocks_next_admission(tmp_path):
    budget = ledger(tmp_path)
    budget.reserve(quote("overage"), BudgetLimits(total_krw=1000, daily_krw=1000), UTC)
    budget.mark_dispatched("overage")
    settled = budget.settle("overage", 1_000_000, 1_000_000, UTC)
    assert settled.actual_krw == 2000
    snap = budget.snapshot(BudgetLimits(total_krw=1000, daily_krw=1000), UTC)
    assert snap.total_spent_krw == 2000
    with pytest.raises(BudgetError):
        budget.reserve(quote("next"), BudgetLimits(total_krw=1000, daily_krw=1000), UTC)
    assert budget.settle("overage", 1_000_000, 1_000_000, UTC) == settled
    with pytest.raises(BudgetError):
        budget.settle("overage", 1_000_000, 999_999, UTC)


def test_cancel_only_unstarted_releases_budget(tmp_path):
    budget = ledger(tmp_path)
    budget.reserve(quote("never-sent"), LIMITS, UTC)
    assert budget.cancel_unstarted("never-sent").state == "cancelled"
    assert budget.snapshot(LIMITS, UTC).total_pending_krw == 0
    with pytest.raises(BudgetError):
        budget.mark_dispatched("never-sent")
    budget.reserve(quote("sent"), LIMITS, UTC)
    budget.mark_dispatched("sent")
    with pytest.raises(BudgetError):
        budget.cancel_unstarted("sent")


def test_decimal_roundup_and_reasoning_output_is_in_reserved_output(tmp_path):
    budget = ledger(tmp_path)
    pricing = TokenPricing(provider="p", model="m",
                           input_krw_per_million="0.000001",
                           output_krw_per_million="0.000001")
    assert budget.reserve(quote("tiny", input_tokens=1,
                                max_output_tokens=1, pricing=pricing), LIMITS, UTC).reserved_krw == 1
    budget.mark_dispatched("tiny")
    assert budget.settle("tiny", 1, 1, UTC).actual_krw == 1
    with pytest.raises(BudgetError):
        budget.reserve(quote("free", pricing=TokenPricing(
            provider="p", model="m", input_krw_per_million="0",
            output_krw_per_million="0")), LIMITS, UTC)


def test_invalid_inputs_and_unknown_database_fail_closed(tmp_path):
    budget = ledger(tmp_path)
    with pytest.raises(ValueError):
        budget.reserve(quote("naive"), LIMITS, datetime(2026, 10, 1))
    with pytest.raises(Exception):
        quote("float", input_tokens=1.5)
    with pytest.raises(Exception):
        TokenPricing(provider="p", model="m", input_krw_per_million="NaN",
                     output_krw_per_million="1")
    budget.reserve(quote("pending"), LIMITS, UTC)
    with pytest.raises(BudgetError):
        budget.settle("pending", 1, 1, UTC)
    with pytest.raises(ValueError):
        budget.settle("pending", True, 1, UTC)
    foreign = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE foreign_data(x TEXT)")
        db.execute("INSERT INTO foreign_data VALUES ('preserve')")
    with pytest.raises(BudgetError):
        BudgetLedger(foreign)
    with sqlite3.connect(foreign) as db:
        assert db.execute("SELECT x FROM foreign_data").fetchone()[0] == "preserve"
    old_schema = tmp_path / "old-schema.sqlite3"
    with sqlite3.connect(old_schema) as db:
        db.execute("CREATE TABLE budget_requests(request_key TEXT)")
        db.execute("PRAGMA user_version=1")
    with pytest.raises(BudgetError):
        BudgetLedger(old_schema)
    (tmp_path / "bad.sqlite3").write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        BudgetLedger(tmp_path / "bad.sqlite3")


def test_daily_only_budget_keeps_unknown_charge_and_resets_at_kst_midnight(tmp_path):
    budget = ledger(tmp_path)
    limits = BudgetLimits(total_krw=None, daily_krw=1000)
    half = dict(input_tokens=250_000, max_output_tokens=250_000)
    budget.reserve(quote("lost", **half), limits, UTC)
    budget.mark_dispatched("lost")
    budget.mark_unknown("lost")
    budget.reserve(quote("new", **half), limits, UTC)
    budget.mark_dispatched("new")
    budget.settle("new", 250_000, 250_000, UTC)
    with pytest.raises(BudgetError):
        budget.reserve(quote("over", **half), limits, UTC)
    tomorrow = UTC + timedelta(minutes=2)
    budget.reserve(quote("tomorrow"), limits, tomorrow)
    snap = budget.snapshot(limits, tomorrow)
    assert snap.total_limit_krw is None and snap.total_spent_krw == 500
    assert snap.total_pending_krw == 1500 and snap.daily_pending_krw == 1000
    assert snap.unknown_count == 1
    assert budget.snapshot(limits, UTC).daily_pending_krw == 500


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_and_explicit_policy_change_preserve_all_usage(tmp_path, version):
    path = tmp_path / "legacy.sqlite3"
    budget = BudgetLedger(path)
    budget.reserve(quote("lost"), LIMITS, UTC)
    budget.mark_dispatched("lost")
    budget.mark_unknown("lost")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE budget_policy RENAME TO old_policy")
        db.execute("""CREATE TABLE budget_policy (
            policy_id INTEGER PRIMARY KEY CHECK(policy_id=1),
            total_krw INTEGER NOT NULL CHECK(total_krw > 0),
            daily_krw INTEGER NOT NULL CHECK(daily_krw > 0))""")
        db.execute("INSERT INTO budget_policy SELECT * FROM old_policy")
        db.execute("DROP TABLE old_policy")
        db.execute(f"PRAGMA user_version={version}")
        before = db.execute("SELECT * FROM budget_requests").fetchall()
    reopened = BudgetLedger(path)
    assert reopened.snapshot(LIMITS, UTC).unknown_count == 1
    daily = BudgetLimits(total_krw=None, daily_krw=1000)
    with pytest.raises(BudgetError):
        reopened.snapshot(daily, UTC)
    with pytest.raises(BudgetError):
        reopened.update_policy(daily, daily)
    reopened.update_policy(LIMITS, daily)
    assert reopened.snapshot(daily, UTC).total_pending_krw == 1000
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM budget_requests").fetchall() == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    with pytest.raises(BudgetError):
        reopened.reserve(quote("over"), daily, UTC)


def test_policy_update_rejects_active_calls(tmp_path):
    budget = ledger(tmp_path)
    budget.reserve(quote("active"), LIMITS, UTC)
    with pytest.raises(BudgetError, match="Stop active"):
        budget.update_policy(LIMITS, BudgetLimits(total_krw=None, daily_krw=1000))
    assert budget.snapshot(LIMITS, UTC).total_pending_krw == 1000


def test_uncapped_policy_preserves_unknown_and_tracks_spend_over_old_daily_limit(tmp_path):
    budget = ledger(tmp_path)
    budget.reserve(quote("lost"), LIMITS, UTC)
    budget.mark_dispatched("lost")
    budget.mark_unknown("lost")
    uncapped = BudgetLimits()
    budget.update_policy(LIMITS, uncapped)
    for key in ("new-1", "new-2", "new-3"):
        budget.reserve(quote(key), uncapped, UTC)
        budget.mark_dispatched(key)
        budget.settle(key, 500_000, 500_000, UTC)
    reopened = BudgetLedger(budget.path)
    snap = reopened.snapshot(uncapped, UTC)
    assert snap.total_limit_krw is None and snap.daily_limit_krw is None
    assert snap.daily_spent_krw == 3000 and snap.daily_pending_krw == 1000
    assert snap.unknown_count == 1
    with pytest.raises(BudgetError):
        reopened.mark_dispatched("lost")


def test_total_only_policy_still_enforces_selected_limit(tmp_path):
    budget = ledger(tmp_path)
    limits = BudgetLimits(total_krw=1000)
    budget.reserve(quote("first"), limits, UTC)
    with pytest.raises(BudgetError):
        budget.reserve(quote("next-day"), limits, UTC + timedelta(days=1))
