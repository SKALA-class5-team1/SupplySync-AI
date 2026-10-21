from fastapi.testclient import TestClient

from main import app

PATH = "/api/v1/supplysync/supply-analysis/analyze"


def test_api_response_and_openapi(gateway, request_data, substitute_plan):
    with TestClient(app) as client:
        response = client.post(PATH, json=request_data)
        assert response.status_code == 200
        body = response.json()
        assert body["metadata"]["status"]
        assert body["result"]["next_step"] == "quote_drafting"
        assert body["result"]["final_plan"] == substitute_plan
        schema = client.get("/api/v1/supplysync/openapi.json").json()
        assert PATH in schema["paths"]
        assert "SupplyAnalysisRequest" in schema["components"]["schemas"]


def test_api_invalid_request_uses_existing_error_envelope(gateway, request_data):
    request_data["demand"]["sample_qty"] = -1
    with TestClient(app) as client:
        response = client.post(PATH, json=request_data)
    assert response.status_code == 400
    assert response.json()["metadata"]["code"] == "E1001"
    assert gateway.calls == []


def test_hold_is_exposed_without_saving(gateway, request_data):
    gateway.alt_docs = []
    with TestClient(app) as client:
        response = client.post(PATH, json=request_data)
    assert response.status_code == 200
    assert response.json()["result"]["next_step"] == "hold"
    assert response.json()["result"]["saved"] is False
