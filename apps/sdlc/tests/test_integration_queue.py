from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_project_workspaces import seed
from test_sandbox import remote as remote

from sdlc_builder import integration_github as github
from sdlc_builder import integration_queue as queue
from sdlc_builder import workspaces as ws
from sdlc_builder.coding_workspace import revision
from sdlc_builder.store import Store, workspace

PROJECT = "shared-project"
ACTOR = {"id": "alice", "name": "Alice"}
PROFILE = {"id": "test", "label": "Test", "provider": "demo", "max_steps": 24}


def accept(identity, version=1, **changes):
    store = Store()
    task = store.get("tasks", identity)
    task.update(created_by=ACTOR, owner=ACTOR, title="Task " + identity[:8])
    store.put("tasks", task)
    value = {
        "status": "accepted",
        "mode": "change",
        "revision": revision(workspace(identity)),
        "verification": "verified",
        "blueprints": [],
    }
    value["review_revision"] = value["revision"]
    value.update(changes)
    store.archive(identity, {"version": version, "value": value})
    return store.get("tasks", identity)


def change(root, path, content):
    ws.write_file(root, path, content, ws.read_file(root, path)["hash"])


def source_tasks():
    first, _ = seed()
    second, _ = seed("b" * 32)
    change(first, "greeting.py", "def greet(name):\n    return f'Hello, {name}!'\n")
    change(second, "new.txt", "second task\n")
    return [accept(first.name), accept(second.name)]


def test_captures_immutable_patches_and_prepares_shared_integration(remote):
    tasks = source_tasks()
    source = Path(Store().get("projects", PROJECT)["path"])
    original_status = ws.git(source, "status", "--porcelain")
    item = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    assert item == queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    change(workspace(tasks[0]["id"]), "greeting.py", "later unaccepted change\n")
    prepared = queue.prepare(PROJECT, item["id"])
    root = workspace(item["id"])
    assert len(remote.created) == 1
    assert prepared["status"] == "testing"
    assert ws.read_file(root, "new.txt")["content"] == "second task\n"
    assert "Hello" in ws.read_file(root, "greeting.py")["content"]
    task = Store().get("tasks", root.name)
    assert task["input"]["integration_run"] and task["dispatch"] == "pending"
    assert task["base_commit"] == prepared["target_commit"]
    assert ws.git(source, "status", "--porcelain") == original_status
    assert queue.prepare(PROJECT, item["id"]) == prepared
    assert queue.input_for(root.name, tasks[0]["id"], "greeting.py")["author"] == ACTOR


def test_changed_unaccepted_and_cross_project_tasks_rejected(remote):
    tasks = source_tasks()
    change(workspace(tasks[0]["id"]), "greeting.py", "new version\n")
    with pytest.raises(ValueError, match="changed after acceptance"):
        queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    with pytest.raises(ValueError, match="source tasks"):
        queue.enqueue("other", "d" * 32, tasks, PROFILE, ACTOR)
    with pytest.raises(ValueError, match="accepted coding"):
        queue.enqueue(
            PROJECT,
            "e" * 32,
            [accept(tasks[1]["id"], 2, status="review")],
            PROFILE,
            ACTOR,
        )
    assert not queue.list_queue(PROJECT)["items"]


def test_conflicts_keep_both_attributed_inputs_and_queue_serializes(remote):
    tasks = source_tasks()
    change(workspace(tasks[1]["id"]), "greeting.py", "conflicting second version\n")
    tasks[1] = accept(tasks[1]["id"], 2)
    first = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    second = queue.enqueue(PROJECT, "d" * 32, tasks, PROFILE, ACTOR)
    with pytest.raises(ValueError, match="earlier integration"):
        queue.prepare(PROJECT, second["id"])
    prepared = queue.prepare(PROJECT, first["id"])
    assert prepared["conflicts"] == [{"task_id": tasks[1]["id"], "path": "greeting.py"}]
    assert (
        "conflicting second version"
        in queue.input_for(first["id"], tasks[1]["id"], "greeting.py")["patch"]
    )
    assert "Hello" in ws.read_file(workspace(first["id"]), "greeting.py")["content"]
    queue.cancel(PROJECT, first["id"], ACTOR)
    assert queue.prepare(PROJECT, second["id"])["status"] == "testing"


def test_concurrent_enqueues_do_not_lose_entries(remote):
    tasks = source_tasks()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda identity: queue.enqueue(
                    PROJECT, identity, tasks, PROFILE, ACTOR
                ),
                ["c" * 32, "d" * 32],
            )
        )
    assert len(queue.list_queue(PROJECT)["items"]) == 2


def test_publication_binds_tested_revision_and_retains_git_history(remote, monkeypatch):
    tasks = source_tasks()
    item = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    prepared = queue.prepare(PROJECT, item["id"])
    task = accept(item["id"])
    project = Store().get("projects", PROJECT)
    project["remote_url"] = "https://github.com/example/project"
    Store().put("projects", project)
    monkeypatch.setattr(
        queue,
        "target",
        lambda _: (prepared["target_branch"], prepared["target_commit"]),
    )
    publish = Mock(side_effect=ValueError("reply lost"))
    monkeypatch.setattr(github, "publish", publish)
    monkeypatch.setattr(github, "reconcile", lambda *a: None)
    expected = task["snapshot"]["value"]["revision"]
    with pytest.raises(ValueError, match="reply lost"):
        queue.publish(PROJECT, item["id"], task, expected, ACTOR)
    pending = queue.find(queue.load(PROJECT), item["id"])
    assert pending["status"] == "publishing"
    commit = pending["publication"]["commit"]
    folder = queue.directory(PROJECT) / item["id"] / "publication"
    assert queue.git(folder, "rev-parse", commit + "^") == prepared["target_commit"]
    assert queue.git(folder, "show", commit + ":new.txt") == "second task"
    assert "Hello" in queue.git(folder, "show", commit + ":greeting.py")
    monkeypatch.setattr(
        queue,
        "publication_candidate",
        lambda *a: pytest.fail("recreated publication commit"),
    )
    publish.side_effect = None
    publish.return_value = {
        "number": 7,
        "url": "https://github.com/example/project/pull/7",
        "commit": commit,
    }
    result = queue.publish(PROJECT, item["id"], task, expected, ACTOR)
    assert result["status"] == "published" and result["publication"]["commit"] == commit
    assert queue.publish(PROJECT, item["id"], task, expected, ACTOR) == result
    assert publish.call_count == 2


