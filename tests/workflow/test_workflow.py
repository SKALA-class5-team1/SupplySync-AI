import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.supervisor.schemas import ApprovalResume, ClarificationResume, StartRunRequest

REQUEST = StartRunRequest(
    task_id="task-uuid", rfq_id=7, request_id="request-uuid", thread_id="task-uuid",
    rfq_number="HB-EV7-EWP-001", base_date="2026-10-19", issue_date="2026-10-19",
)


def decision(job=None, result="APPROVED", reason=None, **kwargs):
    return ApprovalResume(
        step="SG4.request_approval", userId=2,
        decision={"result": result, "reason": reason, "changes": {}}, **kwargs,
    )


async def approve(service, job, result="APPROVED", reason=None):
    return await service.resume(job.thread_id, decision(job, result, reason))


def test_all_stages_preserve_full_plan_and_save_once(workflow):
    service, env = workflow

    async def run():
        waiting = await service.start(REQUEST)
        assert waiting.status == "WAITING" and waiting.current_stage == "SG4"
        assert waiting.thread_id == REQUEST.thread_id and waiting.state["rfq_id"] == 7
        plan = waiting.state["final_plan"]
        assert len(plan) == 7 and plan["P-MCU-G1"] == 1500 and plan["P-MCU-G1-ALT"] == 500
        assert plan["P-CON-4P"] == 2000 and len(waiting.state["bom"]) == 6
        assert waiting.state["parsed_rfq"]["customer"] == "한별모터스"
        saved_reviews = [args for name, args in env.gateway.calls if name == "business_save_review"]
        assert saved_reviews[0]["final_plan"] == plan
        assert waiting.state["quote_id"] == "Q-1"
        substitute = waiting.state["quote_draft"]["conditions"][0]
        assert substitute["qty"] == 500 and substitute["extra_cost_krw"] == 150000
        assert waiting.state["quote_draft"]["unit_price_krw"] == 37300
        assert "register_quote" not in env.names("SG4")
        assert env.events[0] == {"type": "STEP_START", "step": "RFQ_ANALYSIS", "message": "RFQ 분석을 시작합니다"}
        steps = [e.get("step") for e in env.events if e["type"] == "STEP_START"]
        assert "SUPPLY_CHECK" in steps and "ALTERNATIVE_ANALYSIS" in steps
        assert env.events[-1]["type"] == "APPROVAL_REQUIRED"
        assert not any(e["type"] == "SAVE_APPROVAL_REQUIRED" for e in env.events)

        done = await approve(service, waiting)
        assert done.status == "DONE" and done.waiting_for is None
        assert done.state["registered_quote_id"] == "Q-1" and done.state["approver_id"] == 2
        assert done.state["bom"] == waiting.state["bom"] and done.state["final_plan"] == plan
        checked = next(args["parts"] for stage, name, args in env.calls if stage == "SG4" and name == "check_parts_supply")
        assert {p["part_id"]: p["required_qty"] for p in checked} == plan
        assert env.names("SG1").count("get_rfq_content") == 1
        assert env.names("SG1").count("save_review") == 1
        assert env.names("SG3").count("save_quote") == 1
        assert env.names("SG4").count("register_quote") == 1
        assert env.events[-1] == {"type": "RUN_END", "data": {"outcome": "REGISTERED"}}
        assert (await service.get(REQUEST.thread_id)).status == "DONE"
    asyncio.run(run())


def test_sg1_confirmation_preserves_original_and_records_user(workflow):
    service, env = workflow
    env.parsed["spec"]["flow"] = {"value": 22, "unit": "L/h"}

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG1" and first.waiting_for
        assert env.events[-1]["type"] == "CLARIFICATION_REQUIRED"
        assert env.names("SG1").count("save_review") == 1
        next_job = await service.resume(first.thread_id, ClarificationResume(
            task_id=REQUEST.task_id, thread_id=REQUEST.thread_id, kind="CLARIFICATION", userId=2,
            decision={"assumptions": [{"field": "spec.flow", "value": "22 L/min"}]},
        ))
        assert next_job.current_stage == "SG4"
        assert next_job.state["parsed_rfq"]["spec"]["flow"]["unit"] == "L/h"
        assumption = next(a for a in next_job.state["assumptions"] if a["field"] == "spec.flow")
        assert assumption["value"] == "22 L/min" and assumption["approved_by"] == 2
        assert next_job.state["confirmed_by"] == 2
        assert env.names("SG1").count("save_review") == 2
    asyncio.run(run())


