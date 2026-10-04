"""Export standalone core schemas without importing the backend or starting it."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from contracts.approach import ApproachAnalysis  # noqa: E402
from contracts.budget import (BudgetLimits, BudgetReservation, BudgetSnapshot,
                              TokenPricing, TokenQuote)  # noqa: E402
from contracts.devices import DeviceCommand, DeviceState  # noqa: E402
from contracts.parking_assessment import ParkingAssessment  # noqa: E402


def export():
    schemas = {
        "parking-assessment": ParkingAssessment.model_json_schema(),
        "approach-analysis": ApproachAnalysis.model_json_schema(),
        "device-state": DeviceState.model_json_schema(),
        "device-command": DeviceCommand.model_json_schema(),
        "budget": {model.__name__: model.model_json_schema() for model in
                   (BudgetLimits, TokenPricing, TokenQuote, BudgetReservation, BudgetSnapshot)},
    }
    for name, value in schemas.items():
        (ROOT / "code" / "contracts" / f"{name}.schema.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    print(f"Exported {len(schemas)} independent core schema files.")


if __name__ == "__main__":
    export()
