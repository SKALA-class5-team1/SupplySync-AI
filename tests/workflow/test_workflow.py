import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.supervisor.schemas import ResumeJobRequest, StartJobRequest


REQUEST = StartJobRequest(thread_id="workflow-test", rfq_number="HB-EV7-EWP-001", base_date="2026-10-19", issue_date="2026-10-19")


def decision(job, result="APPROVED", reason=None):
    return ResumeJobRequest(interrupt_id=job.waiting_for.id, response={
        "step": job.waiting_for.payload["step"], "userId": 2,
        "decision": {"result": result, "reason": reason},
    })


async def approve(service, job, result="APPROVED", reason=None):
    return await service.resume(job.thread_id, decision(job, result, reason))


def test_all_stages_preserve_full_plan_and_save_once(workflow):
    service, env = workflow

    async def run():
        first = await service.start(REQUEST)
        assert first.status == "WAITING" and first.current_stage == "SG3", first.state.get("error")
        plan = first.state["final_plan"]
        assert len(plan) == 7 and plan["P-MCU-G1"] == 1500 and plan["P-MCU-G1-ALT"] == 500
        assert plan["P-CON-4P"] == 2000
        assert len(first.state["bom"]) == 6
        assert first.state["parsed_rfq"]["customer"] == "한별모터스"
        assert "save_quote" not in env.names("SG3")
        saved_reviews = [args for name, args in env.gateway.calls if name == "business_save_review"]
        assert saved_reviews[0]["final_plan"] == plan

        waiting = await approve(service, first)
        assert waiting.current_stage == "SG4" and waiting.state["quote_id"] == "Q-1"
        assert waiting.state["final_plan"] == plan
        substitute = waiting.state["quote_draft"]["conditions"][0]
        assert substitute["qty"] == 500 and substitute["extra_cost_krw"] == 150000
        assert waiting.state["quote_draft"]["unit_price_krw"] == 37300
        assert "register_quote" not in env.names("SG4")

        done = await approve(service, waiting)
        assert done.status == "DONE" and done.waiting_for is None
        assert done.state["registered_quote_id"] == "Q-1"
        assert done.state["bom"] == first.state["bom"] and done.state["final_plan"] == plan
        checked = next(args["parts"] for stage, name, args in env.calls if stage == "SG4" and name == "check_parts_supply")
        assert {p["part_id"]: p["required_qty"] for p in checked} == plan
        assert env.names("SG1").count("get_rfq_content") == 1
        assert env.names("SG1").count("save_review") == 1
        assert env.names("SG3").count("save_quote") == 1
        assert env.names("SG4").count("register_quote") == 1
        assert (await service.get(REQUEST.thread_id)).status == "DONE"
    asyncio.run(run())


def test_sg1_confirmation_preserves_original_and_updates_demand(workflow):
    service, env = workflow
    env.parsed["volume"]["eau"] = None

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG1" and first.waiting_for
        assert env.names("SG1").count("save_review") == 1
        next_job = await service.resume(first.thread_id, ResumeJobRequest(
            interrupt_id=first.waiting_for.id, response={
                "step": "SG1.human_confirm", "approved": True, "user_id": 2,
                "values": {"volume.eau": 120000},
            },
        ))
        assert next_job.current_stage == "SG3", next_job.state.get("error")
        assert next_job.state["parsed_rfq"]["volume"]["eau"] is None
        assert next_job.state["demand"]["eau"] == 120000
        # WAITING 1회, DONE 1회. interrupt 재개에 따른 WAITING 중복 저장 없음.
        assert env.names("SG1").count("save_review") == 2
        final_approval = await approve(service, next_job)
        assert final_approval.state["quote_draft"]["eau"] == 120000
    asyncio.run(run())


def test_no_shortage_passes_entire_bom(workflow):
    service, env = workflow
    env.gateway.chip.update(available=2000, short_qty=0, status="OK")

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG3"
        assert first.state["final_plan"] == {p["part_id"]: 2000 for p in env.gateway.bom["parts"]}
        assert env.writer.calls == []
        assert (await approve(service, await approve(service, first))).status == "DONE"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["no_model", "no_supply", "save_failed"])
def test_failure_does_not_continue_to_next_stage(workflow, failure):
    service, env = workflow
    if failure == "no_model":
        env.no_models = True
    elif failure == "no_supply":
        env.gateway.substitutes["P-MCU-G1"]["substitutes"] = []
    else:
        env.fail_save = True

    async def run():
        job = await service.start(REQUEST)
        if failure == "save_failed":
            job = await approve(service, job)
            assert job.state["sg3_outcome"] == "SAVE_UNCONFIRMED"
            assert env.names("SG3").count("save_quote") == 1
        else:
            assert env.names("SG3") == []
            assert job.state["final_plan"] == {}
        assert job.status == "ON_HOLD" and job.waiting_for is None
        assert env.names("SG4") == []
    asyncio.run(run())


def test_final_reject_ends_without_rewrite_or_registration(workflow):
    service, env = workflow

    async def run():
        waiting = await approve(service, await service.start(REQUEST))
        result = await approve(service, waiting, "REJECTED", "계약 진행 중단")
        assert result.status == "REJECTED"
        assert result.state["draft_revision"] == 1
        assert env.names("SG3").count("save_quote") == 1
        assert "check_parts_supply" not in env.names("SG4")
    asyncio.run(run())


