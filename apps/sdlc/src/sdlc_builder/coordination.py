"""Project-scoped coordination notes and advisory overlap detection.

Notes never trigger a workflow or grant access to another worktree. Agents read
the board through their own task's activity, using the same catalogue as the UI.
"""

import fnmatch
import json
import time

from .project_workspaces import checkpoint_info
from .store import Store


def connect(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS coordination_notes (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            author TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL
        )""")


def post(task_id: str, target_id: str, message: str, identity: str, author: dict):
    store = Store()
    task = store.get("tasks", task_id)
    target = store.get("tasks", target_id)
    if task["project_id"] != target["project_id"]:
        raise ValueError("Coordinate only with tasks in this project")
    message = message.strip()
    if not message or len(message) > 4000:
        raise ValueError("Write a coordination note of 1–4,000 characters")
    connect(store)
    with store.connect() as db:
        previous = db.execute(
            "SELECT * FROM coordination_notes WHERE id=?", (identity,)
        ).fetchone()
        if previous:
            if (previous["source_id"], previous["target_id"], previous["message"]) != (
                task_id,
                target_id,
                message,
            ):
                raise ValueError("This note ID was already used for different content")
            return
        db.execute(
            "INSERT INTO coordination_notes VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                task["project_id"],
                task_id,
                target_id,
                json.dumps(author),
                message,
                time.time(),
            ),
        )


def board(task_id: str) -> dict:
    store = Store()
    task = store.get("tasks", task_id)
    connect(store)
    related = []
    for other in store.all("tasks"):
        if other["project_id"] != task["project_id"]:
            continue
        state = (other.get("snapshot") or {}).get("value", {})
        checkpoint = (
            checkpoint_info(other["id"])
            if other.get("workspace_layout") == "project-worktree"
            else {}
        )
        changed = checkpoint.get("changed_paths", state.get("files_changed", []))
        blueprints = state.get("blueprints", [])
        scope = blueprints[-1].get("scope", []) if blueprints else []
        related.append(
            {
                "id": other["id"],
                "title": other["title"],
                "owner": other.get("owner", other.get("created_by")),
                "status": state.get("status", other.get("dispatch", "queued")),
                "changed_files": changed[:200],
                "scope": scope[:100],
                "checkpoint_at": checkpoint.get("saved_at"),
            }
        )
    own = next(t for t in related if t["id"] == task_id)
    for other in related:
        own_paths, other_paths = own["changed_files"], other["changed_files"]
        overlap = set(own_paths) & set(other_paths)
        overlap.update(
            p
            for p in own_paths
            if any(fnmatch.fnmatchcase(p, s) for s in other["scope"])
        )
        overlap.update(
            p
            for p in other_paths
            if any(fnmatch.fnmatchcase(p, s) for s in own["scope"])
        )
        overlap.update(set(own["scope"]) & set(other["scope"]))
        other["overlap"] = sorted(overlap)[:100] if other["id"] != task_id else []
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM coordination_notes WHERE project_id=? AND (source_id=? OR target_id=?) ORDER BY created_at DESC LIMIT 50",
            (task["project_id"], task_id, task_id),
        ).fetchall()
    return {
        "task_id": task_id,
        "tasks": [t for t in related if t["id"] != task_id][:50],
        "notes": [{**dict(row), "author": json.loads(row["author"])} for row in rows],
        "guidance": "Overlap is advisory, based on saved changes and declared scope. Coordinate related work before editing; notes do not interrupt agents or authorize edits in another task. Read notes again before review. Integration testing happens separately.",
    }
