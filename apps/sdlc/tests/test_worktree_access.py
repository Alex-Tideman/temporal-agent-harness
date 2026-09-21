from unittest.mock import Mock

from test_collaboration import TASK
from test_collaboration import team as team

from sdlc_builder import project_workspaces, workspaces
from sdlc_builder.models import Control


def shared_task(team, owner_id):
    task = team.store.get("tasks", TASK)
    task.update(
        title="Shared task",
        workspace_layout="project-worktree",
        workspace_backend="e2b",
        owner={"id": owner_id, "name": "Bob"},
        ownership_version=1,
    )
    team.store.put("tasks", task)
    return task


async def test_owner_controls_and_maintainer_review_remain_separate(team, monkeypatch):
    bob, person, _ = await team.member()
    shared_task(team, person["id"])
    for path, body in [
        ("control", {"command": "respond", "gate_id": "plan"}),
        (
            "messages",
            {
                "id": "c" * 32,
                "expected_turn": 2,
                "max_steps": 24,
                "prompt": "change",
                "mode": "change",
                "profile_id": "demo",
            },
        ),
        ("retry-setup", {}),
        ("preview", {}),
    ]:
        response = await team.owner.post(f"/api/tasks/{TASK}/{path}", json=body)
        assert response.status_code == 403, response.text
    denied = await team.owner.put(
        f"/api/tasks/{TASK}/file",
        json={"path": "greeting.py", "content": "changed", "expected_hash": ""},
    )
    assert denied.status_code == 403
    permissions = (await team.owner.get(f"/api/tasks/{TASK}")).json()
    assert (
        not permissions["can_control"]
        and permissions["can_accept"]
        and permissions["can_handoff"]
    )
    accepted = await team.owner.post(
        f"/api/tasks/{TASK}/control", json={"command": "accept", "expected_version": 1}
    )
    assert accepted.status_code == 200
    # Maintainers can stop an abandoned task without gaining edit/approval access.
    stopped = await team.owner.post(
        f"/api/tasks/{TASK}/control", json={"command": "cancel"}
    )
    assert stopped.status_code == 200
    allowed = await bob.post(
        f"/api/tasks/{TASK}/control", json={"command": "respond", "gate_id": "plan"}
    )
    assert allowed.status_code == 200
    assert (await bob.get(f"/api/tasks/{TASK}")).json()["can_control"]


async def test_handoff_requires_idle_membership_and_current_owner_version(team):
    bob, person, _ = await team.member()
    charlie, recipient, _ = await team.member("charlie@example.com", name="Charlie")
    _, viewer, _ = await team.member("viewer@example.com", role="viewer")
    shared_task(team, person["id"])
    path = f"/api/tasks/{TASK}/handoff"
    request = {"user_id": recipient["id"], "expected_ownership_version": 1}
    assert (await charlie.post(path, json=request)).status_code == 403
    team.handle.query.return_value["can_message"] = False
    assert (await bob.post(path, json=request)).status_code == 409
    team.handle.query.return_value["can_message"] = True
    assert (
        await bob.post(path, json={**request, "user_id": viewer["id"]})
    ).status_code == 400
    assert (
        await bob.post(path, json={**request, "user_id": "non-member"})
    ).status_code == 400
    result = await bob.post(path, json=request)
    assert result.status_code == 200, result.text
    assert result.json()["owner"]["id"] == recipient["id"]
    assert result.json()["ownership_version"] == 2
    assert not (await bob.get(f"/api/tasks/{TASK}")).json()["can_control"]
    assert (await charlie.get(f"/api/tasks/{TASK}")).json()["can_control"]
    assert (await team.owner.post(path, json=request)).status_code == 409
    events = team.team.events("project", TASK)
    assert events[0]["action"] == "task.handoff"
    assert events[0]["detail"]["from"]["id"] == person["id"]
    team.handle.execute_update.assert_not_called()


