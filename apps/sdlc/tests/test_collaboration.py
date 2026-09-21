"""Shared accounts and project boundaries with separate browser sessions."""

import asyncio
import json
import time
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sdlc_builder.collaboration import LOCAL_ID, Collaboration, token_hash
from sdlc_builder.server import create_app
from sdlc_builder.store import Store, workspace
from sdlc_builder.team_api import SharedWebsocketBoundary
from sdlc_builder.workspaces import demo_project, prepare_workspace

TASK = "a" * 32
PASSWORD = "a long test password"
ORIGIN = "https://team.example.test"


@pytest.fixture
async def team(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SDLC_PUBLIC_URL", ORIGIN)
    store = Store()
    source = demo_project()
    store.put(
        "projects",
        {"id": "project", "name": "Team project", "path": str(source), "demo": True},
    )
    prepared = prepare_workspace(str(source), TASK)
    store.put(
        "tasks",
        {
            "id": TASK,
            "workflow_id": "sdlc-" + TASK,
            "project_id": "project",
            "project_name": "Team project",
            "dispatch": "submitted",
            **prepared,
        },
    )
    app = create_app()
    app.state.temporal_ok = False
    handle = SimpleNamespace(
        query=AsyncMock(
            return_value={
                "version": 1,
                "value": {"status": "review", "revision": "revision-1"},
                "can_message": True,
            }
        ),
        execute_update=AsyncMock(return_value={"ok": True}),
    )
    app.state.client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    async with AsyncExitStack() as stack:

        async def client():
            return await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=ORIGIN,
                    headers={"X-SDLC": "1"},
                )
            )

        owner = await client()
        setup_token = (tmp_path / "session-token").read_text()
        bootstrap = await owner.post(
            "/api/auth/bootstrap",
            json={
                "email": "owner@example.com",
                "name": "Alice",
                "password": PASSWORD,
                "token": setup_token,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text

        async def member(email="bob@example.com", role="developer", name="Bob"):
            invited = await owner.post(
                "/api/projects/project/invitations", json={"email": email, "role": role}
            )
            assert invited.status_code == 200, invited.text
            guest = await client()
            body = {
                "email": email,
                "name": name,
                "password": PASSWORD,
                "token": invited.json()["url"].split("#invite=")[1],
            }
            joined = await guest.post("/api/auth/join", json=body)
            assert joined.status_code == 200, joined.text
            return guest, joined.json()["user"], body

        yield SimpleNamespace(
            app=app,
            owner=owner,
            client=client,
            member=member,
            store=store,
            team=app.state.team,
            token=setup_token,
            root=tmp_path,
            handle=handle,
        )


async def test_sessions_bootstrap_logout_expiry_and_csrf(team):
    stranger = await team.client()
    assert (await stranger.get("/api/home")).status_code == 401
    stranger.cookies.set("sdlc_session", team.token)
    assert (await stranger.get("/api/home")).status_code == 401
    assert (
        await stranger.post("/api/unlock", json={"token": team.token})
    ).status_code == 409
    assert (
        await team.owner.post(
            "/api/auth/bootstrap",
            json={
                "email": "next@example.com",
                "name": "Other",
                "password": PASSWORD,
                "token": team.token,
            },
        )
    ).status_code == 409
    assert (
        await team.owner.post(
            "/api/auth/logout", headers={"Origin": "https://attacker.example"}
        )
    ).status_code == 403
    with team.store.connect() as db:
        stored = db.execute(
            "SELECT password FROM users WHERE id=?", (LOCAL_ID,)
        ).fetchone()[0]
        assert stored.startswith("scrypt$") and PASSWORD not in stored
        hashes = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
    token = team.owner.cookies.get("sdlc_session")
    assert token not in hashes and token_hash(token) in hashes
    assert (await team.owner.post("/api/auth/logout")).status_code == 200
    stranger.cookies.clear()
    stranger.cookies.set("sdlc_session", token)
    assert (await stranger.get("/api/home")).status_code == 401
    signed = await stranger.post(
        "/api/auth/login", json={"email": "OWNER@example.com", "password": PASSWORD}
    )
    assert signed.status_code == 200
    assert (
        "HttpOnly" in signed.headers["set-cookie"]
        and "Secure" in signed.headers["set-cookie"]
        and "SameSite=strict" in signed.headers["set-cookie"]
    )
    with team.store.connect() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert (await stranger.get("/api/home")).status_code == 401


async def test_project_scoping_and_revocation_cover_all_task_surfaces(team):
    bob, user, _ = await team.member()
    team.store.put("projects", {"id": "private", "name": "Private", "path": "/hidden"})
    private_id = "b" * 32
    team.store.put(
        "tasks",
        {
            "id": private_id,
            "project_id": "private",
            "title": "secret",
            "dispatch": "submitted",
        },
    )
    home = (await bob.get("/api/home")).json()
    assert [p["id"] for p in home["projects"]] == ["project"]
    assert [t["id"] for t in home["tasks"]] == [TASK]
    for suffix in [
        "",
        "/files",
        "/file?path=test.txt",
        "/diff",
        "/review",
        "/review/file?path=test.txt",
        "/patch",
        "/preview",
        "/activity",
    ]:
        response = await bob.get(f"/api/tasks/{private_id}" + suffix)
        assert response.status_code == 404, suffix
        assert "secret" not in response.text
    leaked = await bob.post(
        "/api/tasks",
        json={
            "id": private_id,
            "project_id": "project",
            "prompt": "do something",
            "profile_id": "demo",
            "mode": "change",
        },
    )
    assert leaked.status_code == 404
    assert (await bob.get("/harness/api/sessions")).status_code == 403
    assert (await bob.get("/api/projects/private/members")).status_code == 404
    assert (
        await team.owner.delete(f"/api/projects/project/members/{user['id']}")
    ).status_code == 200
    assert (await bob.get(f"/api/tasks/{TASK}/review")).status_code == 404
    assert (await bob.get("/api/home")).json()["projects"] == []


async def test_viewer_and_developer_permissions_are_enforced_server_side(team):
    viewer, _, _ = await team.member("viewer@example.com", "viewer", "Val")
    dev, _, _ = await team.member()
    assert (await viewer.get(f"/api/tasks/{TASK}/review")).status_code == 200
    for method, path, body in [
        (
            "POST",
            "/api/tasks",
            {
                "id": "c" * 32,
                "project_id": "project",
                "profile_id": "demo",
                "prompt": "test",
                "mode": "change",
            },
        ),
        ("POST", f"/api/tasks/{TASK}/control", {"command": "accept"}),
        ("PUT", f"/api/tasks/{TASK}/review/checkpoint", {}),
        ("POST", f"/api/tasks/{TASK}/review/comments", {}),
        ("POST", f"/api/tasks/{TASK}/preview", {}),
        ("PUT", f"/api/tasks/{TASK}/file", {}),
        ("DELETE", f"/api/tasks/{TASK}", {}),
    ]:
        response = await viewer.request(method, path, json=body)
        assert response.status_code == 403, response.text
    task = team.store.get("tasks", TASK)
    task["workspace_backend"] = "e2b"  # permissions only; no sandbox calls
    team.store.put("tasks", task)
    for path, body in [
        (f"/api/tasks/{TASK}/control", {"command": "accept"}),
        (f"/api/tasks/{TASK}/merge", {}),
        ("/api/projects/project/invitations", {"email": "x@example.com"}),
        ("/api/profiles", {}),
        ("/api/projects", {"path": "/"}),
    ]:
        assert (await dev.post(path, json=body)).status_code == 403
    assert (await dev.get("/api/projects/folders")).status_code == 403
    assert (
        await dev.post(f"/api/tasks/{TASK}/control", json={"command": "pause"})
    ).status_code == 200
    activity = (await dev.get(f"/api/tasks/{TASK}/activity")).json()
    assert (
        activity[0]["actor"]["name"] == "Bob" and activity[0]["action"] == "task.pause"
    )
    assert activity[0]["status"] == "completed"
    assert (await dev.get(f"/api/tasks/{TASK}")).json()["can_accept"] is False


async def test_hide_and_restore_are_personal_and_durable(team):
    bob, _, _ = await team.member()
    assert (await bob.delete("/api/projects/project")).status_code == 200
    assert (await bob.get("/api/home")).json()["projects"] == []
    assert len((await team.owner.get("/api/home")).json()["projects"]) == 1
    assert len((await bob.get("/api/projects/hidden")).json()) == 1
    assert not team.store.get("projects", "project").get("removed")
    assert (await bob.post("/api/projects/project/show")).status_code == 200
    assert len((await bob.get("/api/home")).json()["tasks"]) == 1
    assert workspace(TASK).exists()


async def test_invitation_single_use_bound_email_password_and_last_owner(team):
    bob, user, body = await team.member()
    assert (await bob.post("/api/auth/join", json=body)).status_code == 400
    invited = (
        await team.owner.post(
            "/api/projects/project/invitations",
            json={"email": "bob@example.com", "role": "viewer"},
        )
    ).json()
    body["token"] = invited["url"].split("#invite=")[1]
    stranger = await team.client()
    assert (
        await stranger.post(
            "/api/auth/join", json={**body, "email": "someone@example.com"}
        )
    ).status_code == 400
    assert (
        await stranger.post(
            "/api/auth/join", json={**body, "password": "wrong long password"}
        )
    ).status_code == 401
    assert (await bob.post("/api/auth/join", json=body)).status_code == 200
    assert (await bob.get("/api/projects/project/members")).json()[
        "role"
    ] == "developer"
    assert (
        await team.owner.delete(f"/api/projects/project/members/{LOCAL_ID}")
    ).status_code == 409
    assert (
        await team.owner.put(
            f"/api/projects/project/members/{LOCAL_ID}", json={"role": "viewer"}
        )
    ).status_code == 409
    assert (
        await team.owner.put(
            f"/api/projects/project/members/{user['id']}", json={"role": "owner"}
        )
    ).status_code == 200
    assert (
        await team.owner.put(
            f"/api/projects/project/members/{LOCAL_ID}", json={"role": "maintainer"}
        )
    ).status_code == 200


async def test_expired_and_revoked_invites_cannot_be_reused(team):
    guest = await team.client()
    for expired in [True, False]:
        invite = (
            await team.owner.post(
                "/api/projects/project/invitations", json={"email": "new@example.com"}
            )
        ).json()
        if expired:
            with team.store.connect() as db:
                db.execute(
                    "UPDATE invitations SET expires=0 WHERE id=?", (invite["id"],)
                )
        else:
            await team.owner.delete("/api/projects/project/invitations/" + invite["id"])
        response = await guest.post(
            "/api/auth/join",
            json={
                "name": "New",
                "email": "new@example.com",
                "password": PASSWORD,
                "token": invite["url"].split("#invite=")[1],
            },
        )
        assert response.status_code == 400


async def test_independent_checkpoints_comments_and_resolution_attribution(team):
    bob, _, _ = await team.member()
    (workspace(TASK) / "greeting.py").write_text("changed by agent\n")
    path = f"/api/tasks/{TASK}/review"
    detail = (
        await team.owner.get(path + "/file", params={"path": "greeting.py"})
    ).json()
    checkpoint = {
        "path": "greeting.py",
        "token": detail["source_token"],
        "reviewed": True,
    }
    assert (
        await team.owner.put(path + "/checkpoint", json=checkpoint)
    ).status_code == 200
    assert (await team.owner.get(path)).json()["files"][0]["reviewed"]
    assert not (await bob.get(path)).json()["files"][0]["reviewed"]
    assert (await bob.get(path)).json()["files"][0]["reviewed_by"] == [
        {"id": LOCAL_ID, "name": "Alice"}
    ]
    result = await asyncio.gather(
        bob.put(path + "/checkpoint", json=checkpoint),
        team.owner.put(path + "/checkpoint", json={**checkpoint, "reviewed": False}),
    )
    assert all(r.status_code == 200 for r in result)
    assert not (await team.owner.get(path)).json()["files"][0]["reviewed"]
    assert (await bob.get(path)).json()["files"][0]["reviewed"]
    comment = {
        "id": "d" * 32,
        "path": "greeting.py",
        "token": detail["token"],
        "comparison": "all",
        "side": "new",
        "line": 1,
        "text": "Please improve this",
        "author": {"id": LOCAL_ID, "name": "Alice"},
    }
    assert (await bob.post(path + "/comments", json=comment)).status_code == 200
    assert (await bob.post(path + "/comments", json=comment)).status_code == 200
    assert (await team.owner.post(path + "/comments", json=comment)).status_code == 409
    assert (
        await team.owner.put(
            path + "/comments/" + comment["id"], json={"resolved": True}
        )
    ).status_code == 200
    saved = (await bob.get(path)).json()["comments"][0]
    assert saved["author"]["name"] == "Bob" and saved["resolved_by"]["name"] == "Alice"


async def test_legacy_local_execution_and_new_local_tasks_are_blocked(team):
    bob, _, _ = await team.member()
    assert (
        await bob.post(f"/api/tasks/{TASK}/control", json={"command": "pause"})
    ).status_code == 403
    response = await bob.post(
        "/api/tasks",
        json={
            "id": "f" * 32,
            "project_id": "project",
            "profile_id": "demo",
            "prompt": "test",
            "mode": "change",
        },
    )
    assert response.status_code == 400 and "require E2B" in response.text
    assert team.handle.execute_update.await_count == 0


async def test_uncertain_decisions_are_attributed_without_claiming_success(team):
    team.handle.execute_update.side_effect = RuntimeError("lost connection")
    with pytest.raises(RuntimeError, match="lost connection"):
        await team.owner.post(
            f"/api/tasks/{TASK}/control",
            json={"command": "accept", "expected_version": 1},
        )
    event = (await team.owner.get(f"/api/tasks/{TASK}/activity")).json()[0]
    assert event["status"] == "unconfirmed" and event["actor"]["name"] == "Alice"


async def test_shared_debugger_websockets_are_closed_before_dispatch():
    underlying, send = AsyncMock(), AsyncMock()
    wrapper = SharedWebsocketBoundary(underlying, enabled=True)
    await wrapper({"type": "websocket", "path": "/harness/ws"}, AsyncMock(), send)
    underlying.assert_not_awaited()
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})


