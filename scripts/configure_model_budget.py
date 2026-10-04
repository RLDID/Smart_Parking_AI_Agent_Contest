"""Explicit offline policy update preserving the existing shared usage ledger."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from agent.budget import BudgetLedger
from agent.live import LiveConfiguration
from contracts.budget import BudgetLimits


def optional_amount(value):
    return None if value == "none" else int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=ROOT / "data/local/model-budget.sqlite3")
    parser.add_argument("--expected-total-krw", type=optional_amount, required=True)
    parser.add_argument("--expected-daily-krw", type=optional_amount, required=True)
    args = parser.parse_args()
    config = LiveConfiguration.read(args.config)
    if not args.ledger.is_file():
        parser.error("Existing ledger required; this command never creates a replacement")
    expected = BudgetLimits(total_krw=args.expected_total_krw, daily_krw=args.expected_daily_krw)
    ledger = BudgetLedger(args.ledger.resolve())
    before = ledger.snapshot(expected, datetime.now(timezone.utc))
    ledger.update_policy(expected, config.limits)
    after = ledger.snapshot(config.limits, datetime.now(timezone.utc))
    print(json.dumps({"before": before.model_dump(), "after": after.model_dump(),
                      "provider_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
