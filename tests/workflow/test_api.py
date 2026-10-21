import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.supervisor.service import get_service
from main import app
from tests.workflow.test_workflow import REQUEST


@pytest.fixture
def client(workflow, monkeypatch):
    service, _ = workflow
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-key")
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_service, None)


HEADERS = {"X-Internal-Api-Key": "test-key"}
BODY = REQUEST.model_dump(mode="json", exclude_none=True)
PATH = "/runs/" + REQUEST.thread_id
APPROVAL = {"step": "SG4.request_approval", "userId": 2, "decision": {"result": "APPROVED"}}


def test_api_start_get_resume_through_all_stages(client, workflow):
    _, env = workflow
    assert client.get("/runs/unknown").status_code == 401
    assert client.get("/runs/unknown", headers=HEADERS).status_code == 404
    assert client.post("/runs", json=BODY, headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
    response = client.post("/runs", json=BODY, headers=HEADERS)
    assert response.status_code == 200, response.text
    job = response.json()["result"]
    assert job["current_stage"] == "SG4" and job["status"] == "WAITING"
    assert job["thread_id"] == REQUEST.thread_id
    assert client.post("/runs", json=BODY, headers=HEADERS).json()["result"] == job
    assert client.get(PATH, headers=HEADERS).json()["result"]["waiting_for"] == job["waiting_for"]
    result = client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS)
    assert result.status_code == 200, result.text
    job = result.json()["result"]
    assert job["status"] == "DONE" and len(job["state"]["final_plan"]) == 7
    assert client.post(PATH + "/resume", json=APPROVAL, headers=HEADERS).status_code == 409
    assert env.names("SG4").count("register_quote") == 1


def test_invalid_approval_leaves_checkpoint_pending(client, workflow):
    _, env = workflow
    waiting = client.post("/runs", headers=HEADERS, json=BODY).json()["result"]["waiting_for"]
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
    job = client.post("/runs", json=BODY, headers=HEADERS).json()["result"]
    assert job["current_stage"] == "SG1"
    body = {
        "task_id": REQUEST.task_id, "thread_id": REQUEST.thread_id, "kind": "CLARIFICATION",
        "userId": 2, "decision": {"assumptions": [{"field": "spec.flow", "value": "22 L/min"}]},
    }
    assert client.post(PATH + "/resume", json={**body, "task_id": "wrong"}, headers=HEADERS).status_code == 409
    result = client.post(PATH + "/resume", json=body, headers=HEADERS)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["current_stage"] == "SG4"
    assert result.json()["result"]["state"]["confirmed_by"] == 2


def test_http_failure_exposes_reason_and_duplicate_returns_2xx(client, workflow):
    _, env = workflow
    env.fail_save = True
    response = client.post("/runs", headers=HEADERS, json=BODY)
    assert response.status_code == 502 and "저장" in response.text
    assert client.post("/runs", headers=HEADERS, json=BODY).status_code == 200
    assert env.names("SG3").count("save_quote") == 1


def test_missing_identifiers_and_openapi(client):
    assert client.post("/runs", headers=HEADERS, json={"thread_id": "x"}).status_code == 400
    paths = app.openapi()["paths"]
    assert "/runs" in paths and "/runs/{thread_id}/resume" in paths
    assert "/api/v1/supplysync/runs" not in paths