def test_legacy_visibility_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("SDLC_PUBLIC_URL", raising=False)
    store = Store(tmp_path)
    store.put("projects", {"id": "old", "name": "Old", "removed": True})
    team = Collaboration(store)
    assert team.projects() == [] and len(team.projects(hidden=True)) == 1
    assert not store.get("projects", "old").get("removed")
    team.preference("old", hidden=False)
    assert len(Collaboration(store).projects()) == 1
    assert len(team.members("old")) == 1


def test_rate_limit_persists_without_recording_email(tmp_path, monkeypatch):
    monkeypatch.delenv("SDLC_PUBLIC_URL", raising=False)
    team = Collaboration(Store(tmp_path))
    for n in range(10):
        team.throttle(str(n), "victim@example.com")
    with pytest.raises(Exception) as caught:
        team.throttle("new", "victim@example.com")
    assert caught.value.status_code == 429
    with team.store.connect() as db:
        assert "victim" not in json.dumps(
            [dict(r) for r in db.execute("SELECT * FROM auth_attempts")]
        )


async def test_stale_acceptance_does_not_reach_the_worker(team):
    for payload in [
        {"command": "accept"},
        {"command": "accept", "expected_version": 0},
    ]:
        result = await team.owner.post(f"/api/tasks/{TASK}/control", json=payload)
        assert result.status_code == 409
    team.handle.execute_update.assert_not_awaited()
    result = await team.owner.post(
        f"/api/tasks/{TASK}/control", json={"command": "accept", "expected_version": 1}
    )
    assert result.status_code == 200
    event = (await team.owner.get(f"/api/tasks/{TASK}/activity")).json()[0]
    assert event["detail"]["revision"] == "revision-1"


