import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.supervisor.service import get_service
from main import app
from tests.workflow.test_workflow import REQUEST, settle


@pytest.fixture
def client(workflow, monkeypatch):
    service, _ = workflow
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-key")
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            try:
                yield client
            finally:
                client.portal.call(service.shutdown)
    finally:
        app.dependency_overrides.pop(get_service, None)


HEADERS = {"X-Internal-Api-Key": "test-key"}
BODY = REQUEST.model_dump(mode="json", exclude_none=True)
PATH = "/runs/" + REQUEST.thread_id
APPROVAL = {"step": "SG4.request_approval", "userId": 2, "decision": {"result": "APPROVED"}}


def finish(client, workflow):
    return client.portal.call(settle, workflow[0]).model_dump()


def test_api_start_get_resume_through_all_stages(client, workflow):
    _, env = workflow
    assert client.get("/runs/unknown").status_code == 401
    assert client.get("/runs/unknown", headers=HEADERS).status_code == 404
    assert client.post("/runs", json=BODY, headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
    response = client.post("/runs", json=BODY, headers=HEADERS)
    assert response.status_code == 202, response.text
    receipt = response.json()["result"]
    assert receipt == {
        "task_id": REQUEST.task_id, "request_id": REQUEST.request_id,
        "thread_id": REQUEST.thread_id, "status": "ACCEPTED",
    }
    job = finish(client, workflow)
    assert job["current_stage"] == "SG4" and job["status"] == "WAITING"
    assert client.post("/runs", json=BODY, headers=HEADERS).json()["result"] == receipt
    assert client.get(PATH, headers=HEADERS).json()["result"]["waiting_for"] == job["waiting_for"]
    result = client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS)
    assert result.status_code == 202, result.text
    assert result.json()["result"]["status"] == "ACCEPTED"
    job = finish(client, workflow)
    assert job["status"] == "DONE" and len(job["state"]["final_plan"]) == 7
    assert client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS).status_code == 409
    assert env.names("SG4").count("register_quote") == 1


def test_invalid_approval_leaves_checkpoint_pending(client, workflow):
    _, env = workflow
    client.post("/runs", headers=HEADERS, json=BODY)
    waiting = finish(client, workflow)["waiting_for"]
    assert client.post(PATH + "/resume", headers=HEADERS, json={"step": "SG4.request_approval"}).status_code == 400
    response = client.post(PATH + "/resume", headers=HEADERS, json={
        **APPROVAL, "decision": {"result": "REVISE"},
    })
    assert response.status_code == 422
    assert client.get(PATH, headers=HEADERS).json()["result"]["waiting_for"] == waiting
    assert "register_quote" not in env.names("SG4")


def test_api_clarification_body_and_user_id(client, workflow):
    _, env = workflow
    env.parsed["spec"]["flow"] = {"value": 22, "unit": "L/h"}
    assert client.post("/runs", json=BODY, headers=HEADERS).status_code == 202
    assert finish(client, workflow)["current_stage"] == "SG1"
    body = {
        "task_id": REQUEST.task_id, "thread_id": REQUEST.thread_id, "kind": "CLARIFICATION",
        "userId": 2, "decision": {"assumptions": [{"field": "spec.flow", "value": "22 L/min"}]},
    }
    assert client.post(PATH + "/resume", json={**body, "task_id": "wrong"}, headers=HEADERS).status_code == 409
    result = client.post(PATH + "/resume", json=body, headers=HEADERS)
    assert result.status_code == 202, result.text
    job = finish(client, workflow)
    assert job["current_stage"] == "SG4" and job["state"]["confirmed_by"] == 2


def test_background_failure_uses_event_and_duplicate_keeps_receipt(client, workflow):
    _, env = workflow
    env.fail_save = True
    response = client.post("/runs", headers=HEADERS, json=BODY)
    assert response.status_code == 202
    job = finish(client, workflow)
    assert job["status"] == "FAILED" and "저장" in job["state"]["error"]
    assert env.events[-1]["type"] == "RUN_FAILED" and env.events[-1]["message"]
    duplicate = client.post("/runs", headers=HEADERS, json=BODY)
    assert duplicate.status_code == 202 and duplicate.json()["result"] == response.json()["result"]
    assert env.names("SG3").count("save_quote") == 1


def test_missing_identifiers_and_openapi(client):
    assert client.post("/runs", headers=HEADERS, json={"thread_id": "x"}).status_code == 400
    paths = app.openapi()["paths"]
    assert "/api/v1/supplysync/runs" not in paths
    for path in ("/runs", "/runs/{thread_id}/resume"):
        assert "202" in paths[path]["post"]["responses"]
        assert "200" not in paths[path]["post"]["responses"]


def test_start_and_resume_return_before_blocked_background_work(client, workflow, monkeypatch):
    from app.supervisor import events
    _, env = workflow
    original = events.post_once
    first_gate = client.portal.call(asyncio.Event)
    resume_gate = client.portal.call(asyncio.Event)

    async def block_event(task_id, event):
        if event["type"] == "STEP_START":
            if event["step"] == "RFQ_ANALYSIS":
                await first_gate.wait()
            if event["step"] == "REVALIDATION":
                await resume_gate.wait()
        await original(task_id, event)
    monkeypatch.setattr(events, "post_once", block_event)
    try:
        first = client.post("/runs", json=BODY, headers=HEADERS)
        assert first.status_code == 202
        assert env.names("SG1") == []
        duplicate = client.post("/runs", json=BODY, headers=HEADERS)
        assert duplicate.status_code == 202 and duplicate.json()["result"] == first.json()["result"]
        client.portal.call(first_gate.set)
        assert finish(client, workflow)["status"] == "WAITING"

        resumed = client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS)
        assert resumed.status_code == 202
        assert "register_quote" not in env.names("SG4")
        assert client.get(PATH, headers=HEADERS).json()["result"]["waiting_for"] is None
        assert client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS).status_code == 409
        client.portal.call(resume_gate.set)
        assert finish(client, workflow)["status"] == "DONE"
    finally:
        client.portal.call(first_gate.set)
        client.portal.call(resume_gate.set)


def test_background_resume_failure_reports_run_failed(client, workflow):
    _, env = workflow
    client.post("/runs", json=BODY, headers=HEADERS)
    finish(client, workflow)
    env.register_status = "UNKNOWN"
    assert client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS).status_code == 202
    assert finish(client, workflow)["status"] == "FAILED"
    assert env.events[-1]["type"] == "RUN_FAILED"
    assert "RUN_END" not in env.names("EVENT")
