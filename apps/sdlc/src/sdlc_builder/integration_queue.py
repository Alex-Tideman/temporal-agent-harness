"""Immutable integration inputs, serialized preparation and publication receipts.

The existing CodingWorkflow owns repair, verification, preview and review. This
module owns the queue and Git delivery; repository commands only execute in E2B.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import project_workspaces, remote_repositories, sandbox
from . import workspaces as ws
from .models import Profile, TaskInput
from .store import Store, data_dir, workspace

TERMINAL = {"published", "cancelled", "superseded"}


def directory(project_id: str) -> Path:
    return data_dir() / "integrations" / hashlib.sha256(project_id.encode()).hexdigest()


@contextmanager
def locked(project_id: str):
    root = directory(project_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "queue.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def load(project_id: str) -> dict:
    path = directory(project_id) / "queue.json"
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"project_id": project_id, "items": []}
    )


def save(project_id: str, queue: dict):
    project_workspaces.save(directory(project_id) / "queue.json", queue)


def find(queue: dict, identity: str) -> dict:
    for item in queue["items"]:
        if item["id"] == identity:
            return item
    raise ValueError("Integration request not found")


def public(item: dict) -> dict:
    return {
        k: v for k, v in item.items() if k not in {"inputs", "seed_patch", "profile"}
    }


def list_queue(project_id: str) -> dict:
    return {"items": [public(item) for item in load(project_id)["items"]]}


def capture(task: dict, expected: str) -> list[list[str]]:
    from .coding_workspace import revision

    root = workspace(task["id"])
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or revision(root) != expected:
        raise ValueError("Task changed after acceptance. Review and accept it again.")
    entries = ws.diff_entries(root, task["base_commit"])
    if set(ws.changed_paths(root, task["base_commit"])) != {p for p, _ in entries}:
        raise ValueError(
            "Task contains unsupported changes. Export those files before integration."
        )
    if not entries or sum(len(p.encode()) for _, p in entries) > 500_000:
        raise ValueError(
            "Integration requires a nonempty text patch under 500 KB per task"
        )
    if revision(root) != expected:
        raise ValueError("Task changed while capturing its accepted revision")
    return [[name, patch] for name, patch in entries]


def enqueue(
    project_id: str, identity: str, tasks: list[dict], profile: dict, actor: dict
) -> dict:
    workspace(identity)
    if not tasks or len(tasks) > 20 or len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("Select between one and twenty different accepted tasks")
    request = [
        {
            "task_id": t["id"],
            "revision": (t.get("snapshot") or {}).get("value", {}).get("revision"),
        }
        for t in tasks
    ]
    with locked(project_id):
        queue = load(project_id)
        existing = next(
            (item for item in queue["items"] if item["id"] == identity), None
        )
        if existing:
            if (
                existing["tasks"] != request
                or existing["profile"]["id"] != profile["id"]
            ):
                raise ValueError("Integration ID was reused with different inputs")
            return public(existing)
        if any(t["id"] == identity for t in Store().all("tasks")):
            raise ValueError("Integration ID is already used by a task")
        inputs = []
        for task, version in zip(tasks, request):
            state = (task.get("snapshot") or {}).get("value", {})
            if task.get("project_id") != project_id or task.get("integration_id"):
                raise ValueError(
                    "Choose source tasks in this project, not another integration"
                )
            if (
                task.get("engine") != "v2"
                or state.get("status") != "accepted"
                or state.get("mode") != "change"
            ):
                raise ValueError("Only accepted coding tasks can enter integration")
            with project_workspaces.writer_lease(task["id"], "integration:" + identity):
                entries = capture(task, version["revision"])
            inputs.append(
                {
                    **version,
                    "title": task["title"],
                    "author": task.get("owner") or task.get("created_by"),
                    "requirements": [
                        r
                        for b in state.get("blueprints", [])
                        if b.get("approved")
                        for r in b.get("requirements", [])
                    ],
                    "entries": entries,
                }
            )
        if sum(len(p.encode()) for t in inputs for _, p in t["entries"]) > 1_000_000:
            raise ValueError(
                "Combined integration inputs exceed 1 MB. Use a smaller batch."
            )
        item = {
            "id": identity,
            "project_id": project_id,
            "tasks": request,
            "inputs": inputs,
            "profile": profile,
            "created_by": actor,
            "created_at": time.time(),
            "status": "queued",
            "error": "",
            "conflicts": [],
            "branch": "boltzmann/integration-" + identity,
        }
        queue["items"].append(item)
        save(project_id, queue)
        return public(item)


def target(project: dict) -> tuple[str, str]:
    # Share the source-checkout lock with sync and legacy merge. FETCH_HEAD is
    # mutable; its read must stay paired with our own fetch.
    root = Path(project["path"]).resolve()
    lock_root = data_dir() / "project-merge-locks"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (lock_root / (hashlib.sha256(str(root).encode()).hexdigest() + ".lock")).open(
        "a"
    ) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return _target(project)


def _target(project: dict) -> tuple[str, str]:
    source = Path(project["path"])
    branch = (
        project.get("default_branch")
        or ws.runtime.git(source, "branch", "--show-current").strip()
    )
    if (
        not branch
        or branch.startswith("-")
        or ws.runtime.git(source, "check-ref-format", "--branch", branch).strip()
        != branch
    ):
        raise ValueError("Select a project with a valid target branch")
    if project.get("remote_url"):
        url = remote_repositories.canonical_url(project["remote_url"])
        remote_repositories.git(
            source, "fetch", "--no-tags", "--", url + ".git", "refs/heads/" + branch
        )
        head = ws.runtime.git(source, "rev-parse", "FETCH_HEAD").strip()
    else:
        head = ws.runtime.git(source, "rev-parse", "refs/heads/" + branch).strip()
    return branch, head


def prepare(project_id: str, identity: str) -> dict:
    with locked(project_id):
        queue = load(project_id)
        item = find(queue, identity)
        if item["status"] in TERMINAL or item["status"] in {
            "testing",
            "publishing",
            "stale",
        }:
            return public(item)
        first = next(i for i in queue["items"] if i["status"] not in TERMINAL)
        if first["id"] != identity:
            raise ValueError(
                "Finish or cancel the earlier integration before preparing this batch"
            )
        store = Store()
        project = store.get("projects", project_id)
        folder = directory(project_id) / identity
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if "target_commit" not in item:
                branch, head = target(project)
                archive = sandbox.source_archive(Path(project["path"]), head)
                # This sterile checkout only runs Git primitives, never repo code.
                with tempfile.TemporaryDirectory(dir=folder) as temporary:
                    snapshot = Path(temporary) / "base.tar.gz"
                    snapshot.write_bytes(archive)
                    root = Path(temporary) / "tree"
                    base = ws.runtime.initialize(root, str(snapshot))
                    conflicts = []
                    for entry in item["inputs"]:
                        ws.validate_patch_entries(root, entry["entries"])
                        for name, patch in entry["entries"]:
                            if ws.git_apply(root, patch, "--check").returncode == 0:
                                if ws.git_apply(root, patch).returncode:
                                    raise ValueError(
                                        "Integration patch could not be applied"
                                    )
                            elif ws.git_apply(
                                root, patch, "--check", "--reverse"
                            ).returncode:
                                conflicts.append(
                                    {"task_id": entry["task_id"], "path": name}
                                )
                    delta = ws.runtime.diff(root, base)
                    if delta["truncated"]:
                        raise ValueError(
                            "Combined patch exceeds 500 KB. Use a smaller batch."
                        )
                (folder / "base.tar.gz").write_bytes(archive)
                item.update(
                    target_branch=branch,
                    target_commit=head,
                    seed_patch=delta["patch"],
                    conflicts=conflicts,
                    status="preparing",
                )
                save(project_id, queue)
            try:
                task = store.get("tasks", identity)
            except ValueError:
                summary = [
                    {k: v for k, v in t.items() if k != "entries"}
                    for t in item["inputs"]
                ]
                prompt = (
                    "Integrate these accepted tasks against the captured target branch. Preserve every requirement across tasks. Use read_integration_input to read immutable source patches and conflicts. Repair conflicts in this worktree only. Install dependencies, run combined regression/build checks, prepare and smoke-test a web preview when applicable, and independently review the entire combined diff. All repairs require renewed human review of the final code before publication. Do not publish or merge to the target branch.\n"
                    + json.dumps({"sources": summary, "conflicts": item["conflicts"]})
                )[:20000]
                task_input = TaskInput(
                    task_id=identity,
                    prompt=prompt,
                    profile=Profile.model_validate(item["profile"]),
                    project_coordination=True,
                    integration_run=True,
                )
                task = {
                    "id": identity,
                    "integration_id": identity,
                    "workflow_id": "sdlc-" + identity,
                    "engine": "v2",
                    "project_id": project_id,
                    "project_name": project["name"],
                    "owner": item["created_by"],
                    "created_by": item["created_by"],
                    "ownership_version": 1,
                    "workspace_layout": "project-worktree",
                    "workspace_backend": "e2b",
                    "title": f"Integrate {len(item['tasks'])} accepted tasks",
                    "mode": "change",
                    "profile_id": item["profile"]["id"],
                    "created_at": time.time(),
                    "dispatch": "preparing",
                    "input": task_input.model_dump(),
                }
                store.put("tasks", task)
            if task["dispatch"] == "preparing":
                prepared = project_workspaces.prepare(
                    project_id,
                    project["path"],
                    identity,
                    seed={
                        "base": item["target_commit"],
                        "archive": (folder / "base.tar.gz").read_bytes(),
                        "patch": item["seed_patch"],
                    },
                )
                task.update(prepared, dispatch="pending")
                task.pop("preparation_error", None)
                store.put("tasks", task)
            item.update(status="testing", task_id=identity, error="")
            save(project_id, queue)
        except Exception as error:
            item["error"] = str(error)[:1000]
            save(project_id, queue)
            try:
                task = store.get("tasks", identity)
                if task["dispatch"] == "preparing":
                    task["preparation_error"] = item["error"]
                    store.put("tasks", task)
            except ValueError:
                pass
            raise
        return public(item)


def input_for(task_id: str, source_id: str = "", path: str = "") -> dict:
    task = Store().get("tasks", task_id)
    if not task.get("integration_id"):
        raise ValueError("This task is not an integration run")
    item = find(load(task["project_id"]), task["integration_id"])
    if not source_id:
        return {
            "sources": [
                {k: v for k, v in t.items() if k != "entries"}
                | {"files": [p for p, _ in t["entries"]]}
                for t in item["inputs"]
            ],
            "conflicts": item["conflicts"],
        }
    for source in item["inputs"]:
        if source["task_id"] == source_id:
            for name, patch in source["entries"]:
                if name == path:
                    return {
                        "task_id": source_id,
                        "author": source["author"],
                        "revision": source["revision"],
                        "path": path,
                        "patch": patch,
                    }
    raise ValueError("Accepted source file not found in this integration")


def cancel(project_id: str, identity: str, actor: dict) -> dict:
    with locked(project_id):
        queue = load(project_id)
        item = find(queue, identity)
        if item.get("publication") or item["status"] == "publishing":
            raise ValueError("Reconcile publication before cancelling this integration")
        item.update(status="cancelled", cancelled_by=actor)
        save(project_id, queue)
        return public(item)


def rebuild(project_id: str, identity: str, new_id: str, actor: dict) -> dict:
    workspace(new_id)
    with locked(project_id):
        queue = load(project_id)
        original = find(queue, identity)
        previous = next((i for i in queue["items"] if i["id"] == new_id), None)
        if previous:
            if previous.get("replaces") != identity:
                raise ValueError("Rebuild ID was reused")
            return public(previous)
        if original.get("publication"):
            from . import integration_github

            if original["status"] != "stale" or integration_github.reconcile(
                Store().get("projects", project_id), original
            ):
                raise ValueError("Reconcile the existing publication before rebuilding")
        if original["status"] in TERMINAL:
            raise ValueError("This integration is no longer active")
        if any(t["id"] == new_id for t in Store().all("tasks")):
            raise ValueError("Rebuild ID is already used")
        item = {k: original[k] for k in ("project_id", "tasks", "inputs", "profile")}
        item.update(
            id=new_id,
            created_by=actor,
            created_at=time.time(),
            status="queued",
            conflicts=[],
            error="",
            branch="boltzmann/integration-" + new_id,
            replaces=identity,
        )
        original["status"] = "superseded"
        queue["items"].insert(queue["items"].index(original) + 1, item)
        save(project_id, queue)
        return public(item)


def git(root: Path, *args: str, input: str | None = None) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=root,
        env=env,
        input=input,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("Integration Git operation failed: " + result.stderr[:500])
    return result.stdout.strip()


def publication_candidate(project: dict, item: dict, entries: list[list[str]]) -> str:
    # A private index based on the real target commit retains excluded files and
    # parent history without running checkout filters or repository hooks.
    folder = directory(project["id"]) / item["id"] / "publication"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(folder, "init", "--bare")
    git(
        folder,
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        str(Path(project["path"]).resolve()),
        item["target_commit"],
    )
    git(folder, "read-tree", item["target_commit"])
    git(
        folder,
        "apply",
        "--cached",
        "--whitespace=nowarn",
        "--",
        input="".join(p for _, p in entries),
    )
    tree = git(folder, "write-tree")
    commit = git(
        folder,
        "-c",
        "user.name=boltzmann",
        "-c",
        "user.email=integration@localhost",
        "-c",
        "commit.gpgSign=false",
        "commit-tree",
        tree,
        "-p",
        item["target_commit"],
        "-m",
        "Integrate reviewed tasks "
        + ", ".join(t["task_id"][:8] for t in item["tasks"]),
    )
    git(folder, "update-ref", "refs/heads/" + item["branch"], commit)
    return commit


def publish(
    project_id: str, identity: str, task: dict, expected_revision: str, actor: dict
) -> dict:
    with locked(project_id):
        queue = load(project_id)
        item = find(queue, identity)
        if item["status"] == "published":
            return public(item)
        if item["status"] in TERMINAL:
            raise ValueError("This integration is no longer active")
        project = Store().get("projects", project_id)
        if not project.get("remote_url"):
            raise ValueError(
                "Connect a GitHub repository to publish an integration pull request"
            )
        if item.get("publication"):
            from . import integration_github

            receipt = integration_github.reconcile(project, item)
            if receipt:
                item.update(status="published", pull_request=receipt, error="")
                save(project_id, queue)
                return public(item)
        state = (task.get("snapshot") or {}).get("value", {})
        if (
            task["id"] != item.get("task_id")
            or state.get("status") != "accepted"
            or state.get("verification") != "verified"
            or state.get("review_revision") != expected_revision
            or state.get("revision") != expected_revision
        ):
            raise ValueError(
                "Accept the verified integration revision before publication. Incomplete checks cannot be waived for integration."
            )
        with project_workspaces.writer_lease(identity, "publish:" + actor["id"]):
            entries = capture(task, expected_revision)
            ws.validate_patch_entries(Path(project["path"]), entries)
            branch, head = target(project)
            if head != item["target_commit"] or branch != item["target_branch"]:
                item.update(
                    status="stale",
                    error="The target branch moved. Rebuild this batch against the latest target and review it again.",
                )
                save(project_id, queue)
                raise ValueError(item["error"])
            if not item.get("publication"):
                commit = publication_candidate(project, item, entries)
                item["publication"] = {
                    "revision": expected_revision,
                    "commit": commit,
                    "approved_by": actor,
                    "approved_at": time.time(),
                }
                item["status"] = "publishing"
                save(project_id, queue)
            if item["publication"]["revision"] != expected_revision:
                raise ValueError(
                    "Publication already started for another revision. Reconcile it before continuing."
                )
            try:
                from . import integration_github

                result = integration_github.publish(
                    project, item, directory(project_id) / identity / "publication"
                )
                item.update(status="published", pull_request=result, error="")
            except Exception:
                item.update(
                    status="publishing",
                    error="Publication could not be confirmed. Retry to reconcile the saved branch and pull request.",
                )
                save(project_id, queue)
                raise
            save(project_id, queue)
        return public(item)
