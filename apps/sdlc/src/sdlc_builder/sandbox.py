"""E2B workspace lifecycle and a small, batched filesystem RPC transport.

Only sandbox IDs/configuration are persisted. Credentials stay in the app's
environment; neither repository code nor the remote helper receives them.
"""

import base64
import functools
import hashlib
import io
import json
import logging
import os
import shlex
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import sandbox_templates
from . import workspace_runtime as runtime
from .store import data_dir, workspace

REMOTE_ROOT = "/home/user/workspace"
CONTROL_ROOT = "/home/user/.boltzmann"
IDLE_TIMEOUT = 300
logger = logging.getLogger(__name__)
_clients: dict[str, object] = {}
_lock = threading.RLock()


def configuration() -> dict:
    backend = os.environ.get("SDLC_WORKSPACE_BACKEND", "e2b")
    if backend not in {"e2b", "local"}:
        raise ValueError("SDLC_WORKSPACE_BACKEND must be e2b or local")
    return {
        "backend": backend,
        "ready": backend == "local" or bool(os.environ.get("E2B_API_KEY")),
        "template": os.environ.get("E2B_TEMPLATE", "auto"),
    }


def descriptor(root: Path) -> Path:
    # A repository cannot opt itself into a backend by adding a marker file.
    if root != workspace(root.name):
        return data_dir() / "sandboxes" / "not-a-workspace"
    return data_dir() / "sandboxes" / f"{root.name}.json"


def is_remote(root: Path) -> bool:
    # Project paths need not have task-shaped names.
    if len(root.name) != 32 or any(c not in "0123456789abcdef" for c in root.name):
        return False
    return descriptor(root).is_file()


