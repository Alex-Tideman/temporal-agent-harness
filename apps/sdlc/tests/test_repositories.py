import asyncio
import subprocess
import threading
from types import SimpleNamespace

import httpx
import pytest

from sdlc_builder import repositories, workspaces
from sdlc_builder.server import create_app
from sdlc_builder.store import Store


@pytest.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        await client.post(
            "/api/unlock", json={"token": (tmp_path / "data/session-token").read_text()}
        )
        yield SimpleNamespace(
            app=app, client=client, store=app.state.store, root=tmp_path
        )


def repo(path, *, worktree=False):
    path.mkdir(parents=True)
    if worktree:
        (path / ".git").write_text("gitdir: /not/followed/by/discovery")
    else:
        (path / ".git").mkdir()
    return path


def test_discovery_stays_inside_chosen_folder_and_stops_at_repositories(tmp_path):
    store = Store(tmp_path / "data")
    projects = tmp_path / "Projects"
    first = repo(projects / "one")
    second = repo(projects / "team" / "two", worktree=True)
    repo(first / "nested")
    outside = repo(tmp_path / "outside")
    (projects / "outside-link").symlink_to(outside, target_is_directory=True)
    repo(projects / "node_modules" / "ignored")
    repo(projects / ".hidden" / "ignored")
    repo(projects / "too" / "deep" / "to" / "find")
    assert repositories.discover(store)["suggestions"] == []
    store.save_setting("projects_folder", str(projects))
    store.put("projects", {"id": "one", "path": str(first)})
    result = repositories.discover(store)
    assert [item["path"] for item in result["suggestions"]] == [str(first), str(second)]
    assert result["suggestions"][0]["connected_id"] == "one"
    assert Store(tmp_path / "data").setting("projects_folder") == str(projects)


def test_discovery_is_bounded_and_missing_folder_is_recoverable(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    projects = tmp_path / "Projects"
    for n in range(5):
        repo(projects / f"repo-{n}")
    store.save_setting("projects_folder", str(projects))
    monkeypatch.setattr(repositories, "MAX_RESULTS", 2)
    result = repositories.discover(store)
    assert len(result["suggestions"]) == 2 and result["truncated"]
    store.save_setting("projects_folder", str(tmp_path / "deleted"))
    assert repositories.discover(store)["warning"]


def test_folder_browser_only_lists_visible_directories(tmp_path):
    root = tmp_path / "Projects with spaces"
    first = repo(root / "my repo")
    (root / "secret.txt").write_text("not exposed")
    (root / ".private").mkdir()
    (root / "alias").symlink_to(first, target_is_directory=True)
    result = repositories.browse(str(root))
    assert result["folders"] == [
        {"name": "my repo", "path": str(first), "repository": True}
    ]
    assert result["parents"][-1]["path"] == str(root)
    assert result["parents"][0]["path"] == "/"
    with pytest.raises(ValueError, match="no longer available"):
        repositories.browse(str(root / "gone"))


@pytest.mark.parametrize("outcome", ["selected", "cancelled", "timeout", "failure"])
def test_native_picker_outcomes_without_shell_interpolation(
    tmp_path, monkeypatch, outcome
):
    monkeypatch.setattr(repositories.sys, "platform", "darwin")
    path = tmp_path / "Project with spaces;$(no-shell)"
    path.mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 180)
        return SimpleNamespace(
            returncode=0 if outcome == "selected" else 1,
            stdout=str(path) + "/\n",
            stderr="User canceled. (-128)"
            if outcome == "cancelled"
            else "not available",
        )

    monkeypatch.setattr(repositories.subprocess, "run", run)
    result = repositories.pick_folder()
    assert result == {
        "native": outcome in {"selected", "cancelled"},
        "path": str(path) if outcome == "selected" else None,
    }
    assert calls[0][0][0] == "/usr/bin/osascript"
    assert "shell" not in calls[0][1]
    assert calls[0][1]["timeout"] == 180


def test_missing_desktop_picker_uses_browser_fallback(monkeypatch):
    monkeypatch.setattr(repositories, "picker_command", lambda: None)
    assert repositories.pick_folder() == {"native": False, "path": None}


async def test_connect_subfolder_deduplicates_and_remembers_recent(context):
    source = context.root / "My project"
    source.mkdir()
    workspaces.git(source, "init", "-q")
    nested = source / "src"
    nested.mkdir()
    first = await context.client.post("/api/projects", json={"path": str(nested)})
    assert first.status_code == 200, first.text
    project = first.json()
    assert project["name"] == "My project" and project["path"] == str(source)
    response = await context.client.post("/api/projects", json={"path": str(source)})
    assert response.json()["id"] == project["id"]
    assert len(context.store.all("projects")) == 1
    opened = await context.client.post(f"/api/projects/{project['id']}/open")
    assert opened.json()["last_opened"] >= project["last_opened"]
    assert (
        Store(context.store.root).get("projects", project["id"])["last_opened"]
        == opened.json()["last_opened"]
    )


