"""Project E2B lifecycle, task worktrees, writer leases and off-sandbox checkpoints.

All coordination lives on the single app server. Worktrees isolate edits, not
untrusted code; approved commands in the same project VM share its trust boundary.
"""

import base64
import fcntl
import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import sandbox, sandbox_templates
from . import workspace_runtime as runtime
from .store import Store, data_dir, workspace

REMOTE_WORKTREES = "/home/user/worktrees"
MUTATIONS = {"patch_operation", "check_operation", "write_file", "execute_check"}
_last_extension: dict[str, float] = {}


def save(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def project_path(project_id: str) -> Path:
    # Existing project identifiers are not necessarily UUIDs.
    name = hashlib.sha256(project_id.encode()).hexdigest()
    return data_dir() / "project-sandboxes" / (name + ".json")


def project_state(project_id: str) -> dict:
    path = project_path(project_id)
    return json.loads(path.read_text()) if path.exists() else {}


@contextmanager
def project_lock(project_id: str):
    path = project_path(project_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def writer_lease(task_id: str, holder: str):
    """One live host operation; a persisted receipt identifies its writer.

    OS locks release on process death, not on a wall-clock timeout. The remote
    helper additionally holds an OS lock until an in-flight command really ends.
    """
    workspace(task_id)  # validate before forming any paths
    path = data_dir() / "workspace-leases" / (task_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "This worktree already has an active writer. Wait for its operation to finish."
            ) from None
        token = uuid.uuid4().hex
        save(
            path,
            {
                "holder": holder,
                "token": token,
                "started_at": time.time(),
                "active": True,
            },
        )
        try:
            yield token
        finally:
            save(
                path,
                {
                    "holder": holder,
                    "token": token,
                    "active": False,
                    "finished_at": time.time(),
                },
            )


def shared(root: Path) -> bool:
    return sandbox.is_remote(root) and bool(
        json.loads(sandbox.descriptor(root).read_text()).get("project_id")
    )


def task_meta(root: Path) -> dict:
    return json.loads(sandbox.descriptor(root).read_text())


def remote_call(client, meta: dict, operation: str, *args):
    # Extend an active shared VM before a bounded operation, at most once a minute.
    # Idle projects still pause; a check cannot be paused midway through its budget.
    with sandbox._lock:
        now = time.monotonic()
        if now - _last_extension.get(client.sandbox_id, 0) >= 60:
            # connect resumes paused VMs and only extends a running timeout.
            client.connect(timeout=sandbox.IDLE_TIMEOUT)
            _last_extension[client.sandbox_id] = now
    return sandbox._rpc(
        client, operation, list(args), remote_root=meta["remote_root"], shared=True
    )


def artifacts(task_id: str) -> Path:
    workspace(task_id)
    return data_dir() / "workspace-checkpoints" / task_id


def checkpoint_info(task_id: str) -> dict:
    path = artifacts(task_id) / "latest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def checkpoint(root: Path, client=None, meta=None) -> dict:
    meta = meta or task_meta(root)
    client = client or sandbox._client(meta)
    previous = checkpoint_info(root.name)
    info = remote_call(
        client,
        meta,
        "project_checkpoint",
        meta["sandbox_base"],
        previous.get("revision", ""),
    )
    if info.get("unchanged"):
        return {"revision": previous["revision"], "saved_at": previous["saved_at"]}
    destination = artifacts(root.name)
    generation = uuid.uuid4().hex
    for field in ("archive", "bundle"):
        raw = bytes(client.files.read(info[field], format="bytes"))
        if (
            len(raw) > runtime.MAX_SNAPSHOT + 8_000_000
            or hashlib.sha256(raw).hexdigest() != info[field + "_hash"]
        ):
            raise ValueError(
                "Checkpoint download failed integrity/size validation; previous checkpoint preserved"
            )
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = destination / (generation + "." + field)
        with target.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)
        info[field] = target.name
    info.update(saved_at=time.time(), generation=generation)
    save(destination / (generation + ".json"), info)
    save(destination / "latest.json", info)
    # Keep the current and previous complete generation for bounded crash recovery.
    generations = sorted(
        (p for p in destination.glob("*.archive") if p.with_suffix(".json").exists()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in generations[2:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".bundle").unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return {"revision": info["revision"], "saved_at": info["saved_at"]}


def call(root: Path, operation: str, args: list):
    meta = task_meta(root)
    project = project_state(meta["project_id"])
    if (
        project.get("status") != "ready"
        or project.get("generation") != meta["generation"]
    ):
        raise ValueError(
            "Project sandbox is being recovered. Wait for recovery before using this task."
        )
    client = sandbox._client(meta)
    result = remote_call(client, meta, operation, *args)
    if operation in MUTATIONS:
        try:
            checkpoint(root, client, meta)
        except Exception as error:
            raise ValueError(
                "The operation finished, but its recovery checkpoint failed. Files remain in the sandbox; retry checkpointing before continuing. "
                + str(error)[:300]
            ) from None
    return result


def _create(project: dict):
    selection = project["template_selection"]
    recovering = project.get("status") == "recovering"
    # Persist before the request: an unconfirmed create is never repeated silently.
    project["status"] = "creating"
    save(project_path(project["project_id"]), project)
    options = dict(
        timeout=sandbox.IDLE_TIMEOUT,
        secure=True,
        metadata={
            "app": "boltzmann",
            "project_id": project["project_id"],
            "generation": project["generation"],
        },
        lifecycle={"on_timeout": "pause", "auto_resume": True},
    )
    try:
        client = sandbox._sdk().create(template=selection["template"], **options)
    except Exception as error:
        from e2b import NotFoundException

        if not (
            isinstance(error, NotFoundException)
            and selection.get("source") == "automatic"
            and selection["template"] != "base"
        ):
            raise
        project["template_selection"] = {
            **selection,
            "template": "base",
            "source": "fallback",
            "reason": "The selected template was removed; using the general-purpose environment.",
        }
        save(project_path(project["project_id"]), project)
        client = sandbox._sdk().create(template="base", **options)
    project["sandbox_id"] = client.sandbox_id
    project["status"] = "recovering" if recovering else "ready"
    save(project_path(project["project_id"]), project)
    sandbox._install(client)
    with sandbox._lock:
        sandbox._clients[client.sandbox_id] = client
    return client


def prepare(project_id: str, source: str, task_id: str, parent_id: str = "") -> dict:
    started = time.monotonic()
    root = workspace(task_id)
    with project_lock(project_id):
        project = project_state(project_id)
        if sandbox.is_remote(root):
            meta = task_meta(root)
            if meta.get("project_id") != project_id:
                raise ValueError("Task already has a different workspace")
        else:
            base = runtime.git(Path(source), "rev-parse", "HEAD").strip()
            archive = sandbox.source_archive(Path(source), base)
            patch = ""
            if parent_id:
                from . import workspaces

                parent = Store().get("tasks", parent_id)
                base = parent["base_commit"]
                delta = workspaces.diff(workspace(parent_id), base)
                if delta["truncated"] or set(
                    workspaces.changed_paths(workspace(parent_id), base)
                ) != set(delta["files"]):
                    raise ValueError(
                        "The prior task has unsupported changes; preserve or export them before forking"
                    )
                archive = base64.b64decode(
                    sandbox.call(workspace(parent_id), "export_base", base)
                )
                patch = delta["patch"]
            directory = artifacts(task_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            (directory / "initial.tar.gz").write_bytes(archive)
            meta = {
                "project_id": project_id,
                "remote_root": REMOTE_WORKTREES + "/" + task_id,
                "source_base": base,
                "source_dirty": not parent_id
                and bool(runtime.git(Path(source), "status", "--porcelain")),
                "status": "preparing",
                "initialization_patch": patch,
                "parent_task_id": parent_id,
            }
            if not project:
                project = {
                    "project_id": project_id,
                    "generation": uuid.uuid4().hex,
                    "tasks": {},
                    "template_selection": sandbox_templates.select(
                        archive, sandbox.configuration()["template"]
                    ),
                }
            used = set(project["tasks"].values())
            port = next((p for p in range(20000, 49000) if p not in used), None)
            if port is None:
                raise ValueError("Project preview port capacity reached")
            project["tasks"][task_id] = port
            save(project_path(project_id), project)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            sandbox._save(root, meta)
        if project.get("status") == "creating":
            raise ValueError(
                "Project sandbox creation is unconfirmed. Check E2B and use Recover sandbox; creation was not repeated."
            )
        if project.get("status") not in {None, "ready"}:
            raise ValueError(
                "Finish project sandbox recovery before preparing another task"
            )
        client = (
            sandbox._client(project) if project.get("sandbox_id") else _create(project)
        )
        meta.update(
            sandbox_id=project["sandbox_id"],
            generation=project["generation"],
            template_selection=project["template_selection"],
            preview_port=project["tasks"][task_id],
        )
        sandbox._save(root, meta)
        if meta["status"] != "ready":
            archive_path = sandbox.CONTROL_ROOT + "/initial-" + task_id + ".tar.gz"
            client.files.write(
                archive_path, (artifacts(task_id) / "initial.tar.gz").read_bytes()
            )
            meta["sandbox_base"] = remote_call(
                client,
                meta,
                "project_prepare",
                archive_path,
                meta["source_base"],
                meta.get("initialization_patch", ""),
            )
            meta["status"] = "ready"
            sandbox._save(root, meta)
        checkpoint(root, client, meta)
        return prepared(meta, started)


def prepared(meta: dict, started: float) -> dict:
    return {
        **sandbox._prepared(meta, started),
        "workspace_layout": "project-worktree",
        "workspace": f"e2b://{meta['sandbox_id']}{meta['remote_root']}",
        "worktree_branch": "sdlc/" + meta["remote_root"].rsplit("/", 1)[1],
        "sandbox_generation": meta["generation"],
        "preview_port": meta["preview_port"],
    }


def reserve_port(root: Path, requested: int | None) -> int:
    meta = task_meta(root)
    with project_lock(meta["project_id"]):
        project = project_state(meta["project_id"])
        port = requested if requested is not None else project["tasks"][root.name]
        if not 1024 <= port <= 65535 or port in {49983, 49999}:
            raise ValueError("Use an application port between 1024 and 65535")
        if any(
            identity != root.name and value == port
            for identity, value in project["tasks"].items()
        ):
            raise ValueError(
                "This preview port belongs to another task. Choose a different port or use automatic setup."
            )
        project["tasks"][root.name] = port
        save(project_path(meta["project_id"]), project)
        meta["preview_port"] = port
        sandbox._save(root, meta)
        return port


def release(root: Path, remove: bool):
    from . import previews

    meta = task_meta(root)
    # Preview locks precede project locks (also used when reserving a port).
    if previews.saved(root):
        previews.stop(root)
    with project_lock(meta["project_id"]):
        if remove:
            if meta.get("sandbox_id"):
                remote_call(sandbox._client(meta), meta, "project_remove")
            project = project_state(meta["project_id"])
            project["tasks"].pop(root.name, None)
            save(project_path(meta["project_id"]), project)
            shutil.rmtree(artifacts(root.name), ignore_errors=False)
            sandbox.descriptor(root).unlink()
        else:
            if meta.get("status") == "ready":
                checkpoint(root)
            meta["retired"] = True
            sandbox._save(root, meta)


def recover(project_id: str) -> dict:
    """Caller must first quiesce workflows. Kill/fence the old VM before replacement."""
    from . import previews

    with project_lock(project_id):
        project = project_state(project_id)
        if not project:
            raise ValueError("This project has no shared sandbox")
        if project.get("status") == "awaiting_verification_reset":
            return {
                "sandbox_id": project["sandbox_id"],
                "tasks": [
                    {
                        "id": identity,
                        **prepared(task_meta(workspace(identity)), time.monotonic()),
                    }
                    for identity in project["tasks"]
                ],
            }
        if project.get("status") != "recovering":
            # Validate every recovery input before touching the live sandbox.
            for identity in project["tasks"]:
                meta = task_meta(workspace(identity))
                info = checkpoint_info(identity)
                if meta.get("status") == "ready" and not info:
                    raise ValueError(
                        "A task has no complete recovery checkpoint. Save one before recovering."
                    )
                for field in ("archive", "bundle") if info else ():
                    name = info[field]
                    if (
                        Path(name).name != name
                        or hashlib.sha256(
                            (artifacts(identity) / name).read_bytes()
                        ).hexdigest()
                        != info[field + "_hash"]
                    ):
                        raise ValueError(
                            "Recovery checkpoint checksum mismatch; the current sandbox was preserved"
                        )
                if not info and not (artifacts(identity) / "initial.tar.gz").is_file():
                    raise ValueError(
                        "Initial workspace snapshot is missing; the current sandbox was preserved"
                    )
            old_id = project.get("sandbox_id")
            if old_id:
                try:
                    sandbox._sdk().kill(old_id)
                except Exception as error:
                    from e2b import NotFoundException

                    if not isinstance(error, NotFoundException):
                        raise ValueError(
                            "Could not confirm the old sandbox was stopped; no replacement was created"
                        ) from None
                with sandbox._lock:
                    sandbox._clients.pop(old_id, None)
                    _last_extension.pop(old_id, None)
            project.update(status="recovering", generation=uuid.uuid4().hex)
            project.pop("sandbox_id", None)
            save(project_path(project_id), project)
        if project.get("sandbox_id"):
            try:
                client = sandbox._client(project)
            except Exception as error:
                from e2b import NotFoundException

                if not isinstance(error, NotFoundException):
                    raise
                with sandbox._lock:
                    sandbox._clients.pop(project["sandbox_id"], None)
                project.pop("sandbox_id")
                project["generation"] = uuid.uuid4().hex
                save(project_path(project_id), project)
                client = _create(project)
        else:
            client = _create(project)
            project["status"] = "recovering"
            save(project_path(project_id), project)
        recovered = []
        for identity in project["tasks"]:
            root = workspace(identity)
            meta = task_meta(root)
            info = checkpoint_info(identity)
            meta.update(
                sandbox_id=project["sandbox_id"], generation=project["generation"]
            )
            if info:
                upload = dict(info)
                for field in ("archive", "bundle"):
                    remote = (
                        sandbox.CONTROL_ROOT + "/recovery-" + identity + "." + field
                    )
                    client.files.write(
                        remote, (artifacts(identity) / info[field]).read_bytes()
                    )
                    upload[field] = remote
                meta["sandbox_base"] = remote_call(
                    client, meta, "project_restore", upload, meta["source_base"]
                )
            else:
                remote = sandbox.CONTROL_ROOT + "/initial-" + identity + ".tar.gz"
                client.files.write(
                    remote, (artifacts(identity) / "initial.tar.gz").read_bytes()
                )
                meta["sandbox_base"] = remote_call(
                    client,
                    meta,
                    "project_prepare",
                    remote,
                    meta["source_base"],
                    meta.get("initialization_patch", ""),
                )
            meta["status"] = "ready"
            sandbox._save(root, meta)
            previews.forget(
                root
            )  # old process IDs and preview URLs never follow a replacement
            recovered.append({"id": identity, **prepared(meta, time.monotonic())})
        project["status"] = "awaiting_verification_reset"
        save(project_path(project_id), project)
        return {"sandbox_id": project["sandbox_id"], "tasks": recovered}


def finish_recovery(project_id: str):
    with project_lock(project_id):
        project = project_state(project_id)
        if project.get("status") != "awaiting_verification_reset":
            raise ValueError("Sandbox recovery has not finished restoring worktrees")
        project["status"] = "ready"
        save(project_path(project_id), project)