def test_no_shortage_skips_alternative_event(workflow):
    service, env = workflow
    env.gateway.chip.update(available=2000, short_qty=0, status="OK")

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG4"
        assert first.state["final_plan"] == {p["part_id"]: 2000 for p in env.gateway.bom["parts"]}
        assert env.writer.calls == []
        assert not any(e.get("step") == "ALTERNATIVE_ANALYSIS" for e in env.events)
        assert (await approve(service, first)).status == "DONE"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["no_model", "no_supply"])
def test_business_hold_saves_empty_plan_before_event(workflow, failure):
    service, env = workflow
    if failure == "no_model":
        env.no_models = True
    else:
        env.gateway.substitutes["P-MCU-G1"]["substitutes"] = []

    async def run():
        job = await service.start(REQUEST)
        assert env.names("SG3") == [] and env.names("SG4") == []
        assert job.status == "ON_HOLD" and job.waiting_for is None
        assert job.state["final_plan"] == {}
        assert env.events[-1]["type"] == "RUN_ON_HOLD"
        if failure == "no_supply":
            assert job.state["saved"] is True
            assert env.gateway.calls[-1][1]["final_plan"] == {}
        assert "RUN_FAILED" not in env.names("EVENT")
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["SG1", "SG2", "SG3"])
def test_technical_failure_returns_error_and_run_failed(workflow, monkeypatch, stage):
    from app.rfq_review import mcp_tools
    from app.supply_analysis.mcp_tools import ToolError
    service, env = workflow

    async def fail(*args, **kwargs):
        raise ConnectionError("MCP 연결 실패")
    if stage == "SG1":
        monkeypatch.setattr(mcp_tools, "_call", fail)
    elif stage == "SG2":
        env.gateway.failures["business_get_bom"] = ToolError("MCP 연결 실패")
    else:
        env.fail_save = True

    async def run():
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST)
        assert exc.value.status_code == 502 and exc.value.detail
        job = await service.get(REQUEST.thread_id)
        assert job.status == "FAILED" and job.waiting_for is None
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert "RUN_ON_HOLD" not in env.names("EVENT")
        assert env.names("SG4") == []
        if stage == "SG3":
            assert env.names("SG3").count("save_quote") == 1
        calls = len(env.calls)
        duplicate = await service.start(REQUEST)
        assert duplicate.status == "FAILED" and len(env.calls) == calls
        with pytest.raises(HTTPException) as again:
            await service.resume(job.thread_id, decision())
        assert again.value.status_code == 409
    asyncio.run(run())


def test_final_reject_ends_without_rewrite_or_registration(workflow):
    service, env = workflow

    async def run():
        waiting = await service.start(REQUEST)
        result = await approve(service, waiting, "REJECTED", "계약 진행 중단")
        assert result.status == "REJECTED" and result.state["draft_revision"] == 1
        assert env.names("SG3").count("save_quote") == 1
        assert "check_parts_supply" not in env.names("SG4")
        assert env.events[-1] == {"type": "RUN_END", "data": {"outcome": "REJECTED"}}
    asyncio.run(run())


def test_revise_saves_new_revision_without_end_and_checks_optional_quote_id(workflow):
    service, env = workflow

    async def run():
        waiting = await service.start(REQUEST)
        revised = await approve(service, waiting, "REVISE", "대체 부품 문구 수정")
        assert revised.current_stage == "SG4" and revised.state["draft_revision"] == 2
        assert revised.state["final_plan"] == waiting.state["final_plan"]
        assert revised.state["quote_id"] == "Q-2"
        assert "RUN_END" not in env.names("EVENT")
        with pytest.raises(HTTPException) as exc:
            await service.resume(waiting.thread_id, decision(quoteId="Q-1"))
        assert exc.value.status_code == 422
        assert "register_quote" not in env.names("SG4")
        assert (await approve(service, revised)).status == "DONE"
        assert revised.state["quote_ids"] == ["Q-1", "Q-2"]
        assert "대체 부품 문구 수정" in env.writer.calls[-1][1].content
        assert [args["revision"] for s, name, args in env.calls if s == "SG3" and name == "save_quote"] == [1, 2]
    asyncio.run(run())


