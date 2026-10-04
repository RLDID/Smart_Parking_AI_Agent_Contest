"""Exercise the shared internal tools using an isolated synthetic probe database."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from backend.auth import Auth  # noqa: E402
from backend.runtime import Runtime  # noqa: E402
from contracts.knowledge import KnowledgeEvidence  # noqa: E402
from simulator.world import FACILITY  # noqa: E402


async def check():
    # Unique ignored path; never opens or modifies the running preview DB.
    path = ROOT / "data/local/probes" / ("knowledge-" + uuid4().hex + ".sqlite3")
    runtime = Runtime(path)
    try:
        auth = Auth(runtime.store)
        _, session = auth.login("demo-operator", "parking-demo-only", "internal-probe")
        run = await runtime.mutate(session, "probe-run", "create", {"seed": 1})
        task = runtime.read_task(session, run["run_id"])
        policy = await runtime.read_tool(session, "get_operating_policy", {"facility_id": FACILITY}, task)
        result = await runtime.read_tool(session, "search_operating_knowledge", {
            "facility_id": FACILITY, "run_id": run["run_id"], "query": "통로 차단 이동 요청과 미응답",
            "topic": "parking_order", "zone_id": "aisle-west"}, task)
        evidence = KnowledgeEvidence(retrieval_id=result["retrieval_id"],
                                     reference_ids=[r["reference_id"] for r in result["references"]])
        validated = await runtime.validate_knowledge(session, run["run_id"], evidence,
                                                    tool_name="notify_vehicle_user", purpose="move_request")
        print(json.dumps({"status": result["status"], "policy_version": policy["policy_version"],
                          "reference_ids": evidence.reference_ids, "validation": validated,
                          "execution": "not_executed_by_probe", "database": str(path.relative_to(ROOT))}, ensure_ascii=False))
    finally:
        runtime.store.close()


if __name__ == "__main__":
    asyncio.run(check())
