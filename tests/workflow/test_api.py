from fastapi.testclient import TestClient

from app.core.config import settings
from app.supervisor.service import get_service
from main import app


def test_api_start_get_resume_through_all_stages(workflow):
    service, env = workflow
    app.dependency_overrides[get_service] = lambda: service
    prefix = settings.API_V1_PREFIX + "/jobs"
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
    try:
        with TestClient(app) as client:
            assert client.get(prefix + "/unknown").status_code == 401
            assert client.get(prefix + "/unknown", headers=headers).status_code == 404
            body = {"thread_id": "api-workflow", "rfq_number": "HB-EV7-EWP-001", "base_date": "2026-10-19"}
            response = client.post(prefix, json=body, headers=headers)
            assert response.status_code == 200, response.text
            job = response.json()["result"]
            assert job["current_stage"] == "SG3" and job["status"] == "WAITING"
            assert client.post(prefix, json=body, headers=headers).status_code == 409
            path = prefix + "/api-workflow"
            assert client.get(path, headers=headers).json()["result"]["waiting_for"] == job["waiting_for"]

            wrong_step = {"interrupt_id": job["waiting_for"]["id"], "response": {"step": "SG4.request_approval"}}
            assert client.post(path + "/resume", json=wrong_step, headers=headers).status_code == 409
            assert "save_quote" not in env.names("SG3")
            for stage in ("SG3", "SG4"):
                assert job["current_stage"] == stage
                request = {"interrupt_id": job["waiting_for"]["id"], "response": {
                    "step": job["waiting_for"]["payload"]["step"], "userId": 2,
                    "decision": {"result": "APPROVED"},
                }}
                result = client.post(path + "/resume", json=request, headers=headers)
                assert result.status_code == 200, result.text
                job = result.json()["result"]
            assert job["status"] == "DONE" and len(job["state"]["final_plan"]) == 7
            assert client.post(path + "/resume", json=request, headers=headers).status_code == 409
            assert env.names("SG4").count("register_quote") == 1
    finally:
        app.dependency_overrides.pop(get_service, None)


def test_invalid_approval_leaves_checkpoint_pending(workflow):
    service, env = workflow
    app.dependency_overrides[get_service] = lambda: service
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
    prefix = settings.API_V1_PREFIX + "/jobs"
    try:
        with TestClient(app) as client:
            response = client.post(prefix, headers=headers, json={
                "thread_id": "invalid", "rfq_number": "HB-EV7-EWP-001", "base_date": "2026-10-19",
            })
            waiting = response.json()["result"]["waiting_for"]
            response = client.post(prefix + "/invalid/resume", headers=headers, json={
                "interrupt_id": waiting["id"], "response": {"step": waiting["payload"]["step"]},
            })
            assert response.status_code == 422
            assert client.get(prefix + "/invalid", headers=headers).json()["result"]["waiting_for"] == waiting
            assert "save_quote" not in env.names("SG3")
    finally:
        app.dependency_overrides.pop(get_service, None)
