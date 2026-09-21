import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_sandbox import remote as remote

from sdlc_builder import coordination, sandbox
from sdlc_builder import project_workspaces as projects
from sdlc_builder import workspaces as ws
from sdlc_builder.coding_models import FileChange, ToolCall
from sdlc_builder.coding_workspace import coding_operation, revision
from sdlc_builder.store import Store, workspace


def seed(identity="a" * 32, project_id="shared-project"):
    source = ws.demo_project()
    store = Store()
    store.put("projects", {"id": project_id, "path": str(source), "name": "Shared"})
    store.put(
        "tasks",
        {
            "id": identity,
            "project_id": project_id,
            "title": identity,
            "workspace_layout": "project-worktree",
            "engine": "v2",
        },
    )
    prepared = ws.prepare_workspace(str(source), identity)
    store.put("tasks", {**store.get("tasks", identity), **prepared})
    return workspace(identity), prepared


def actual(remote, root):
    meta = projects.task_meta(root)
    return remote.clients[meta["sandbox_id"]].translate(meta["remote_root"])


def test_shared_vm_independent_git_worktrees_and_ports(remote):
    first, a = seed()
    second, b = seed("b" * 32)
    assert len(remote.created) == 1
    assert a["sandbox_id"] == b["sandbox_id"]
    assert a["worktree_branch"] != b["worktree_branch"]
    assert a["preview_port"] != b["preview_port"]
    assert ws.runtime.git(
        actual(remote, first), "rev-parse", "--git-common-dir"
    ) == ws.runtime.git(actual(remote, second), "rev-parse", "--git-common-dir")
    old = ws.read_file(first, "greeting.py")
    ws.write_file(first, "greeting.py", "first task only\n", old["hash"])
    assert ws.read_file(second, "greeting.py")["content"] == old["content"]
    with pytest.raises(ValueError, match="another task"):
        projects.reserve_port(second, a["preview_port"])
    with pytest.raises(ValueError, match="application port"):
        projects.reserve_port(second, 80)
    assert projects.checkpoint_info(first.name)["revision"] == revision(first)
    assert projects.checkpoint_info(first.name)["changed_paths"] == ["greeting.py"]
    previous = projects.checkpoint_info(first.name)["generation"]
    projects.checkpoint(first)
    assert projects.checkpoint_info(first.name)["generation"] == previous
    sandbox.release(first, remove=True)
    assert not actual(remote, second).joinpath(".git").is_dir()  # worktree gitfile
    assert ws.read_file(second, "greeting.py")["content"] == old["content"]
    assert remote.killed == remote.paused == []
    assert not sandbox.descriptor(first).exists()


def test_parallel_preparation_creates_one_vm(remote):
    source = ws.demo_project()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda identity: projects.prepare("project", str(source), identity),
                ["c" * 32, "d" * 32],
            )
        )
    assert len(remote.created) == 1
    assert results[0]["sandbox_id"] == results[1]["sandbox_id"]


def test_recovery_preserves_exact_sources_and_other_tasks(remote):
    root, prepared = seed()
    other, _ = seed("b" * 32)
    tree = actual(remote, root)
    (tree / "greeting.py").unlink()
    (tree / "script.sh").write_bytes(b"#!/bin/sh\necho changed\n")
    (tree / "script.sh").chmod(0o751)
    (tree / "asset.bin").write_bytes(b"\x00\xff\x01")
    (tree / ".env").write_text("SECRET=do-not-checkpoint")
    (tree / "node_modules").mkdir()
    (tree / "node_modules/generated").write_text("generated")
    projects.checkpoint(root)
    expected, other_expected = revision(root), revision(other)
    sandbox._sdk().kill(prepared["sandbox_id"])
    recovered = projects.recover("shared-project")
    assert len(recovered["tasks"]) == 2 and len(remote.created) == 2
    with pytest.raises(ValueError, match="being recovered"):
        revision(root)
    # Retry finalization does not create or kill another VM.
    assert projects.recover("shared-project") == recovered
    assert len(remote.created) == 2
    projects.finish_recovery("shared-project")
    assert revision(root) == expected and revision(other) == other_expected
    tree = actual(remote, root)
    assert not (tree / "greeting.py").exists()
    assert (tree / "asset.bin").read_bytes() == b"\x00\xff\x01"
    assert (tree / "script.sh").stat().st_mode & 0o777 == 0o751
    assert not (tree / ".env").exists() and not (tree / "node_modules").exists()
    assert set(ws.changed_paths(root, prepared["base_commit"])) == {
        "asset.bin",
        "script.sh",
        "greeting.py",
    }


