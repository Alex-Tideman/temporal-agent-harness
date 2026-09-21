"""Workspace routing, lifecycle and guarded delivery into the source checkout."""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from . import workspace_runtime as runtime
from .sandbox import remote_capable
from .store import Store, data_dir, workspace
from .workspace_runtime import MAX_FILE as MAX_FILE
from .workspace_runtime import allowed as allowed
from .workspace_runtime import git_apply, safe_path

git = remote_capable(runtime.git)
files = remote_capable(runtime.files)
read_file = remote_capable(runtime.read_file)
write_file = remote_capable(runtime.write_file)
diff_entries = remote_capable(runtime.diff_entries)
changed_paths = remote_capable(runtime.changed_paths)
diff = remote_capable(runtime.diff)
search_files = remote_capable(runtime.search_files)
source_snapshot = remote_capable(runtime.source_snapshot)
base_snapshot = remote_capable(runtime.base_snapshot)
review_snapshots = remote_capable(runtime.review_snapshots)


async def execute_check(root: Path, argv: list[str]) -> dict:
    from .sandbox import call, is_remote

    if is_remote(root):
        return await asyncio.to_thread(call, root, "execute_check", argv)
    return await runtime.execute_check(
        root, argv, data_dir() / "command-homes" / root.name
    )


def prepare_workspace(source: str, task_id: str) -> dict:
    from .sandbox import configuration, prepare

    if configuration()["backend"] == "e2b":
        task = next((t for t in Store().all("tasks") if t["id"] == task_id), {})
        if task.get("workspace_layout") == "project-worktree":
            from . import project_workspaces

            return project_workspaces.prepare(task["project_id"], source, task_id)
        return prepare(source, task_id)
    root = workspace(task_id)
    source_path = Path(source).expanduser().resolve()
    base = git(source_path, "rev-parse", "HEAD").strip()
    dirty = bool(git(source_path, "status", "--porcelain"))
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists():
        raise ValueError("Workspace already exists")
    # Independent clone: no worktree metadata/branch changes in the user's checkout.
    git(
        source_path,
        "clone",
        "--no-hardlinks",
        "--no-checkout",
        "--",
        str(source_path),
        str(root),
    )
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "checkout", "--detach", base)
    git(root, "switch", "-c", f"sdlc/{task_id[:8]}")
    return {
        "workspace": str(root),
        "workspace_backend": "local",
        "base_commit": base,
        "source_dirty": dirty,
    }


def merge_to_project(
    root: Path,
    base_revision: str,
    source: str,
    expected_revision: str = "",
) -> dict:
    """Apply an accepted task into its source checkout without staging or committing."""
    # Imported lazily because coding_workspace uses these workspace primitives.
    from .coding_workspace import revision

    root = root.resolve()
    source_root = Path(source).expanduser().resolve()
    if source_root == root:
        raise ValueError("The task workspace cannot be its own project checkout")
    actual_root = Path(
        git(source_root, "rev-parse", "--show-toplevel").strip()
    ).resolve()
    if actual_root != source_root:
        raise ValueError("The configured project path is not the Git repository root")
    git(source_root, "cat-file", "-e", f"{base_revision}^{{commit}}")

    observed_revision = revision(root)
    if expected_revision and observed_revision != expected_revision:
        raise ValueError(
            "The task workspace changed after acceptance. Review and accept the current revision before merging."
        )
    paths = changed_paths(root, base_revision)
    entries = diff_entries(root, base_revision)
    if revision(root) != observed_revision:
        raise ValueError(
            "The task workspace changed while preparing the merge. Review and accept it again."
        )
    if not paths:
        raise ValueError("There are no task changes to merge into the project")
    supported = {name for name, _ in entries}
    if set(paths) != supported:
        raise ValueError(
            "Some task changes cannot be merged automatically. Inspect large, binary, linked, secret, or excluded files in the task workspace."
        )
    if sum(len(patch) for _, patch in entries) > 500_000:
        raise ValueError(
            "The task patch exceeds the 500 KB merge limit. Use Git in the task workspace."
        )
    for name in paths:
        safe_path(source_root, name)

    # Remote outputs are untrusted. Validate each patch's actual paths on the
    # host before git apply can mutate the source checkout.
    validate_patch_entries(source_root, entries)

    pending = []
    already_present = []
    for name, patch in entries:
        if git_apply(source_root, patch, "--check").returncode == 0:
            pending.append((name, patch))
        elif git_apply(source_root, patch, "--check", "--reverse").returncode == 0:
            already_present.append(name)
        else:
            raise ValueError(
                "The project has overlapping changes in files from this task. Nothing was changed. Resolve those edits in the project, then try again."
            )

    combined = "".join(patch for _, patch in pending)
    if combined:
        # Check the combined patch too, then let git apply perform its own atomic
        # preflight immediately before writing the working tree.
        if git_apply(source_root, combined, "--check").returncode:
            raise ValueError(
                "The project changed while preparing the merge. Nothing was changed. Try again after reviewing the project checkout."
            )
        applied = git_apply(source_root, combined)
        if applied.returncode:
            raise ValueError(
                "The project changed while the merge was starting. Git did not complete the merge; inspect the project checkout before retrying."
            )

    return {
        "ok": True,
        "revision": observed_revision,
        "files": paths,
        "applied_files": [name for name, _ in pending],
        "already_present_files": already_present,
        "already_applied": not pending,
        "project_path": str(source_root),
        "branch": git(source_root, "branch", "--show-current").strip()
        or "detached HEAD",
    }