async def test_non_git_and_missing_folder_get_clear_errors(context):
    existing = await context.client.post(
        "/api/projects", json={"path": str(context.root)}
    )
    assert (
        existing.status_code == 400
        and "not inside a Git repository" in existing.json()["detail"]
    )
    missing = await context.client.post(
        "/api/projects", json={"path": str(context.root / "missing")}
    )
    assert (
        missing.status_code == 400 and "no longer available" in missing.json()["detail"]
    )


async def test_remove_and_reconnect_preserves_files_tasks_and_identity(context):
    source = context.root / "project"
    source.mkdir()
    workspaces.git(source, "init", "-q")
    (source / "valuable.txt").write_text("keep this edit")
    project = (
        await context.client.post("/api/projects", json={"path": str(source)})
    ).json()
    context.store.put(
        "tasks", {"id": "a" * 32, "project_id": project["id"], "dispatch": "submitted"}
    )
    context.store.save_setting("projects_folder", str(source.parent))
    context.app.state.temporal_ok = False
    for _ in range(2):
        removed = await context.client.delete(f"/api/projects/{project['id']}")
        assert removed.status_code == 200
    home = (await context.client.get("/api/home")).json()
    assert home["projects"] == [] and home["tasks"] == []
    assert (source / "valuable.txt").read_text() == "keep this edit"
    assert context.store.get("tasks", "a" * 32)["project_id"] == project["id"]
    assert (
        await context.client.post(f"/api/projects/{project['id']}/open")
    ).status_code == 409
    suggestion = repositories.discover(context.store)["suggestions"][0]
    assert not suggestion["connected_id"]
    readded = (
        await context.client.post("/api/projects", json={"path": str(source)})
    ).json()
    assert readded["id"] == project["id"] and not readded.get("removed")
    home = (await context.client.get("/api/home")).json()
    assert len(home["projects"]) == len(home["tasks"]) == 1
    assert len(context.store.all("projects")) == 1


async def test_discovery_folder_settings_and_browser_api(context):
    first = repo(context.root / "projects" / "first")
    response = await context.client.post(
        "/api/projects/discovery", json={"path": str(first.parent)}
    )
    assert (
        response.status_code == 200
        and response.json()["suggestions"][0]["name"] == "first"
    )
    again = await context.client.get("/api/projects/discovery")
    assert again.json()["folder"] == str(first.parent)
    listing = await context.client.get(
        "/api/projects/folders", params={"path": str(first.parent)}
    )
    assert listing.json()["folders"][0]["path"] == str(first)
    forgotten = await context.client.delete("/api/projects/discovery")
    assert forgotten.json()["folder"] == "" and first.exists()


async def test_duplicate_concurrent_connections_use_one_catalog_entry(context):
    source = context.root / "source"
    source.mkdir()
    workspaces.git(source, "init", "-q")
    replies = await asyncio.gather(
        *[
            context.client.post("/api/projects", json={"path": str(source)})
            for _ in range(3)
        ]
    )
    assert len({response.json()["id"] for response in replies}) == 1
    assert len(context.store.all("projects")) == 1


async def test_picker_requires_session_and_application_request_header(
    context, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        repositories,
        "pick_folder",
        lambda: calls.append(True) or {"native": True, "path": None},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context.app), base_url="http://localhost"
    ) as external:
        assert (
            await external.post("/api/projects/pick-folder", headers={"X-SDLC": "1"})
        ).status_code == 401
        assert (await external.get("/api/projects/discovery")).status_code == 401
        assert (await external.get("/api/projects/folders")).status_code == 401
    assert (
        await context.client.post("/api/projects/pick-folder", headers={"X-SDLC": ""})
    ).status_code == 403
    assert (
        await context.client.post(
            "/api/projects/pick-folder", headers={"Origin": "https://other.example"}
        )
    ).status_code == 403
    assert not calls
    assert (await context.client.post("/api/projects/pick-folder")).json()[
        "path"
    ] is None
    assert calls == [True]


async def test_only_one_native_picker_can_be_open(context, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def picker():
        entered.set()
        assert release.wait(5)
        return {"native": True, "path": None}

    monkeypatch.setattr(repositories, "pick_folder", picker)
    first = asyncio.create_task(context.client.post("/api/projects/pick-folder"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = await context.client.post("/api/projects/pick-folder")
        assert second.status_code == 409
    finally:
        release.set()
        assert (await first).status_code == 200
