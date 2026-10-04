"""Explicit opt-in, small real-provider comparison using synthetic local reads.

Without --run, reports only process credential availability and cost metadata.
Never prints keys, auth cookies, request headers, or raw provider responses.
The shared persistent budget ledger is the same one used by the local console.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from fastapi.testclient import TestClient
from agent.live import LiveConfiguration, LiveModels
from backend.app import Settings, create_app
from simulator.world import advance, initial_world


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "data/samples/live-read-defaults.json")
    parser.add_argument("--run", action="store_true", help="Dispatch a small billable comparison, with no retries")
    args = parser.parse_args()
    config = LiveConfiguration.read(args.config)
    settings = Settings()
    settings.budget_database.parent.mkdir(parents=True, exist_ok=True)
    models = LiveModels(config, settings.budget_database)
    status = models.public_status()
    print(json.dumps(status, ensure_ascii=False))
    if not all(item["available"] for item in status["providers"]):
        print("MODEL_KEY_UNAVAILABLE: 새 터미널/프로세스의 환경 변수 등록을 확인하세요. 호출하지 않았습니다.")
        return 2
    if not args.run:
        print("준비 상태만 확인했습니다. 실제 API 호출은 --run으로 별도 실행합니다.")
        return 0
    if status["budget"]["unknown_count"]:
        print("미확인 비용은 예약액으로 유지합니다. 새 요청에는 명시적으로 설정한 예산 한도만 적용합니다.")
    directory = ROOT / "Work_tree/artifacts/live-comparison" / uuid4().hex
    directory.mkdir(parents=True)
    app = create_app(Settings(database=directory / "foundation.sqlite3", test_control=True,
        origins=("http://testserver",), background_ticks=False))
    summaries = []
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime = app.state.runtime
        async def setup():
            runtime.world = initial_world(2)
            for _ in range(60):
                advance(runtime.world)
            runtime.world["run_status"] = "paused"
            runtime.store.commit(runtime.world, runtime.event(runtime.world))
            runtime.queries.live_models = models
            return runtime.world["run_id"]
        run_id = client.portal.call(setup)
        logged = client.post("/api/v1/auth/session", headers={"Origin": "http://testserver"},
            json={"username": "demo-owner", "password": "parking-demo-only"})
        if logged.status_code != 200:
            print("LOCAL_AUTH_FAILED")
            return 3
        csrf = client.get("/api/v1/me").json()["csrf_token"]
        for provider in config.providers:
            for goal in ("current_state", "regulation"):
                response = client.post("/api/v1/test/agent/live-queries",
                    json={"run_id": run_id, "goal": goal, "provider": provider,
                          "query": "통로를 막은 차량에는 어떻게 이동 요청하나요?" if goal == "regulation" else "현재 관측을 설명해주세요."},
                    headers={"Origin": "http://testserver", "X-CSRF-Token": csrf,
                             "Idempotency-Key": uuid4().hex})
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                summary = {"provider": provider, "goal": goal, "http_status": response.status_code,
                    **{name: body.get(name) for name in ("mode", "model_ref", "status", "reason_code", "model_calls", "tool_calls",
                        "elapsed_ms", "usage_status", "input_tokens", "output_tokens", "cost_estimated_krw", "cost_pending_krw")},
                    "tools": [item["name"] for item in body.get("tool_results", [])],
                    "error_code": body.get("error", {}).get("code")}
                summaries.append(summary)
                print(json.dumps(summary, ensure_ascii=False))
                record = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "results": summaries,
                    "budget": models.public_status()["budget"], "cost_basis": "buffered_token_estimate_not_invoice"}
                (directory / "summary.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if response.status_code != 200 or body.get("status") != "completed":
                    print("비교를 중단했습니다. 자동 재시도하지 않았으며, 완료와 보류를 구분해 기록했습니다.")
                    return 3
    print("실제 제공자별 현재 관측/규정 조회를 확인했습니다. 기록: " + str(directory / "summary.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