def validate_patch_entries(source_root: Path, entries):
    """Validate sandbox-supplied paths before any host Git mutation."""
    for name, patch in entries:
        safe_path(source_root, name)
        inspected = git_apply(source_root, patch, "--numstat", "-z")
        records = [
            record.split("\t", 2) for record in inspected.stdout.split("\0") if record
        ]
        if (
            inspected.returncode
            or len(records) != 1
            or len(records[0]) != 3
            or records[0][2] != name
            or not all(n.isdigit() for n in records[0][:2])
            or any(
                line in {"new file mode 120000", "new mode 120000"}
                for line in patch.splitlines()
            )
        ):
            raise ValueError(
                "The task returned an unsupported patch. Nothing was changed."
            )


def continue_workspace(parent_id: str, task_id: str) -> dict:
    from .sandbox import configuration, is_remote, prepare

    parent = workspace(parent_id)
    if configuration()["backend"] == "e2b" or is_remote(parent):
        task = next((t for t in Store().all("tasks") if t["id"] == task_id), {})
        if task.get("workspace_layout") == "project-worktree":
            from . import project_workspaces

            project = Store().get("projects", task["project_id"])
            return project_workspaces.prepare(
                task["project_id"], project["path"], task_id, parent_id
            )
        return prepare("", task_id, parent_id=parent_id)
    patch = diff(parent)
    if patch["truncated"]:
        raise ValueError(
            "The prior diff exceeds the continuation limit. Commit changes in its workspace first."
        )
    prepared = prepare_workspace(str(parent), task_id)
    if patch["patch"]:
        result = subprocess.run(
            ["git", "apply", "--"],
            cwd=workspace(task_id),
            input=patch["patch"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode:
            raise ValueError(
                "Could not carry forward the previous patch; the original workspace is preserved"
            )
    prepared["source_dirty"] = False
    prepared["parent_task_id"] = parent_id
    return prepared


async def perform(task_id: str, operation_id: str, action: dict) -> dict:
    """Stable operation IDs prevent retrying a write or an uncertain command."""
    root = workspace(task_id)
    journal = data_dir() / "operations" / task_id
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not operation_id.replace("-", "").isalnum():
        raise ValueError("Invalid operation ID")
    record = journal / f"{operation_id}.json"
    fingerprint = hashlib.sha256(
        json.dumps(action, sort_keys=True).encode()
    ).hexdigest()
    if record.exists():
        previous = json.loads(record.read_text())
        if previous["fingerprint"] != fingerprint:
            raise ValueError("Operation ID was reused with different arguments")
        if "result" in previous:
            return previous["result"]
        if action["kind"] == "check":
            return {
                "error": "Command outcome is unknown after interruption; it was not run twice. Request a new check."
            }
    record.write_text(json.dumps({"fingerprint": fingerprint}))
    try:
        kind = action["kind"]
        if kind == "list":
            result = {"files": await asyncio.to_thread(files, root)}
        elif kind == "read":
            result = await asyncio.to_thread(read_file, root, action["path"])
        elif kind == "search":
            result = {
                "matches": await asyncio.to_thread(search_files, root, action["query"])
            }
        elif kind == "write":
            result = await asyncio.to_thread(
                write_file,
                root,
                action["path"],
                action["content"],
                action["expected_hash"],
            )
        elif kind == "check":
            result = await execute_check(root, action["argv"])
        else:
            raise ValueError("Unsupported workspace action")
    except (ValueError, OSError, UnicodeError) as error:
        result = {"error": str(error)[:1000]}
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps({"fingerprint": fingerprint, "result": result}))
    temporary.replace(record)
    return result


def demo_project() -> Path:
    root = data_dir() / "demo-repository"
    if not root.exists():
        root.mkdir(parents=True)
        (root / "greeting.py").write_text(
            'def greet(name):\n    return f"hello {name}"\n'
        )
        (root / "test_greeting.py").write_text(
            'import unittest\nfrom greeting import greet\n\nclass GreetingTest(unittest.TestCase):\n    def test_greeting(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada!")\n'
        )
        (root / "README.md").write_text(
            "# Greeting workshop\n\nFix greeting.py so the unittest passes.\nRun: python3 -m unittest -v\n"
        )
        git(root, "init", "-b", "main")
        git(root, "add", ".")
        git(
            root,
            "-c",
            "user.name=SDLC Demo",
            "-c",
            "user.email=demo@localhost",
            "commit",
            "-m",
            "Add greeting exercise",
        )
    return root
