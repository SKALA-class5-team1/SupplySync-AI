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


async def settle(service):
    while service._background:
        await asyncio.gather(*tuple(service._background))
    return await service.get(REQUEST.thread_id)


async def start(service):
    receipt = await service.start(REQUEST)
    assert receipt.status == "ACCEPTED"
    return await settle(service)


async def approve(service, job, result="APPROVED", reason=None):
    receipt = await service.resume(job.thread_id, decision(job, result, reason))
    assert receipt.status == "ACCEPTED"
    return await settle(service)


def test_all_stages_preserve_full_plan_and_save_once(workflow):
    service, env = workflow

    async def run():
        waiting = await start(service)
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
        # 계산·저장·등록이 단계의 시작과 종료 사이에 포함되고 모든 step이 BE enum을 사용한다.
        expected = [
            "RFQ_ANALYSIS", "SPEC_MATCH", "SUPPLY_CHECK", "ALTERNATIVE_ANALYSIS",
            "QUOTE", "FINAL_REVIEW", "APPROVAL", "APPROVAL", "REVALIDATION", "REGISTRATION",
        ]
        progress = [(e["type"], e["step"]) for e in env.events if e["type"] in {"STEP_START", "STEP_END"}]
        assert progress == [(kind, step) for step in expected for kind in ("STEP_START", "STEP_END")]
        register_index = next(i for i, (stage, name, _) in enumerate(env.calls) if stage == "SG4" and name == "register_quote")
        registration_end = next(i for i, (stage, name, args) in enumerate(env.calls)
                                if stage == "EVENT" and name == "STEP_END" and args["step"] == "REGISTRATION")
        assert register_index < registration_end
    asyncio.run(run())


def test_sg1_confirmation_preserves_original_and_records_user(workflow):
    service, env = workflow
    env.parsed["spec"]["flow"] = {"value": 22, "unit": "L/h"}

    async def run():
        first = await start(service)
        assert first.current_stage == "SG1" and first.waiting_for
        assert env.events[-1]["type"] == "CLARIFICATION_REQUIRED"
        assert env.events[-1]["step"] == "SPEC_MATCH"
        assert env.names("SG1").count("save_review") == 1
        await service.resume(first.thread_id, ClarificationResume(
            task_id=REQUEST.task_id, thread_id=REQUEST.thread_id, kind="CLARIFICATION", userId=2,
            decision={"assumptions": [{"field": "spec.flow", "value": "22 L/min"}]},
        ))
        next_job = await settle(service)
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
        first = await start(service)
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
        job = await start(service)
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
def test_technical_failure_after_acceptance_sends_run_failed(workflow, monkeypatch, stage):
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
        job = await start(service)
        assert job.state["error"]
        assert job.status == "FAILED" and job.waiting_for is None
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert "RUN_ON_HOLD" not in env.names("EVENT")
        assert env.names("SG4") == []
        if stage == "SG3":
            assert env.names("SG3").count("save_quote") == 1
        calls = len(env.calls)
        duplicate = await service.start(REQUEST)
        assert duplicate.status == "ACCEPTED" and len(env.calls) == calls
        with pytest.raises(HTTPException) as again:
            await service.resume(job.thread_id, decision())
        assert again.value.status_code == 409
    asyncio.run(run())


def test_final_reject_ends_without_rewrite_or_registration(workflow):
    service, env = workflow

    async def run():
        waiting = await start(service)
        result = await approve(service, waiting, "REJECTED", "계약 진행 중단")
        assert result.status == "REJECTED" and result.state["draft_revision"] == 1
        assert env.names("SG3").count("save_quote") == 1
        assert "check_parts_supply" not in env.names("SG4")
        assert env.events[-1] == {"type": "RUN_END", "data": {"outcome": "REJECTED"}}
    asyncio.run(run())


def test_revise_saves_new_revision_without_end_and_checks_optional_quote_id(workflow):
    service, env = workflow

    async def run():
        waiting = await start(service)
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
        waiting = await start(service)
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
        job = await start(service)
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
        waiting = await start(service)
        done = await approve(service, waiting)
        assert done.status == "FAILED" and done.state["sg4_outcome"] == "FAILED"
        assert "register_quote" not in env.names("SG4")
        assert env.events[-1]["type"] == "RUN_FAILED"
    asyncio.run(run())


