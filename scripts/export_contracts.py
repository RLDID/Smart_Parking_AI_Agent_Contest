"""Export public schemas/examples, without starting a DB or reading local data."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from backend.app import create_app  # noqa: E402
from contracts.models import Observation  # noqa: E402
from contracts.spatial import SpatialAnalysis  # noqa: E402
from contracts.exit_geometry import ExitCandidateAnalysis  # noqa: E402
from contracts.s1c_bay_geometry import BayGeometryAnalysis  # noqa: E402
from contracts.agent_loop import AgentQuery, LiveAgentQuery, ModelTurn  # noqa: E402
from contracts.knowledge import KnowledgeEvidence, KnowledgeResult, OperatingPolicy  # noqa: E402
from agent.tools import TOOL_INPUTS  # noqa: E402
from backend.business import INPUTS as BUSINESS_INPUTS  # noqa: E402
from contracts.business import ExecutionView  # noqa: E402
from contracts.relationships import (CustomerCreate, CustomerChange, VehicleCreate, VehicleChange,
    VehicleUserChange, ObjectMappingChange, PersonMappingChange)  # noqa: E402
from contracts.autonomous import AutonomousControl, AutonomousDecision, CommandClarification  # noqa: E402
from contracts.synthetic_users import SyntheticUserInput, SyntheticUserPolicy  # noqa: E402
from contracts.environment_controls import DeviceFaultInput, S2ReactionInput  # noqa: E402
from simulator.world import MAP, initial_world, public_state  # noqa: E402


def write_json(relative, value):
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


if __name__ == "__main__":
    write_json("code/contracts/openapi.json", create_app().openapi())
    write_json("code/contracts/observation.schema.json", Observation.model_json_schema())
    write_json("code/contracts/spatial-analysis.schema.json", SpatialAnalysis.model_json_schema())
    write_json("code/contracts/exit-candidate.schema.json", ExitCandidateAnalysis.model_json_schema())
    write_json("code/contracts/bay-footprint.schema.json", BayGeometryAnalysis.model_json_schema())
    write_json("code/contracts/agent-query-inputs.schema.json", {"query": AgentQuery.model_json_schema(), "live_query": LiveAgentQuery.model_json_schema(), "model_turn": ModelTurn.model_json_schema()})
    write_json("code/contracts/knowledge-result.schema.json", KnowledgeResult.model_json_schema())
    write_json("code/contracts/knowledge-evidence.schema.json", KnowledgeEvidence.model_json_schema())
    write_json("code/contracts/operating-policy.schema.json", OperatingPolicy.model_json_schema())
    write_json("code/contracts/read-tools.schema.json", {name: model.model_json_schema() for name, model in TOOL_INPUTS.items()})
    write_json("code/contracts/business-tools.schema.json", {name: model.model_json_schema() for name, model in BUSINESS_INPUTS.items()})
    write_json("code/contracts/execution.schema.json", ExecutionView.model_json_schema())
    write_json("code/contracts/relationships.schema.json", {model.__name__: model.model_json_schema()
        for model in (CustomerCreate, CustomerChange, VehicleCreate, VehicleChange,
                      VehicleUserChange, ObjectMappingChange, PersonMappingChange)})
    write_json("code/contracts/autonomous.schema.json", {model.__name__: model.model_json_schema()
        for model in (AutonomousControl, AutonomousDecision, CommandClarification)})
    write_json("code/contracts/sim0-controls.schema.json", {model.__name__: model.model_json_schema()
        for model in (SyntheticUserInput, SyntheticUserPolicy, DeviceFaultInput, S2ReactionInput)})
    write_json("data/samples/foundation-map.json", MAP)
    state = public_state(initial_world(1))
    snapshot = state["snapshot"]
    snapshot["run_id"], snapshot["observation_id"] = "run-example-001", "obs-example-001"
    snapshot["observed_at"] = snapshot["received_at"] = "2026-09-30T00:00:00Z"
    for device in snapshot["devices"]:
        device["observed_at"] = snapshot["observed_at"]
    write_json("data/samples/foundation-state.json", state)
    print("Exported OpenAPI, observation/spatial/knowledge JSON Schemas, internal tool inputs and synthetic map/state samples.")