def test_stock_change_replans_all_parts_and_increments_revision(workflow):
    service, env = workflow

    async def run():
        waiting = await service.start(REQUEST)
        env.gateway.chip.update(available=1000, short_qty=1000)
        replanned = await approve(service, waiting)
        assert replanned.current_stage == "SG4"
        assert replanned.state["draft_revision"] == 2
        assert replanned.state["final_plan"]["P-MCU-G1"] == 1000
        assert replanned.state["final_plan"]["P-MCU-G1-ALT"] == 1000
        assert len(replanned.state["final_plan"]) == 7
        assert "register_quote" not in env.names("SG4")
        assert "RUN_END" not in env.names("EVENT")
        assert env.names("SG1").count("get_rfq_content") == 1
        assert replanned.state["quote_draft"]["conditions"][0]["extra_cost_krw"] == 300000
        assert [a["revision"] for s, n, a in env.calls if s == "SG3" and n == "save_quote"] == [1, 2]
        assert (await approve(service, replanned)).status == "DONE"
    asyncio.run(run())


def test_rewrite_limit_allows_three_revisions(workflow):
    service, env = workflow

    async def run():
        job = await service.start(REQUEST)
        for revision in range(1, 5):
            assert job.state["draft_revision"] == revision
            job = await approve(service, job, "REVISE", "문구 수정")
        assert job.status == "ON_HOLD" and job.waiting_for is None
        assert "한도" in job.state["error"]
        assert env.names("SG3").count("save_quote") == 4
        assert env.events[-1]["type"] == "RUN_ON_HOLD"
        assert "RUN_END" not in env.names("EVENT")
    asyncio.run(run())


@pytest.mark.parametrize("malformation", ["missing", "quantity", "duplicate", "status", "shortage", "date"])
def test_sg4_invalid_supply_cannot_register(workflow, malformation):
    service, env = workflow

    def transform(supply):
        supply = deepcopy(supply)
        if malformation == "missing":
            supply["parts"].pop()
        elif malformation == "duplicate":
            supply["parts"][-1] = supply["parts"][0]
        elif malformation == "quantity":
            supply["parts"][0]["required_qty"] -= 1
        elif malformation == "status":
            supply["parts"][0]["status"] = "UNKNOWN"
        elif malformation == "date":
            supply["due_date"] = "2020-01-01"
        else:
            supply["parts"][0]["available"] = 0
        return supply
    env.supply_transform = transform

    async def run():
        waiting = await service.start(REQUEST)
        with pytest.raises(HTTPException) as exc:
            await approve(service, waiting)
        assert exc.value.status_code == 502
        done = await service.get(waiting.thread_id)
        assert done.status == "FAILED" and done.state["sg4_outcome"] == "FAILED"
        assert "register_quote" not in env.names("SG4")
        assert env.events[-1]["type"] == "RUN_FAILED"
    asyncio.run(run())


def test_full_substitution_removes_original_from_final_plan(workflow):
    service, env = workflow
    env.gateway.chip.update(available=0, short_qty=2000)

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG4"
        assert "P-MCU-G1" not in first.state["final_plan"]
        assert first.state["final_plan"]["P-MCU-G1-ALT"] == 2000
        assert len(first.state["final_plan"]) == len(first.state["bom"]) == 6
        assert (await approve(service, first)).status == "DONE"
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["SG3", "SG4"])
def test_unexpected_write_status_is_not_success(workflow, stage):
    service, env = workflow
    if stage == "SG3":
        env.save_status = "UNKNOWN"
    else:
        env.register_status = "UNKNOWN"

    async def run():
        with pytest.raises(HTTPException) as exc:
            job = await service.start(REQUEST)
            await approve(service, job)
        assert exc.value.status_code == 502 and "확인 필요" in exc.value.detail
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert "RUN_END" not in env.names("EVENT")
        if stage == "SG3":
            assert env.names("SG4") == []
        else:
            assert env.names("SG4").count("register_quote") == 1
    asyncio.run(run())


def test_duplicate_start_and_concurrent_resume_do_not_repeat_side_effects(workflow):
    service, env = workflow

    async def run():
        job = await service.start(REQUEST)
        calls = len(env.calls)
        assert await service.start(REQUEST) == job
        assert len(env.calls) == calls
        responses = await asyncio.gather(
            service.resume(job.thread_id, decision(job)), service.resume(job.thread_id, decision(job)),
            return_exceptions=True,
        )
        assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in responses) == 1
        assert env.names("SG3").count("save_quote") == 1
        assert env.names("SG4").count("register_quote") == 1
    asyncio.run(run())


