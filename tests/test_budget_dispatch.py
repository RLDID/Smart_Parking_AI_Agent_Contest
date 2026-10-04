"""Dispatch admission serializes with an unknown result across DB connections."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import sqlite3

import pytest

from agent.budget import BudgetError, BudgetLedger
from contracts.budget import BudgetLimits, TokenPricing, TokenQuote


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
LIMITS = BudgetLimits(total_krw=100, daily_krw=100)
PRICING = TokenPricing(provider="demo-provider", model="model-1",
                       input_krw_per_million="1000", output_krw_per_million="1000")


def quote(key):
    return TokenQuote(request_key=key, input_tokens=1000, max_output_tokens=1000, pricing=PRICING)


def test_unknown_from_other_connection_keeps_reservation_without_blocking_new_dispatch(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first, second = BudgetLedger(path), BudgetLedger(path)
    first.reserve(quote("sent"), LIMITS, NOW)
    second.reserve(quote("waiting"), LIMITS, NOW)
    first.mark_dispatched("sent")
    first.mark_unknown("sent")
    assert second.mark_dispatched("waiting").state == "dispatched"
    assert second.snapshot(LIMITS, NOW).total_pending_krw == 4
    with pytest.raises(BudgetError):
        first.mark_dispatched("sent")
    # Reconciliation remains independent of the newer request.
    assert first.settle("sent", 1000, 1000, NOW).state == "settled"
    assert second.mark_dispatched("waiting").state == "dispatched"
    assert second.settle("waiting", 1000, 1000, NOW).state == "settled"


def test_unknown_committed_while_dispatch_waits_keeps_both_reservations(tmp_path):
    path = tmp_path / "interleaved.sqlite3"
    first, second = BudgetLedger(path), BudgetLedger(path)
    first.reserve(quote("sent"), LIMITS, NOW)
    second.reserve(quote("waiting"), LIMITS, NOW)
    first.mark_dispatched("sent")
    started = Event()

    def dispatch():
        started.set()
        assert second.mark_dispatched("waiting").state == "dispatched"

    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE budget_requests SET state='unknown' WHERE request_key='sent'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(dispatch)
            assert started.wait(2)
            assert not pending.done()
            writer.commit()
            pending.result(timeout=5)
    snap = second.snapshot(LIMITS, NOW)
    assert snap.unknown_count == 1 and snap.total_pending_krw == 4