def _save(root: Path, value: dict):
    path = descriptor(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def _sdk():
    from e2b import Sandbox

    if not os.environ.get("E2B_API_KEY"):
        raise ValueError(
            "Set E2B_API_KEY in the launch environment and restart boltzmann to use E2B workspaces."
        )
    return Sandbox


@functools.lru_cache(maxsize=1)
def _runtime_path() -> tuple[str, str]:
    source = Path(runtime.__file__).read_text()
    version = hashlib.sha256(source.encode()).hexdigest()[:16]
    return f"{CONTROL_ROOT}/runtime-{version}.py", source


def _install(client):
    path, source = _runtime_path()
    client.commands.run(f"mkdir -p {shlex.quote(CONTROL_ROOT)}", timeout=15)
    client.files.write(path, source)


def _client(meta: dict):
    identity = meta["sandbox_id"]
    # Reuse the SDK transport. E2B auto-resume handles idle sandboxes; after an
    # app restart connect resumes the saved ID instead of creating an empty VM.
    with _lock:
        if identity not in _clients:
            client = _sdk().connect(identity, timeout=IDLE_TIMEOUT)
            _install(client)
            _clients[identity] = client
        return _clients[identity]


def _rpc(client, operation: str, args: list, *, remote_root=REMOTE_ROOT, shared=False):
    script, _ = _runtime_path()
    request_path = f"{CONTROL_ROOT}/{uuid.uuid4().hex}.json"
    client.files.write(
        request_path,
        json.dumps(
            {
                "root": remote_root,
                "shared": shared,
                "operation": operation,
                "args": args,
            }
        ),
    )
    reply = client.commands.run(
        shlex.join(["python3", "-I", script, request_path]),
        timeout=110,
        request_timeout=115,
    )
    envelope = json.loads(reply.stdout)
    if "error" in envelope:
        raise ValueError(envelope["error"])
    return envelope["result"]


def call(root: Path, operation: str, *args):
    started = time.monotonic()
    meta = json.loads(descriptor(root).read_text())
    if meta.get("status") != "ready":
        raise ValueError(
            "E2B workspace preparation is incomplete. Delete this task's workspace and start again."
        )
    if operation in {"diff", "diff_entries", "changed_paths"} and not args:
        args = (meta["sandbox_base"],)
    # The remote Git baseline is a sanitized snapshot, while project merge still
    # validates the original commit in the source checkout.
    args = [
        meta.get("sandbox_base", arg)
        if isinstance(arg, str) and arg == meta.get("source_base")
        else arg
        for arg in args
    ]
    try:
        if meta.get("project_id"):
            from . import project_workspaces

            return project_workspaces.call(root, operation, args)
        return _rpc(_client(meta), operation, args)
    except ValueError:
        raise
    except Exception as error:
        # No automatic re-execution: a transport failure after a command/write
        # may have an unknown outcome. Activity journals preserve that boundary.
        raise ValueError(
            f"E2B workspace unavailable ({type(error).__name__}). Check E2B connectivity and the saved sandbox; the operation was not automatically repeated."
        ) from None
    finally:
        logger.info(
            "workspace_operation backend=e2b task=%s operation=%s duration_ms=%d",
            root.name,
            operation,
            round((time.monotonic() - started) * 1000),
        )


def remote_capable(function):
    @functools.wraps(function)
    def wrapped(root: Path, *args, **kwargs):
        if is_remote(root):
            if kwargs:
                raise ValueError(
                    "Remote workspace operations require positional arguments"
                )
            return call(root, function.__name__, *args)
        return function(root, *args, **kwargs)

    return wrapped


def source_archive(source: Path, base: str) -> bytes:
    """Copy only allowed regular files at committed HEAD, never .git or secrets."""
    result = io.BytesIO()
    with tempfile.TemporaryFile() as raw:
        subprocess.run(
            ["git", "archive", "--format=tar", base],
            cwd=source,
            stdout=raw,
            stderr=subprocess.PIPE,
            check=True,
            timeout=45,
        )
        raw.seek(0)
        total = count = 0
        with (
            tarfile.open(fileobj=raw) as archive,
            tarfile.open(fileobj=result, mode="w:gz") as filtered,
        ):
            for member in archive:
                if not member.isfile() or not runtime.allowed(member.name):
                    continue
                total += member.size
                count += 1
                if total > runtime.MAX_SNAPSHOT or count > 50000:
                    raise ValueError("Repository exceeds sandbox snapshot limits")
                # No ownership/PAX metadata from the source is needed remotely.
                info = tarfile.TarInfo(member.name)
                info.size, info.mode = member.size, member.mode & 0o777
                filtered.addfile(info, archive.extractfile(member))
    return result.getvalue()


def prepare(source: str, task_id: str, *, parent_id: str = "") -> dict:
    started = time.monotonic()
    config = configuration()
    sdk = _sdk()  # Fail closed before creating any workspace without a key.
    root = workspace(task_id)
    if root.exists() or is_remote(root):
        raise ValueError("Workspace already exists")
    patch = ""
    selection = None
    if parent_id:
        from . import workspaces as ws
        from .store import Store

        parent = workspace(parent_id)
        task = Store().get("tasks", parent_id)
        base = task["base_commit"]
        delta = ws.diff(parent, base)
        if delta["truncated"]:
            raise ValueError("The prior diff exceeds the continuation limit")
        if set(ws.changed_paths(parent, base)) != set(delta["files"]):
            raise ValueError(
                "The prior workspace contains unsupported changes; it was preserved"
            )
        patch = delta["patch"]
        if is_remote(parent):
            selection = json.loads(descriptor(parent).read_text()).get(
                "template_selection"
            )
        archive = (
            base64.b64decode(call(parent, "export_base", base))
            if is_remote(parent)
            else source_archive(parent, base)
        )
        dirty = False
    else:
        source_path = Path(source).expanduser().resolve()
        base = runtime.git(source_path, "rev-parse", "HEAD").strip()
        dirty = bool(runtime.git(source_path, "status", "--porcelain"))
        archive = source_archive(source_path, base)
    # Forks retain their parent's environment. New tasks inspect exactly the
    # committed files that will enter the sandbox, never dirty host manifests.
    selection = selection or sandbox_templates.select(archive, config["template"])
    client = None
    try:
        options = dict(
            timeout=IDLE_TIMEOUT,
            secure=True,
            metadata={"app": "boltzmann", "task_id": task_id},
            lifecycle={"on_timeout": "pause", "auto_resume": True},
        )
        try:
            client = sdk.create(template=selection["template"], **options)
        except Exception as error:
            from e2b import NotFoundException

            # Only a definite missing template is safe to retry. A transport
            # error could mean the sandbox was created but its reply was lost.
            if not (
                isinstance(error, NotFoundException)
                and selection["source"] == "automatic"
                and selection["template"] != "base"
            ):
                raise
            selection = {
                **selection,
                "template": "base",
                "source": "fallback",
                "reason": "The selected template was removed; using the general-purpose environment.",
            }
            client = sdk.create(template="base", **options)
        root.mkdir(parents=True, mode=0o700)
        meta = {
            "sandbox_id": client.sandbox_id,
            "source_base": base,
            "status": "preparing",
            "template_selection": selection,
            "source_dirty": dirty,
            "parent_task_id": parent_id,
            "initialization_patch": patch,
        }
        _save(root, meta)  # Save the ID before uploading so failed prep can be deleted.
        _install(client)
        archive_path = f"{CONTROL_ROOT}/source.tar.gz"
        client.files.write(archive_path, archive)
        meta["sandbox_base"] = _rpc(client, "initialize", [archive_path, patch])
        meta["status"] = "ready"
        _save(root, meta)
        with _lock:
            _clients[client.sandbox_id] = client
    except Exception as error:
        # If storing the ID failed, do not leave an untracked running sandbox.
        if client is not None and not is_remote(root):
            try:
                sdk.kill(client.sandbox_id)
            except Exception as cleanup_error:
                logger.error(
                    "Could not clean up sandbox %s after descriptor failure (%s)",
                    client.sandbox_id,
                    type(cleanup_error).__name__,
                )
        if isinstance(error, ValueError):
            raise
        raise ValueError(
            f"E2B workspace preparation failed ({type(error).__name__}). Check E2B configuration; any saved sandbox can be removed by deleting this task's workspace."
        ) from None
    return _prepared(meta, started)


def _prepared(meta: dict, started: float) -> dict:
    selection = meta.get("template_selection") or {"template": "base"}
    return {
        "workspace": f"e2b://{meta['sandbox_id']}{REMOTE_ROOT}",
        "workspace_backend": "e2b",
        "sandbox_id": meta["sandbox_id"],
        "sandbox_template": selection["template"],
        "sandbox_environment": selection,
        "base_commit": meta["source_base"],
        "source_dirty": meta.get("source_dirty", False),
        "preparation_ms": round((time.monotonic() - started) * 1000),
        **(
            {"parent_task_id": meta["parent_task_id"]}
            if meta.get("parent_task_id")
            else {}
        ),
    }


def retry_preparation(task: dict) -> dict:
    """Resume an undispatched task in its saved sandbox, preserving its snapshot."""
    started = time.monotonic()
    root = workspace(task["id"])
    from . import project_workspaces

    if project_workspaces.shared(root):
        from .store import Store

        project = Store().get("projects", task["project_id"])
        return project_workspaces.prepare(
            project["id"], project["path"], task["id"], task.get("parent_task_id", "")
        )
    meta = json.loads(descriptor(root).read_text())
    if meta.get("status") != "ready":
        if (
            task["input"].get("continuation_context")
            and "initialization_patch" not in meta
        ):
            raise ValueError(
                "This older workspace has no saved continuation patch. Start a new task from its parent; the existing sandbox was preserved."
            )
        try:
            client = _client(meta)
            _install(client)
            meta["sandbox_base"] = _rpc(
                client,
                "initialize",
                [f"{CONTROL_ROOT}/source.tar.gz", meta.get("initialization_patch", "")],
            )
            meta["status"] = "ready"
            _save(root, meta)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(
                f"Could not resume sandbox setup ({type(error).__name__}). The sandbox was preserved. Retry after checking E2B connectivity, or start a new task."
            ) from None
    return _prepared(meta, started)


def release(root: Path, *, remove: bool):
    if not is_remote(root):
        return
    meta = json.loads(descriptor(root).read_text())
    if meta.get("project_id"):
        from . import project_workspaces

        return project_workspaces.release(root, remove)
    try:
        sdk = _sdk()
        if remove:
            sdk.kill(meta["sandbox_id"])
        else:
            sdk.pause(meta["sandbox_id"])
    except Exception as error:
        raise ValueError(
            f"Could not {'delete' if remove else 'pause'} the E2B workspace ({type(error).__name__}). The task was kept so you can retry."
        ) from None
    with _lock:
        _clients.pop(meta["sandbox_id"], None)
    if remove:
        descriptor(root).unlink()
