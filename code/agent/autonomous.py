"""Strict model output boundary for autonomous business decisions.

The mock is a deterministic baseline over public observations. It is never
reported as a paid model decision or as proof of a physical resolution.
"""
from contracts.autonomous import AutonomousDecision


class MockOperationsAdapter:
    mode = "mock"

    async def decide(self, context: dict) -> dict:
        scenario = context["scenario"]
        analysis = context.get("analysis") or {}
        incident = context.get("incident")
        target = context.get("target_ref")
        if scenario == "s1a":
            if incident:
                notice = context.get("notification") or {}
                if analysis.get("metrics", {}).get("clearance_sustained"):
                    action = "recheck"
                elif (context.get("recipient_check") or {}).get("mapping_status") == "unverified":
                    action = "notify"  # Server holds contact and records owner review.
                elif not notice and analysis.get("support_status") == "supported" and target:
                    action = "notify"
                elif notice.get("response") in ("cannot_move", "question"):
                    action = "report"
                elif notice.get("response_due") and not notice.get("response"):
                    max_contacts = (context.get("policy") or {}).get("execution_rules", {}).get("contact_max_sequence", 1)
                    action = "notify" if notice.get("contact_sequence", 0) < max_contacts and target else "report"
                elif notice and not notice.get("response") and not analysis.get("metrics", {}).get("clearance_sustained"):
                    action = "hold"
                else:
                    action = "recheck"
            elif analysis.get("support_status") == "supported" and target:
                action = "notify"
            else:
                action = "hold"
        elif scenario in ("s1b", "s1c"):
            if incident:
                notice = context.get("notification") or {}
                action = "recheck" if analysis.get("clearance_sustained") else "notify" if (
                    (context.get("recipient_check") or {}).get("mapping_status") == "unverified"
                    or not notice and analysis.get("violation_candidate") and target) else "report" if notice.get("response") in ("cannot_move", "question") or (
                    notice.get("response_due") and not notice.get("response")) else "recheck"
            elif analysis.get("support_status") == "supported" and target and analysis.get("violation_candidate"):
                action = "notify"
            else:
                action = "hold"
        elif scenario == "s2":
            action = "recheck" if incident else "report" if analysis.get("violation_candidate") and target else "hold"
        else:
            command = context.get("command") or {}
            goal = command.get("normalized_goal") or {}
            action = goal.get("action", "clarify")
            if action not in ("announce", "restrict_entry", "clarify"):
                action = "hold"
        return AutonomousDecision(scenario=scenario, action=action,
            target_ref=target if action == "notify" else None,
            reason_code="MOCK_OBSERVATION_REVIEW", rationale="현재 공개 관측과 서버 분석에 따른 모의 판단").model_dump()

    def result_metadata(self):
        return {"mode": "mock", "provider": None, "model_ref": "deterministic_mock",
                "model_call_count": 0, "cost_estimated_krw": 0}
