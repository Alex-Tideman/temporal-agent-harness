"""Setup failures are recoverable before Temporal starts, without losing a task."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from sdlc_builder import sandbox, workspaces
from sdlc_builder.server import create_app
from sdlc_builder.store import workspace

TASK_ID = "e" * 32


@pytest.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    app.state.temporal_ok = False
    temporal = Mock(side_effect=AssertionError("Preparation must not query Temporal"))
    app.state.client = SimpleNamespace(get_workflow_handle=temporal)
    source = workspaces.demo_project()
    app.state.store.put(
        "projects", {"id": "repo", "name": "repo", "path": str(source), "demo": True}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        await client.post(
            "/api/unlock", json={"token": (tmp_path / "data/session-token").read_text()}
        )
        yield SimpleNamespace(client=client, store=app.state.store, temporal=temporal)


def preparing(context, **extra):
    task = dict(
        id=TASK_ID,
        workflow_id="sdlc-" + TASK_ID,
        project_id="repo",
        dispatch="preparing",
        input={"prompt": "Keep my original request"},
        preparation_error="Workspace already exists",
        **extra,
    )
    context.store.put("tasks", task)
    return task


async def test_retry_without_worker_preserves_task_and_is_idempotent(
    context, monkeypatch
):
    original = preparing(context)
    monkeypatch.setattr(sandbox, "is_remote", lambda root: True)
    retry = Mock(
        return_value={
            "workspace_backend": "e2b",
            "sandbox_id": "saved-sandbox",
            "base_commit": "abc",
        }
    )
    monkeypatch.setattr(sandbox, "retry_preparation", retry)
    current = (await context.client.get(f"/api/tasks/{TASK_ID}")).json()
    assert (
        current["preparation_error"] == "Workspace already exists"
        and not current["preparation_running"]
    )
    for _ in range(2):
        result = await context.client.post(f"/api/tasks/{TASK_ID}/retry-setup")
        assert result.status_code == 200, result.text
        assert result.json()["dispatch"] == "pending"
        assert result.json()["input"] == original["input"]
        assert "preparation_error" not in result.json()
    assert retry.call_count == 1
    context.temporal.assert_not_called()


async def test_failed_retry_keeps_error_and_can_be_retried(context, monkeypatch):
    preparing(context)
    monkeypatch.setattr(sandbox, "is_remote", lambda root: True)
    monkeypatch.setattr(
        sandbox, "retry_preparation", Mock(side_effect=ValueError("E2B unavailable"))
    )
    result = await context.client.post(f"/api/tasks/{TASK_ID}/retry-setup")
    assert result.status_code == 400
    saved = context.store.get("tasks", TASK_ID)
    assert (
        saved["dispatch"] == "preparing"
        and saved["preparation_error"] == "E2B unavailable"
    )
    assert saved["input"]["prompt"] == "Keep my original request"


async def test_setup_interrupted_before_workspace_can_resume_locally(context):
    task = preparing(context)
    task.pop("preparation_error")
    context.store.put("tasks", task)
    home = (await context.client.get("/api/home")).json()
    assert "interrupted" in home["tasks"][0]["preparation_error"]
    result = await context.client.post(f"/api/tasks/{TASK_ID}/retry-setup")
    assert result.status_code == 200 and result.json()["dispatch"] == "pending"
    assert (workspace(TASK_ID) / "greeting.py").exists()


async def test_concurrent_retry_rejected_and_running_setup_is_not_reported_as_interrupted(
    context, monkeypatch
):
    preparing(context)
    monkeypatch.setattr(sandbox, "is_remote", lambda root: True)
    entered, release = threading.Event(), threading.Event()

    def retry(task):
        entered.set()
        assert release.wait(5)
        return {"workspace_backend": "e2b", "sandbox_id": "one"}

    monkeypatch.setattr(sandbox, "retry_preparation", retry)
    first = asyncio.create_task(
        context.client.post(f"/api/tasks/{TASK_ID}/retry-setup")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        state = (await context.client.get(f"/api/tasks/{TASK_ID}")).json()
        assert state["preparation_running"] and not state.get("preparation_error")
        second = await context.client.post(f"/api/tasks/{TASK_ID}/retry-setup")
        assert second.status_code == 409
    finally:
        release.set()
        assert (await first).status_code == 200
