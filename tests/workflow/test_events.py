import asyncio
import json

import httpx
import pytest

from app.core.config import settings
from app.supervisor import events


@pytest.mark.parametrize("statuses", [[200], [409], [503, 200], [429, 503, 200]])
def test_event_http_contract_and_retry(monkeypatch, statuses):
    requests = []
    original = httpx.AsyncClient
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "event-test-key")

    def handle(request):
        requests.append(request)
        return httpx.Response(statuses[len(requests) - 1])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handle),
    ))
    event = {"type": "RUN_END", "data": {"outcome": "REGISTERED"}}
    asyncio.run(events.send_event("task-uuid", event))
    assert len(requests) == len(statuses)
    assert all(r.method == "POST" and r.url.path == "/internal/tasks/task-uuid/events" for r in requests)
    assert all(r.headers["X-Internal-Api-Key"] == "event-test-key" for r in requests)
    assert all(json.loads(r.content) == event for r in requests)
    assert str(requests[0].url).startswith(settings.BACKEND_BASE_URL)


@pytest.mark.parametrize("status, attempts", [(401, 1), (503, 3)])
def test_event_failure_is_bounded(monkeypatch, status, attempts):
    calls = []

    async def fail(task_id, event):
        calls.append(event)
        response = httpx.Response(status, request=httpx.Request("POST", "http://be/internal/tasks/id/events"))
        response.raise_for_status()

    monkeypatch.setattr(events, "post_once", fail)
    with pytest.raises(events.EventDeliveryError, match=f"HTTP {status}"):
        asyncio.run(events.send_event("task", {"type": "STEP_START"}))
    assert len(calls) == attempts


def test_tool_callbacks_emit_activity_without_tool_content(monkeypatch):
    calls = []

    async def capture(task_id, event):
        calls.append(event)
    monkeypatch.setattr(events, "post_once", capture)

    async def run():
        progress = events.ProgressEvents("task")
        await progress.start("QUOTE")
        await progress.on_tool_start({"name": "search_policy_docs"}, "private input", run_id="tool-1")
        await progress.on_tool_end("private result", run_id="tool-1")
        await progress.finish()
    asyncio.run(run())
    assert [e["type"] for e in calls] == ["STEP_START", "TOOL_START", "TOOL_END", "STEP_END"]
    assert all(e["step"] == "QUOTE" for e in calls)
    assert calls[1]["data"] == {"tool": "search_policy_docs", "tool_run_id": "tool-1"}
    assert "private" not in json.dumps(calls)