def test_duplicate_during_initial_run_returns_acceptance_immediately(workflow, monkeypatch):
    from app.supervisor import events
    service, env = workflow
    original = events.post_once

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def pause(task_id, event):
            if not started.is_set():
                started.set()
                await release.wait()
            await original(task_id, event)
        monkeypatch.setattr(events, "post_once", pause)
        first = asyncio.create_task(service.start(REQUEST))
        await started.wait()
        receipt = await asyncio.wait_for(service.start(REQUEST), 0.2)
        assert receipt.status == "ACCEPTED"
        release.set()
        assert (await first).status == "WAITING"
        assert env.names("SG1").count("get_rfq_content") == 1
    asyncio.run(run())


@pytest.mark.parametrize("updates", [{"rfq_id": 8}, {"request_id": "different"}, {"thread_id": "different"}])
def test_conflicting_identifiers_rejected(workflow, updates):
    service, _ = workflow

    async def run():
        await service.start(REQUEST)
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST.model_copy(update=updates))
        assert exc.value.status_code == 409
    asyncio.run(run())


def test_timeout_cancels_execution_and_reports_failure(workflow, monkeypatch):
    from app.core.config import settings
    from app.rfq_review import mcp_tools
    service, env = workflow

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("취소되지 않았습니다")
    monkeypatch.setattr(mcp_tools, "_call", slow)
    monkeypatch.setattr(settings, "RUN_TIMEOUT_SEC", 0.03)

    async def run():
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST)
        assert exc.value.status_code == 504 and "제한 시간" in exc.value.detail
        assert (await service.get(REQUEST.thread_id)).status == "FAILED"
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert env.names("SG3") == []
    asyncio.run(run())


def test_wait_event_failure_returns_http_error_and_disables_resume(workflow, monkeypatch):
    import httpx
    from app.supervisor import events
    service, env = workflow
    original = events.post_once
    attempts = []

    async def fail_wait(task_id, event):
        if event["type"] == "APPROVAL_REQUIRED":
            attempts.append(event)
            raise httpx.ConnectError("연결 끊김")
        await original(task_id, event)
    monkeypatch.setattr(events, "post_once", fail_wait)

    async def run():
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST)
        assert exc.value.status_code == 502 and "APPROVAL_REQUIRED" in exc.value.detail
        assert len(attempts) == 3
        job = await service.get(REQUEST.thread_id)
        assert job.status == "FAILED" and job.waiting_for is None
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert env.names("SG3").count("save_quote") == 1
    asyncio.run(run())


def test_immediate_resume_from_approval_event_finds_saved_checkpoint(workflow, monkeypatch):
    from app.supervisor import events
    service, env = workflow
    original = events.post_once
    resumed = []

    async def respond_immediately(task_id, event):
        await original(task_id, event)
        if event["type"] == "APPROVAL_REQUIRED":
            resumed.append(asyncio.create_task(service.resume(REQUEST.thread_id, decision())))
            await asyncio.sleep(0)
    monkeypatch.setattr(events, "post_once", respond_immediately)

    async def run():
        await service.start(REQUEST)
        assert len(resumed) == 1
        assert (await resumed[0]).status == "DONE"
        assert env.names("SG4").count("register_quote") == 1
    asyncio.run(run())


def test_progress_event_failure_stops_before_business_action(workflow, monkeypatch):
    import httpx
    from app.supervisor import events
    service, env = workflow
    original = events.post_once

    async def fail_supply(task_id, event):
        if event.get("step") == "SUPPLY_CHECK":
            response = httpx.Response(401, request=httpx.Request("POST", "http://be/internal/tasks/id/events"))
            response.raise_for_status()
        await original(task_id, event)
    monkeypatch.setattr(events, "post_once", fail_supply)

    async def run():
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST)
        assert exc.value.status_code == 502
        assert env.gateway.calls == []
        assert env.events[-1]["type"] == "RUN_FAILED"
    asyncio.run(run())


def test_hold_save_failure_is_technical_failure(workflow):
    from app.supply_analysis.mcp_tools import ToolError
    service, env = workflow
    env.gateway.substitutes["P-MCU-G1"]["substitutes"] = []
    env.gateway.failures["business_save_review"] = ToolError("보류 결과 저장 실패")

    async def run():
        with pytest.raises(HTTPException, match="보류 결과 저장 실패"):
            await service.start(REQUEST)
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert "RUN_ON_HOLD" not in env.names("EVENT")
    asyncio.run(run())