def test_revise_makes_new_quote_and_invalidates_old_approval(workflow):
    service, env = workflow

    async def run():
        waiting = await approve(service, await service.start(REQUEST))
        old_approval = decision(waiting)
        revised = await approve(service, waiting, "REVISE", "대체 부품 문구 수정")
        assert revised.current_stage == "SG3" and revised.state["draft_revision"] == 2
        assert revised.state["final_plan"] == waiting.state["final_plan"]
        new_approval = await approve(service, revised)
        assert new_approval.state["quote_id"] == "Q-2"
        with pytest.raises(HTTPException) as exc:
            await service.resume(waiting.thread_id, old_approval)
        assert exc.value.status_code == 409
        assert "register_quote" not in env.names("SG4")
        assert (await approve(service, new_approval)).status == "DONE"
        assert new_approval.state["quote_ids"] == ["Q-1", "Q-2"]
        assert env.writer.calls[-1][1].content.find("대체 부품 문구 수정") != -1
    asyncio.run(run())


def test_stock_change_replans_all_parts_and_requires_new_approvals(workflow):
    service, env = workflow

    async def run():
        waiting = await approve(service, await service.start(REQUEST))
        env.gateway.chip.update(available=1000, short_qty=1000)
        replanned = await approve(service, waiting)
        assert replanned.current_stage == "SG3", replanned.state.get("error")
        assert replanned.state["draft_revision"] == 2
        assert replanned.state["final_plan"]["P-MCU-G1"] == 1000
        assert replanned.state["final_plan"]["P-MCU-G1-ALT"] == 1000
        assert len(replanned.state["final_plan"]) == 7
        assert "register_quote" not in env.names("SG4")
        assert env.names("SG1").count("get_rfq_content") == 1
        waiting_again = await approve(service, replanned)
        assert waiting_again.state["quote_draft"]["conditions"][0]["extra_cost_krw"] == 300000
        assert (await approve(service, waiting_again)).status == "DONE"
    asyncio.run(run())


def test_rewrite_limit_ends_on_hold(workflow):
    service, env = workflow

    async def run():
        job = await service.start(REQUEST)
        for revision in range(1, 5):
            waiting = await approve(service, job)
            assert waiting.state["draft_revision"] == revision
            job = await approve(service, waiting, "REVISE", "문구 수정")
        assert job.status == "ON_HOLD" and job.waiting_for is None
        assert "한도" in job.state["error"]
        assert env.names("SG3").count("save_quote") == 4
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
        waiting = await approve(service, await service.start(REQUEST))
        done = await approve(service, waiting)
        assert done.status == "ON_HOLD" and done.state["sg4_outcome"] == "FAILED"
        assert "register_quote" not in env.names("SG4")
    asyncio.run(run())


def test_full_substitution_removes_original_from_final_plan(workflow):
    service, env = workflow
    env.gateway.chip.update(available=0, short_qty=2000)

    async def run():
        first = await service.start(REQUEST)
        assert first.current_stage == "SG3", first.state.get("error")
        assert "P-MCU-G1" not in first.state["final_plan"]
        assert first.state["final_plan"]["P-MCU-G1-ALT"] == 2000
        assert len(first.state["final_plan"]) == len(first.state["bom"]) == 6
        assert (await approve(service, await approve(service, first))).status == "DONE"
    asyncio.run(run())


def test_external_failure_is_reported_and_cannot_resume(workflow, monkeypatch):
    from app.rfq_review import mcp_tools
    service, env = workflow

    async def fail(*args, **kwargs):
        raise ConnectionError("MCP 연결 실패")
    monkeypatch.setattr(mcp_tools, "_call", fail)

    async def run():
        job = await service.start(REQUEST)
        assert job.status == "FAILED" and job.waiting_for is None
        assert "MCP 연결 실패" in job.state["error"]
        assert (await service.get(job.thread_id)).status == "FAILED"
        with pytest.raises(HTTPException) as exc:
            await service.resume(job.thread_id, ResumeJobRequest(interrupt_id="old", response={}))
        assert exc.value.status_code == 409
        assert env.names("SG3") == [] and env.names("SG4") == []
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["SG3", "SG4"])
def test_unexpected_write_status_is_not_success(workflow, stage):
    service, env = workflow
    if stage == "SG3":
        env.save_status = "UNKNOWN"
    else:
        env.register_status = "UNKNOWN"

    async def run():
        job = await approve(service, await service.start(REQUEST))
        if stage == "SG4":
            job = await approve(service, job)
        assert job.status == "ON_HOLD" and job.waiting_for is None
        assert "확인 필요" in job.state["error"]
        if stage == "SG3":
            assert env.names("SG4") == []
        else:
            assert env.names("SG4").count("register_quote") == 1
    asyncio.run(run())


def test_duplicate_start_and_resume_do_not_repeat_side_effects(workflow):
    service, env = workflow

    async def run():
        job = await service.start(REQUEST)
        with pytest.raises(HTTPException) as exc:
            await service.start(REQUEST)
        assert exc.value.status_code == 409
        responses = await asyncio.gather(
            service.resume(job.thread_id, decision(job)), service.resume(job.thread_id, decision(job)),
            return_exceptions=True,
        )
        assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in responses) == 1
        assert env.names("SG3").count("save_quote") == 1
    asyncio.run(run())