async def test_new_shared_tasks_opt_into_worktrees_and_agent_coordination(
    team, monkeypatch
):
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "test-only")

    def prepare(source, identity):
        task = team.store.get("tasks", identity)
        assert task["workspace_layout"] == "project-worktree"
        assert task["owner"]["name"] == "Alice"
        assert task["input"]["project_coordination"]
        return {
            "workspace_backend": "e2b",
            "workspace": "e2b://fixture/worktree",
            "sandbox_id": "fixture",
            "base_commit": "base",
        }

    monkeypatch.setattr(workspaces, "prepare_workspace", prepare)
    response = await team.owner.post(
        "/api/tasks",
        json={
            "id": "b" * 32,
            "project_id": "project",
            "profile_id": "demo",
            "prompt": "Build something",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ownership_version"] == 1
    response = await team.owner.post(
        "/api/tasks",
        json={
            "id": "c" * 32,
            "project_id": "project",
            "profile_id": "demo",
            "prompt": "Build",
            "engine": "v1",
        },
    )
    assert response.status_code == 400


async def test_recovery_checks_quiescence_and_resets_evidence_before_unlock(
    team, monkeypatch
):
    _, bob, _ = await team.member()
    shared_task(team, bob["id"])
    state = {
        "status": "recovering",
        "tasks": {TASK: 20000},
        "sandbox_id": "replacement",
    }
    monkeypatch.setattr(project_workspaces, "project_state", lambda _: state)
    restored = Mock(
        return_value={
            "sandbox_id": "replacement",
            "tasks": [
                {
                    "id": TASK,
                    "sandbox_id": "replacement",
                    "workspace": "e2b://replacement/worktree",
                }
            ],
        }
    )
    finished = Mock()
    monkeypatch.setattr(project_workspaces, "recover", restored)
    monkeypatch.setattr(project_workspaces, "finish_recovery", finished)
    team.handle.query.return_value["can_message"] = False
    response = await team.owner.post("/api/projects/project/sandbox/recover")
    assert response.status_code == 409
    restored.assert_not_called()
    team.handle.query.return_value["can_message"] = True
    team.handle.execute_update.return_value = {"ok": False, "error": "not idle"}
    response = await team.owner.post("/api/projects/project/sandbox/recover")
    assert response.status_code == 409
    finished.assert_not_called()
    blocked = await team.owner.post(
        f"/api/tasks/{TASK}/control", json={"command": "accept", "expected_version": 1}
    )
    assert blocked.status_code == 409
    team.handle.execute_update.return_value = {"ok": True}
    response = await team.owner.post("/api/projects/project/sandbox/recover")
    assert response.status_code == 200, response.text
    team.handle.execute_update.assert_awaited_with(
        "control", Control(command="workspace_restored")
    )
    finished.assert_called_once_with("project")
    assert team.store.get("tasks", TASK)["sandbox_id"] == "replacement"


async def test_coordination_notes_are_project_scoped_and_attributed(team):
    bob, person, _ = await team.member()
    shared_task(team, person["id"])
    team.store.put(
        "tasks", {"id": "b" * 32, "project_id": "project", "title": "Related"}
    )
    team.store.put(
        "tasks", {"id": "c" * 32, "project_id": "private-project", "title": "Private"}
    )
    payload = {
        "id": "d" * 32,
        "target_id": "b" * 32,
        "message": "Please coordinate the response fields.",
    }
    response = await bob.post(f"/api/tasks/{TASK}/coordination", json=payload)
    assert response.status_code == 200, response.text
    await bob.post(f"/api/tasks/{TASK}/coordination", json=payload)
    board = (await bob.get(f"/api/tasks/{TASK}/coordination")).json()
    assert len(board["notes"]) == 1
    assert board["notes"][0]["author"]["id"] == person["id"]
    assert [t["id"] for t in board["tasks"]] == ["b" * 32]
    response = await bob.post(
        f"/api/tasks/{TASK}/coordination", json={**payload, "target_id": "c" * 32}
    )
    assert response.status_code == 404