def test_stale_target_rebuild_and_failed_checks_block_publication(remote, monkeypatch):
    tasks = source_tasks()
    item = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    queue.prepare(PROJECT, item["id"])
    task = accept(item["id"], verification="incomplete")
    project = Store().get("projects", PROJECT)
    Store().put(
        "projects", {**project, "remote_url": "https://github.com/example/project"}
    )
    expected = task["snapshot"]["value"]["revision"]
    monkeypatch.setattr(
        github, "publish", lambda *a: pytest.fail("published stale code")
    )
    with pytest.raises(ValueError, match="verified integration"):
        queue.publish(PROJECT, item["id"], task, expected, ACTOR)
    task = accept(item["id"], 2)
    monkeypatch.setattr(queue, "target", lambda _: ("main", "f" * 40))
    with pytest.raises(ValueError, match="target branch moved"):
        queue.publish(PROJECT, item["id"], task, expected, ACTOR)
    assert queue.find(queue.load(PROJECT), item["id"])["status"] == "stale"
    rebuilt = queue.rebuild(PROJECT, item["id"], "d" * 32, ACTOR)
    assert rebuilt["status"] == "queued" and "target_commit" not in rebuilt
    assert rebuilt["tasks"] == item["tasks"]
    assert queue.find(queue.load(PROJECT), item["id"])["status"] == "superseded"


def test_forged_patch_paths_never_reach_host_checkout(remote, monkeypatch):
    tasks = source_tasks()
    item = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    stored = queue.load(PROJECT)
    stored["items"][0]["inputs"][0]["entries"][0][0] = "innocent.txt"
    queue.save(PROJECT, stored)
    with pytest.raises(ValueError, match="unsupported patch"):
        queue.prepare(PROJECT, item["id"])
    assert len(remote.created) == 1


def test_github_reconciles_lost_reply_without_duplicate_push_or_pr(
    monkeypatch, tmp_path
):
    project = {"remote_url": "https://github.com/example/project"}
    commit = "e" * 40
    item = {
        "branch": "boltzmann/integration-abc",
        "target_branch": "main",
        "publication": {"commit": commit},
    }
    git = Mock(return_value=commit + "\trefs/heads/boltzmann/integration-abc")
    monkeypatch.setattr(github.remote_repositories, "git", git)
    request = Mock(
        return_value=[
            {
                "head": {"sha": commit},
                "base": {"ref": "main"},
                "number": 1,
                "html_url": "https://github.com/example/project/pull/1",
                "state": "open",
            }
        ]
    )
    monkeypatch.setattr(github, "request", request)
    assert github.publish(project, item, tmp_path)["number"] == 1
    assert git.call_count == 1 and request.call_count == 1
    assert request.call_args.args[1] == "GET"
    git.return_value = "f" * 40 + "\trefs/heads/boltzmann/integration-abc"
    with pytest.raises(ValueError, match="not overwritten"):
        github.publish(project, item, tmp_path)


def test_existing_pull_request_receipt_survives_target_move(remote, monkeypatch):
    tasks = source_tasks()
    item = queue.enqueue(PROJECT, "c" * 32, tasks, PROFILE, ACTOR)
    queue.prepare(PROJECT, item["id"])
    task = accept(item["id"])
    project = Store().get("projects", PROJECT)
    Store().put(
        "projects", {**project, "remote_url": "https://github.com/example/project"}
    )
    saved = queue.load(PROJECT)
    saved["items"][0].update(status="publishing", publication={"commit": "e" * 40})
    queue.save(PROJECT, saved)
    receipt = {
        "number": 7,
        "url": "https://github.com/example/project/pull/7",
        "commit": "e" * 40,
    }
    monkeypatch.setattr(github, "reconcile", lambda *a: receipt)
    monkeypatch.setattr(
        queue,
        "target",
        lambda *a: pytest.fail("revalidated an already published receipt"),
    )
    result = queue.publish(
        PROJECT, item["id"], task, task["snapshot"]["value"]["revision"], ACTOR
    )
    assert result["status"] == "published" and result["pull_request"] == receipt


def test_ci_failure_is_not_hidden_by_successful_commit_status(monkeypatch):
    project = {"remote_url": "https://github.com/example/project"}
    item = {
        "publication": {"commit": "e" * 40},
        "target_branch": "main",
        "target_commit": "f" * 40,
        "branch": "integration",
    }
    monkeypatch.setattr(
        github,
        "request",
        Mock(
            side_effect=[
                {"state": "success", "total_count": 1},
                {
                    "check_runs": [
                        {
                            "name": "Build",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ],
                    "total_count": 1,
                },
                [{"state": "open", "head": {"sha": "e" * 40}}],
                {"object": {"sha": "f" * 40}},
            ]
        ),
    )
    result = github.feedback(project, item)
    assert (
        result["state"] == "failure"
        and result["head_current"]
        and result["target_current"]
    )