def test_bad_checkpoint_never_kills_current_vm(remote):
    root, _ = seed()
    info = projects.checkpoint_info(root.name)
    (projects.artifacts(root.name) / info["archive"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        projects.recover("shared-project")
    assert remote.killed == [] and len(remote.created) == 1


async def test_writer_lease_blocks_competing_agent_and_releases_after_failure(remote):
    root, _ = seed()
    with projects.writer_lease(root.name, "developer"):
        result = await coding_operation(root.name, "competing", ToolCall(kind="list"))
        assert not result.ok and "active writer" in result.error
    assert (await coding_operation(root.name, "after", ToolCall(kind="list"))).ok
    with pytest.raises(RuntimeError):
        with projects.writer_lease(root.name, "failed-operation"):
            raise RuntimeError("interrupted")
    with projects.writer_lease(root.name, "new-owner"):
        pass


async def test_simultaneous_agent_edits_are_isolated_and_checkpointed(remote):
    first, _ = seed()
    second, _ = seed("b" * 32)
    before = ws.read_file(first, "greeting.py")["hash"]
    results = await asyncio.gather(
        *[
            coding_operation(
                root.name,
                "edit-1",
                ToolCall(
                    kind="patch",
                    changes=[
                        FileChange(
                            path="greeting.py", expected_hash=before, content=root.name
                        )
                    ],
                ),
            )
            for root in (first, second)
        ]
    )
    assert all(result.ok for result in results)
    for root in (first, second):
        assert ws.read_file(root, "greeting.py")["content"] == root.name
        assert projects.checkpoint_info(root.name)["revision"] == revision(root)
    board = coordination.board(first.name)
    assert board["tasks"][0]["overlap"] == ["greeting.py"]
    coordination.post(
        first.name,
        second.name,
        "I am changing the greeting signature",
        "note-1",
        {"name": "Agent"},
    )
    coordination.post(
        first.name,
        second.name,
        "I am changing the greeting signature",
        "note-1",
        {"name": "Agent"},
    )
    assert len(coordination.board(second.name)["notes"]) == 1
    third, _ = seed("c" * 32, "private-project")
    with pytest.raises(ValueError, match="this project"):
        coordination.post(first.name, third.name, "invalid", "note-2", {})
    assert all(t["id"] != third.name for t in board["tasks"])


def test_unconfirmed_creation_never_retries_automatically(remote, monkeypatch):
    create = sandbox._sdk().create

    def lost(**kwargs):
        create(**kwargs)
        raise TimeoutError("create response lost")

    monkeypatch.setattr(sandbox._sdk(), "create", lost)
    with pytest.raises(TimeoutError):
        seed()
    monkeypatch.setattr(sandbox._sdk(), "create", create)
    with pytest.raises(ValueError, match="unconfirmed"):
        projects.prepare("shared-project", str(ws.demo_project()), "a" * 32)
    assert len(remote.created) == 1


def test_interrupted_restore_retries_only_its_unfinished_worktree(
    remote, monkeypatch, tmp_path
):
    from sdlc_builder import workspace_runtime as runtime

    root, prepared = seed()
    old = ws.read_file(root, "greeting.py")
    ws.write_file(root, "greeting.py", "restored content", old["hash"])
    checkpoint = projects.checkpoint_info(root.name)
    checkpoint = {
        **checkpoint,
        **{
            field: str(projects.artifacts(root.name) / checkpoint[field])
            for field in ("archive", "bundle")
        },
    }
    destination = tmp_path / "replacement" / root.name
    copy = runtime.shutil.copyfileobj
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("interrupted during restore")
        return copy(*args, **kwargs)

    monkeypatch.setattr(runtime.shutil, "copyfileobj", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        runtime.project_restore(destination, checkpoint, prepared["base_commit"])
    monkeypatch.setattr(runtime.shutil, "copyfileobj", copy)
    runtime.project_restore(destination, checkpoint, prepared["base_commit"])
    assert runtime.revision(destination) == checkpoint["revision"]
    (destination / "greeting.py").write_text("subsequent user edit")
    with pytest.raises(ValueError, match="preserved"):
        runtime.project_restore(destination, checkpoint, prepared["base_commit"])
    assert (destination / "greeting.py").read_text() == "subsequent user edit"


def test_live_remote_writer_is_not_displaced_by_new_host_operation(remote):
    from sdlc_builder import workspace_runtime as runtime

    root, _ = seed()
    with runtime.workspace_lock(actual(remote, root)):
        with pytest.raises(ValueError, match="in-flight"):
            ws.write_file(root, "new.txt", "must not write", "")
    assert not actual(remote, root).joinpath("new.txt").exists()
    ws.write_file(root, "new.txt", "safe after release", "")


def test_lost_preparation_reply_reuses_receipt_and_current_files(remote, monkeypatch):
    original = projects.remote_call

    def lost(client, meta, operation, *args):
        result = original(client, meta, operation, *args)
        if operation == "project_prepare":
            raise TimeoutError("initialization result lost")
        return result

    monkeypatch.setattr(projects, "remote_call", lost)
    with pytest.raises(TimeoutError):
        seed()
    root = workspace("a" * 32)
    actual(remote, root).joinpath("greeting.py").write_text("preserve existing changes")
    monkeypatch.setattr(projects, "remote_call", original)
    projects.prepare("shared-project", str(ws.demo_project()), root.name)
    assert len(remote.created) == 1
    assert ws.read_file(root, "greeting.py")["content"] == "preserve existing changes"
    assert projects.checkpoint_info(root.name)["revision"] == revision(root)


async def test_agent_coordination_notes_are_idempotent_and_visible_in_file_listing(
    remote,
):
    first, _ = seed()
    second, _ = seed("b" * 32)
    request = ToolCall(
        kind="coordinate",
        path=second.name,
        query="Please keep the public interface compatible",
    )
    result = await coding_operation(first.name, "coordinate-1", request)
    assert result.ok
    assert await coding_operation(first.name, "coordinate-1", request) == result
    incoming = await coding_operation(second.name, "list-1", ToolCall(kind="list"))
    value = json.loads(incoming.output)
    assert value["notes"][0]["author"]["kind"] == "agent"
    assert len(value["notes"]) == 1


def test_shared_fork_keeps_parent_patch_without_another_vm(remote):
    root, _ = seed()
    original = ws.read_file(root, "greeting.py")
    ws.write_file(root, "greeting.py", "parent change", original["hash"])
    second = "d" * 32
    store = Store()
    store.put(
        "tasks",
        {
            "id": second,
            "title": "Fork",
            "project_id": "shared-project",
            "workspace_layout": "project-worktree",
        },
    )
    result = ws.continue_workspace(root.name, second)
    assert len(remote.created) == 1
    assert result["parent_task_id"] == root.name
    assert ws.read_file(workspace(second), "greeting.py")["content"] == "parent change"
    assert not result["source_dirty"]