def test_full_substitution_removes_original_from_final_plan(workflow):
    service, env = workflow
    env.gateway.chip.update(available=0, short_qty=2000)

    async def run():
        first = await start(service)
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
        job = await start(service)
        if stage == "SG4":
            job = await approve(service, job)
        assert job.status == "FAILED" and "확인 필요" in job.state["error"]
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
        job = await start(service)
        calls = len(env.calls)
        assert (await service.start(REQUEST)).status == "ACCEPTED"
        assert len(env.calls) == calls
        responses = await asyncio.gather(
            service.resume(job.thread_id, decision(job)), service.resume(job.thread_id, decision(job)),
            return_exceptions=True,
        )
        assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in responses) == 1
        assert env.names("SG3").count("save_quote") == 1
        await settle(service)
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
        first = await service.start(REQUEST)
        await started.wait()
        receipt = await asyncio.wait_for(service.start(REQUEST), 0.2)
        assert receipt.status == "ACCEPTED"
        release.set()
        assert first == receipt
        assert (await settle(service)).status == "WAITING"
        assert env.names("SG1").count("get_rfq_content") == 1
    asyncio.run(run())


@pytest.mark.parametrize("updates", [{"rfq_id": 8}, {"request_id": "different"}, {"thread_id": "different"}])
def test_conflicting_identifiers_rejected(workflow, updates):
    service, _ = workflow

    async def run():
        await start(service)
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
        job = await start(service)
        assert job.status == "FAILED" and "제한 시간" in job.state["error"]
        assert (await service.get(REQUEST.thread_id)).status == "FAILED"
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert env.names("SG3") == []
    asyncio.run(run())


def test_wait_event_failure_reports_run_failed_and_disables_resume(workflow, monkeypatch):
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
        job = await start(service)
        assert job.status == "FAILED" and "APPROVAL_REQUIRED" in job.state["error"]
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
            resumed.append(await asyncio.wait_for(service.resume(REQUEST.thread_id, decision()), 0.2))
    monkeypatch.setattr(events, "post_once", respond_immediately)

    async def run():
        await start(service)
        assert len(resumed) == 1
        assert resumed[0].status == "ACCEPTED"
        assert (await service.get(REQUEST.thread_id)).status == "DONE"
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
        assert (await start(service)).status == "FAILED"
        assert env.gateway.calls == []
        assert env.events[-1]["type"] == "RUN_FAILED"
    asyncio.run(run())


def test_hold_save_failure_is_technical_failure(workflow):
    from app.supply_analysis.mcp_tools import ToolError
    service, env = workflow
    env.gateway.substitutes["P-MCU-G1"]["substitutes"] = []
    env.gateway.failures["business_save_review"] = ToolError("보류 결과 저장 실패")

    async def run():
        job = await start(service)
        assert job.status == "FAILED" and "보류 결과 저장 실패" in job.state["error"]
        assert env.events[-1]["type"] == "RUN_FAILED"
        assert "RUN_ON_HOLD" not in env.names("EVENT")
    asyncio.run(run())


def test_shutdown_drains_accepted_work_and_rejects_new_commands(workflow, monkeypatch):
    from app.supervisor import events
    service, env = workflow
    original = events.post_once

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(task_id, event):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await original(task_id, event)
        monkeypatch.setattr(events, "post_once", blocked)
        receipt = await service.start(REQUEST)
        await entered.wait()
        closing = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST.model_copy(update={
                "thread_id": "another", "task_id": "another", "request_id": "another",
            }))
        assert exc.value.status_code == 503
        assert await service.start(REQUEST) == receipt
        assert not closing.done()
        release.set()
        await closing
        assert not service._background
        assert (await service.get(REQUEST.thread_id)).status == "WAITING"
        assert env.events[-1]["type"] == "APPROVAL_REQUIRED"
    asyncio.run(run())


def test_queued_resume_does_not_run_if_wait_event_delivery_fails(workflow, monkeypatch):
    import httpx
    from app.supervisor import events
    service, env = workflow
    original = events.post_once
    receipts = []

    async def delivered_but_response_lost(task_id, event):
        if event["type"] == "APPROVAL_REQUIRED":
            if not receipts:
                receipts.append(await service.resume(REQUEST.thread_id, decision()))
            raise httpx.ConnectError("이벤트 수신 응답 유실")
        await original(task_id, event)
    monkeypatch.setattr(events, "post_once", delivered_but_response_lost)

    async def run():
        job = await start(service)
        assert receipts[0].status == "ACCEPTED"
        assert job.status == "FAILED"
        assert "check_parts_supply" not in env.names("SG4")
        assert env.names("EVENT").count("RUN_FAILED") == 1
        assert not service._background
    asyncio.run(run())