async def test_remote_connection_deduplicates_without_granting_another_user_access(
    team, monkeypatch
):
    from unittest.mock import Mock

    from sdlc_builder import remote_repositories

    bob, _, _ = await team.member()
    connector = Mock(
        side_effect=lambda url, identity: {
            "id": identity,
            "name": "remote",
            "path": str(team.root / identity),
            "remote_url": url,
            "github_url": url,
            "default_branch": "main",
            "source": "github",
        }
    )
    monkeypatch.setattr(remote_repositories, "connect", connector)
    first = await team.owner.post(
        "/api/projects/remote", json={"url": "https://github.com/Example/Remote.git"}
    )
    assert first.status_code == 200
    project = first.json()
    second = await team.owner.post(
        "/api/projects/remote", json={"url": "https://github.com/example/remote/"}
    )
    assert second.json()["id"] == project["id"]
    assert connector.call_count == 1
    assert (
        await bob.post(
            "/api/projects/remote", json={"url": "https://github.com/example/remote"}
        )
    ).status_code == 404
    assert (await team.owner.get(f"/api/projects/{project['id']}/members")).json()[
        "role"
    ] == "owner"


async def test_followup_audit_keeps_initiator_after_delivery_retry(team):
    task = team.store.get("tasks", TASK)
    task["workspace_backend"] = "e2b"
    team.store.put("tasks", task)
    bob, user, _ = await team.member()
    team.handle.execute_update.side_effect = [
        RuntimeError("disconnected"),
        SimpleNamespace(turn_number=2),
    ]
    body = {
        "id": "e" * 32,
        "expected_turn": 2,
        "prompt": "Improve it",
        "mode": "change",
        "profile_id": "demo",
        "max_steps": 20,
    }
    response = await bob.post(f"/api/tasks/{TASK}/messages", json=body)
    assert response.status_code == 200 and response.json()["status"] == "pending"
    response = await team.owner.post(f"/api/tasks/{TASK}/messages", json=body)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    events = (await team.owner.get(f"/api/tasks/{TASK}/activity")).json()
    assert len(events) == 1
    assert events[0]["actor"]["id"] == user["id"] and events[0]["status"] == "completed"
