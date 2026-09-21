from unittest.mock import Mock

from test_collaboration import TASK
from test_collaboration import team as team

from sdlc_builder import integration_queue as queue
from sdlc_builder.coding_workspace import revision
from sdlc_builder.store import workspace


async def test_queue_requires_maintainer_fresh_acceptance_and_same_project(
    team, monkeypatch
):
    bob, _, _ = await team.member()
    viewer, _, _ = await team.member("viewer@example.com", role="viewer")
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "fixture-only")
    team.store.put(
        "profiles",
        {
            "id": "openai",
            "label": "Test",
            "provider": "openai",
            "model": "test",
            "max_steps": 24,
        },
    )
    task = team.store.get("tasks", TASK)
    team.store.put("tasks", {**task, "engine": "v2", "title": "Accepted work"})
    team.handle.query.return_value["value"].update(
        status="accepted", mode="change", revision=revision(workspace(TASK))
    )
    monkeypatch.setattr(
        queue, "capture", lambda *args: [["greeting.py", "fixture patch"]]
    )
    path = "/api/projects/project/integration"
    body = {
        "id": "c" * 32,
        "task_ids": [TASK],
        "expected_versions": {TASK: 1},
        "profile_id": "openai",
    }
    assert (await bob.post(path, json=body)).status_code == 403
    assert (await viewer.post(path, json=body)).status_code == 403
    assert (
        await team.owner.post(path, json={**body, "expected_versions": {TASK: 0}})
    ).status_code == 409
    result = await team.owner.post(path, json=body)
    assert result.status_code == 200, result.text
    assert (await team.owner.post(path, json=body)).json()["id"] == body["id"]
    assert len(queue.load("project")["items"]) == 1
    listed = (await viewer.get(path)).json()
    assert not listed["can_manage"]
    assert "inputs" not in listed["items"][0] and "profile" not in listed["items"][0]
    assert (await bob.post(path + "/" + body["id"] + "/prepare")).status_code == 403
    assert (await bob.post(path + "/" + body["id"] + "/cancel")).status_code == 403
    team.store.put(
        "tasks", {"id": "d" * 32, "project_id": "private", "title": "Private"}
    )
    denied = await team.owner.post(
        path, json={**body, "id": "e" * 32, "task_ids": ["d" * 32]}
    )
    assert denied.status_code == 404
    cancelled = await team.owner.post(path + "/" + body["id"] + "/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


async def test_publication_rejects_stale_versions_and_freezes_edits(team, monkeypatch):
    bob, _, _ = await team.member()
    task = team.store.get("tasks", TASK)
    task.update(integration_id=TASK, engine="v2", workspace_backend="e2b")
    team.store.put("tasks", task)
    queue.save(
        "project",
        {"items": [{"id": TASK, "task_id": TASK, "status": "testing", "tasks": []}]},
    )
    publish = Mock(return_value={"status": "published"})
    monkeypatch.setattr(queue, "publish", publish)
    path = f"/api/projects/project/integration/{TASK}/publish"
    payload = {"expected_version": 0, "revision": "e" * 64}
    assert (await bob.post(path, json=payload)).status_code == 403
    assert (await team.owner.post(path, json=payload)).status_code == 409
    publish.assert_not_called()
    response = await team.owner.post(path, json={**payload, "expected_version": 1})
    assert response.status_code == 200
    assert publish.call_args.args[-1]["name"] == "Alice"
    queue.save(
        "project",
        {
            "items": [
                {
                    "id": TASK,
                    "task_id": TASK,
                    "status": "publishing",
                    "publication": {"commit": "e" * 40},
                }
            ]
        },
    )
    permission = (await team.owner.get(f"/api/tasks/{TASK}")).json()
    assert not permission["can_control"]
    denied = await team.owner.put(
        f"/api/tasks/{TASK}/file",
        json={"path": "greeting.py", "content": "new", "expected_hash": ""},
    )
    assert denied.status_code == 409
    assert (await team.owner.delete(f"/api/tasks/{TASK}")).status_code == 409
