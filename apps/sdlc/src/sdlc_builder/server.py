"""Local-only API, worker lifetime, packaged harness embed, and state archive."""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import secrets
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.large_payload import local_payload_storage
from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    create_agent_harness_app,
    create_session_manager_worker,
)
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from . import (
    coordination,
    previews,
    project_workspaces,
    remote_repositories,
    repositories,
    reviews,
    sandbox,
    workspaces,
)
from .activities import decide, workspace_action
from .bounded_code import bounded_resume, bounded_start
from .coding_workflow import WORKFLOW_NAME_V2, CodingRoleWorkflow, CodingWorkflow
from .coding_workspace import coding_operation, project_merge_operation
from .collaboration import Collaboration, current_user
from .collaboration import identity as user_identity
from .integrations import integration_for, profile_available, registered_integrations
from .models import Control, FollowUpInput, Profile, TaskInput
from .store import Store, data_dir, workspace
from .team_api import SharedWebsocketBoundary
from .team_api import install_routes as install_team_routes
from .worker_lifecycle import managed_worker
from .workflow import TASK_QUEUE, WORKFLOW_NAME, SdlcAgentWorkflow


class NewProject(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


class RemoteProject(BaseModel):
    url: str = Field(min_length=1, max_length=1000)


class NewTask(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str
    profile_id: str
    prompt: str = Field(min_length=1, max_length=20000)
    mode: str = Field(default="change", pattern="^(ask|change)$")
    parent_task_id: str = ""
    engine: str = Field(default="v2", pattern="^(v1|v2)$")


class SaveProfile(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(pattern="^(openai|anthropic|compatible)$")
    model: str = Field(min_length=1, max_length=200)
    base_url: str = ""
    env_var: str = Field(default="", pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    api_key: str = Field(default="", max_length=4096)
    max_steps: int = Field(default=24, ge=3, le=200)


class FileEdit(BaseModel):
    path: str
    content: str = Field(max_length=100000)
    expected_hash: str


class DecisionVersion(BaseModel):
    expected_version: int | None = None


class HttpControl(Control):
    expected_version: int | None = None


class DeleteTask(BaseModel):
    remove_workspace: bool = False


class StartPreview(BaseModel):
    command: str = Field(default="", max_length=4000)
    cwd: str | None = Field(default=None, max_length=4000)
    port: int | None = Field(default=None, ge=1024, le=65535)


class Handoff(BaseModel):
    user_id: str
    expected_ownership_version: int


class CoordinationNote(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    target_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    message: str = Field(min_length=1, max_length=4000)


class NewMessage(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_turn: int = Field(ge=2)
    prompt: str = Field(min_length=1, max_length=20000)
    mode: str = Field(pattern="^(ask|change)$")
    profile_id: str
    max_steps: int = Field(ge=3, le=200)


def create_app() -> FastAPI:
    store = Store()
    team = Collaboration(store)
    token_path = data_dir() / "session-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()
    offload = local_payload_storage(base_dir=data_dir() / "payloads")
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="sdlc",
                workflow_type=WORKFLOW_NAME,
                task_queue=TASK_QUEUE,
                label="SDLC builder",
                description="Local development controller",
            ),
            AgentDescriptor(
                key="sdlc-v2",
                workflow_type=WORKFLOW_NAME_V2,
                task_queue=TASK_QUEUE,
                label="Coding agent V2",
                description="Native coding tools, Code Mode and independent review",
            ),
            AgentDescriptor(
                key="coding-role-v2",
                workflow_type="SdlcCodingRoleV2",
                task_queue=TASK_QUEUE,
                label="Coding role",
                description="Controller-owned coding assignment",
            ),
        ]
    )
    harness = create_agent_harness_app(
        registry=registry,
        manager_workflow_id="sdlc-session-manager",
        manager_task_queue="sdlc-session-manager",
        large_payload_offload=offload,
    )
    task_locks: dict[str, asyncio.Lock] = {}
    folder_picker_lock = asyncio.Lock()
    project_lock = asyncio.Lock()
    project_workspace_locks: dict[str, asyncio.Lock] = {}
    profile_checks: set[str] = set()
    removing_profiles: set[str] = set()

    async def dispatch_followup(app, task):
        pending = task["pending_message"]
        try:
            reply = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(
                    type="follow_up",
                    payload=pending["payload"],
                    expected_turn=pending["expected_turn"],
                ),
                id="sdlc-message-" + pending["id"],
                result_type=AgentMessageReply,
                rpc_timeout=timedelta(seconds=5),
            )
        except WorkflowUpdateFailedError:
            receipt = {
                "id": pending["id"],
                "status": "rejected",
                "error": "The agent could not accept this message. Refresh the task, wait for its current message to finish, and send again.",
            }
            task["message_error"] = {**receipt, "prompt": pending["payload"]["prompt"]}
        except asyncio.CancelledError:
            raise
        except Exception:
            pending["error"] = (
                "Waiting for Temporal to confirm this message. boltzmann will retry delivery automatically; it will not run the message twice."
            )
            store.put("tasks", task)
            return {"id": pending["id"], "status": "pending"}
        else:
            task["current_profile_id"] = pending["payload"]["profile"]["id"]
            receipt = {
                "id": pending["id"],
                "status": "accepted",
                "turn_number": reply.turn_number,
            }
            task.pop("message_error", None)
        if pending.get("audit_id"):
            team.finish_audit(
                pending["audit_id"],
                "completed" if receipt["status"] == "accepted" else "rejected",
            )
        task.setdefault("message_receipts", {})[pending["id"]] = {
            **receipt,
            "request_hash": pending["request_hash"],
        }
        task.pop("pending_message")
        store.put("tasks", task)
        return receipt

    async def reconcile(app):
        slots = asyncio.Semaphore(8)

        async def collect(identity):
            async with slots, task_locks.setdefault(identity, asyncio.Lock()):
                try:
                    task = store.get("tasks", identity)
                except ValueError:
                    return
                return await reconcile_task(app, task)

        while True:
            # One slow task must not delay dispatch/progress for every other task.
            results = await asyncio.gather(
                *(collect(t["id"]) for t in store.all("tasks"))
            )
            observed = [result for result in results if result is not None]
            if observed:
                app.state.temporal_ok = all(observed)
            await asyncio.sleep(1)

    async def reconcile_task(app, task):
        if task.get("dispatch") == "preparing":
            return
        try:
            if task.get("dispatch") != "submitted":
                await AgentClient(
                    app.state.client, task["workflow_id"]
                ).start_and_submit_message(
                    "build",
                    task["input"],
                    1,
                    workflow_name=WORKFLOW_NAME_V2
                    if task.get("engine") == "v2"
                    else WORKFLOW_NAME,
                    task_queue=TASK_QUEUE,
                    start_config=AgentConfig(),
                    update_id=task["id"],
                )
                task["dispatch"] = "submitted"
                task.pop("dispatch_error", None)
                store.put("tasks", task)
            if task.get("pending_message"):
                await dispatch_followup(app, task)
            state = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).query("progress", rpc_timeout=timedelta(seconds=3))
            store.archive(task["id"], state)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Keep the last known snapshot. UI marks it stale instead of erasing progress.
            if task.get("dispatch") != "submitted":
                task["dispatch_error"] = (
                    f"Waiting to dispatch ({type(error).__name__}). Retrying automatically."
                )
                store.put("tasks", task)
            return False

    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.temporal_ok = False
        integrations = list(registered_integrations().values())
        client = await Client.connect(
            **ClientConfig.load_client_connect_config(),
            plugins=[
                *[
                    plugin
                    for backend in integrations
                    for plugin in backend.client_plugins()
                ],
                AgentHarnessPlugin(
                    tools=[workspace_action], large_payload_offload=offload
                ),
            ],
        )
        app.state.client = client
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(
                managed_worker(
                    Worker(
                        client,
                        task_queue=TASK_QUEUE,
                        workflows=[
                            SdlcAgentWorkflow,
                            CodingWorkflow,
                            CodingRoleWorkflow,
                        ],
                        activities=[
                            decide,
                            coding_operation,
                            project_merge_operation,
                            bounded_start,
                            bounded_resume,
                        ],
                        plugins=[
                            plugin
                            for backend in integrations
                            for plugin in backend.worker_plugins()
                        ],
                    )
                )
            )
            await stack.enter_async_context(
                managed_worker(
                    create_session_manager_worker(
                        client, task_queue="sdlc-session-manager"
                    )
                )
            )
            # Mounted FastAPI applications do not enter their lifespan automatically.
            await stack.enter_async_context(harness.router.lifespan_context(harness))
            app.state.temporal_ok = True
            collector = asyncio.create_task(reconcile(app))
            try:
                yield
            finally:
                collector.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await collector

    app = FastAPI(
        title="boltzmann",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    app.state.team = team
    app.add_middleware(SharedWebsocketBoundary, enabled=team.enabled)
    install_team_routes(app, team, token)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed_host = (
            host == urlsplit(team.origin).netloc
            if team.enabled
            else host.split(":")[0] in {"127.0.0.1", "localhost", "testserver"}
        )
        if not allowed_host:
            return JSONResponse(
                {
                    "detail": "Host does not match this workspace"
                    if team.enabled
                    else "Local requests only"
                },
                status_code=403,
            )
        path = request.url.path
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            expected_origin = team.origin if team.enabled else f"http://{host}"
            if origin and origin != expected_origin:
                return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
            if request.headers.get("x-sdlc") != "1":
                return JSONResponse(
                    {"detail": "Missing application request header"}, status_code=403
                )
        public = {
            "/api/auth/status",
            "/api/auth/bootstrap",
            "/api/auth/login",
            "/api/auth/join",
            "/api/unlock",
        }
        user = None
        if path.startswith(("/api/", "/harness")) and path not in public:
            cookie = request.cookies.get("sdlc_session", "")
            user = (
                team.session_user(cookie)
                if team.enabled
                else team.user("local-owner")
                if secrets.compare_digest(cookie, token)
                else None
            )
            if not user:
                return JSONResponse(
                    {
                        "detail": "Sign in to continue"
                        if team.enabled
                        else "Open the launch URL to unlock this local app"
                    },
                    status_code=401,
                )
        if team.enabled and path.startswith("/harness"):
            return JSONResponse(
                {"detail": "The debugger is available in local mode"}, status_code=403
            )
        if path.startswith("/harness") and request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                {"detail": "Use the SDLC controls; the embedded debugger is read-only"},
                status_code=403,
            )
        context = current_user.set(user)
        try:
            if user and team.enabled:
                parts = path.strip("/").split("/")
                method = request.method
                if len(parts) >= 3 and parts[:2] == ["api", "tasks"]:
                    try:
                        task = store.get("tasks", parts[2])
                    except ValueError:
                        raise HTTPException(404, "Task not found") from None
                    minimum = "viewer" if method in {"GET", "HEAD"} else "developer"
                    if (
                        (len(parts) > 3 and parts[3] == "merge")
                        or method == "DELETE"
                        and len(parts) == 3
                    ):
                        minimum = "maintainer"
                    team.require(task["project_id"], minimum)
                    if (
                        method not in {"GET", "HEAD"}
                        and len(parts) > 3
                        and parts[3]
                        in {"control", "messages", "file", "preview", "retry-setup"}
                        and task.get("workspace_backend") != "e2b"
                        and not user["admin"]
                    ):
                        raise HTTPException(
                            403,
                            "Only the administrator can operate a legacy local workspace. Start an E2B task for shared work.",
                        )
                elif len(parts) >= 3 and parts[:2] == ["api", "projects"]:
                    if parts[2] in {"pick-folder", "folders", "discovery"}:
                        team.require_admin()
                    elif parts[2] not in {"hidden", "remote"}:
                        team.require(parts[2])
                elif parts[:2] == ["api", "profiles"] and method not in {"GET", "HEAD"}:
                    team.require_admin()
                elif path == "/api/demo":
                    team.require_admin()
            response = await call_next(request)
        except HTTPException as error:
            response = JSONResponse(
                {"detail": error.detail}, status_code=error.status_code
            )
        finally:
            current_user.reset(context)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        # Pydantic's default response includes input values, which can contain an API key.
        return JSONResponse(
            {
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"]} for item in error.errors()
                ]
            },
            status_code=422,
        )

    @app.post("/api/unlock")
    async def unlock(request: Request):
        if team.enabled:
            raise HTTPException(
                409, "Create the owner account or sign in to the shared workspace"
            )
        body = await request.json()
        if not secrets.compare_digest(str(body.get("token", "")), token):
            raise HTTPException(401, "Incorrect launch token")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "sdlc_session", token, httponly=True, samesite="strict", max_age=30 * 86400
        )
        return response

    @app.get("/api/home")
    async def home():
        config = sandbox.configuration()
        projects = team.projects()
        visible = {item["id"] for item in projects}
        return {
            "projects": projects,
            "user": team.actor(),
            "shared": team.enabled,
            "profiles": [
                {**profile, "available": profile_available(profile)}
                for profile in store.all("profiles")
            ],
            "tasks": [
                preparation_state(task)
                for task in store.all("tasks")
                if task.get("project_id") in visible
                or not team.enabled
                and not task.get("project_id")
            ],
            "health": {
                "temporal": app.state.temporal_ok,
                "harness_version": importlib.metadata.version("temporal-agent-harness"),
                "data_dir": str(data_dir()) if team.actor()["admin"] else "",
                "execution": "e2b"
                if config["backend"] == "e2b"
                else "host-with-approval",
                "sandbox_ready": config["ready"],
                "sandbox_template": config["template"],
                "milestone": "3 · execution workspace and interactive review",
            },
        }

    def task_permissions(task: dict) -> dict:
        role = team.role(task.get("project_id", ""))
        writable = role in {"developer", "maintainer", "owner"}
        maintain = role in {"maintainer", "owner"}
        host_allowed = (
            not team.enabled
            or team.actor()["admin"]
            or task.get("workspace_backend") == "e2b"
        )
        owned = (
            not team.enabled
            or task.get("workspace_layout") != "project-worktree"
            or (task.get("owner") or {}).get("id") == team.actor()["id"]
        )
        shared_state = (
            project_workspaces.project_state(task["project_id"])
            if task.get("workspace_layout") == "project-worktree"
            else {}
        )
        recovering = shared_state.get("status") not in {None, "ready"}
        return {
            **task,
            "sandbox_status": shared_state.get("status"),
            "can_control": bool(writable and host_allowed and owned and not recovering),
            "can_handoff": bool(
                writable
                and (owned or maintain)
                and task.get("workspace_layout") == "project-worktree"
            ),
            "checkpoint": {
                k: v
                for k, v in project_workspaces.checkpoint_info(task["id"]).items()
                if k in {"saved_at", "revision"}
            }
            if task.get("workspace_layout") == "project-worktree"
            else None,
            "can_review": writable,
            "can_accept": maintain and not recovering,
            "can_merge": maintain and not recovering,
            "can_manage": maintain,
        }

    def require_available(task: dict):
        if task.get(
            "workspace_layout"
        ) == "project-worktree" and project_workspaces.project_state(
            task["project_id"]
        ).get("status") not in {None, "ready"}:
            raise HTTPException(
                409, "Finish project sandbox recovery before continuing this task"
            )

    def require_writer(task: dict):
        require_available(task)
        if (
            team.enabled
            and task.get("workspace_layout") == "project-worktree"
            and (task.get("owner") or {}).get("id") != team.actor()["id"]
        ):
            raise HTTPException(
                403,
                "This task belongs to another developer. Ask its owner or a maintainer to hand it off first.",
            )

    def writer_guard(identity: str):
        return (
            project_workspaces.writer_lease(identity, "user:" + team.actor()["id"])
            if project_workspaces.shared(workspace(identity))
            else contextlib.nullcontext()
        )

    def preparation_state(task: dict) -> dict:
        if task.get("dispatch") == "preparing":
            lock = task_locks.get(task["id"])
            task["preparation_running"] = bool(lock and lock.locked())
            if not task["preparation_running"] and not task.get("preparation_error"):
                task["preparation_error"] = (
                    "Workspace setup was interrupted. Retry setup to continue."
                )
        return task_permissions(task)

    @app.post("/api/projects/pick-folder")
    async def pick_project_folder():
        if folder_picker_lock.locked():
            raise HTTPException(
                409, "Finish choosing a folder in the open picker first."
            )
        async with folder_picker_lock:
            return await asyncio.to_thread(repositories.pick_folder)

    @app.get("/api/projects/folders")
    async def browse_project_folders(path: str = ""):
        return await asyncio.to_thread(repositories.browse, path)

    @app.get("/api/projects/discovery")
    async def discover_projects():
        return await asyncio.to_thread(repositories.discover, store, team.projects())

    @app.post("/api/projects/discovery")
    async def set_projects_folder(body: NewProject):
        path = await asyncio.to_thread(repositories.directory, body.path)
        store.save_setting("projects_folder", str(path))
        return await asyncio.to_thread(repositories.discover, store, team.projects())

    @app.delete("/api/projects/discovery")
    async def forget_projects_folder():
        store.save_setting("projects_folder", "")
        return await asyncio.to_thread(repositories.discover, store, team.projects())

    @app.post("/api/projects/{identity}/open")
    async def open_project(identity: str):
        async with project_lock:
            project = store.get("projects", identity)
            if any(item["id"] == identity for item in team.projects(hidden=True)):
                raise HTTPException(409, "Add this repository again before opening it")
            project["last_opened"] = time.time()
            store.put("projects", project)
            team.preference(identity, opened=True)
            return project

    @app.post("/api/projects/remote")
    async def add_remote_project(body: RemoteProject):
        url = remote_repositories.canonical_url(body.url)
        async with project_lock:
            for project in store.all("projects"):
                if project.get("remote_url") == url:
                    team.require(project["id"])
                    team.preference(project["id"], hidden=False, opened=True)
                    return {**project, "role": team.role(project["id"])}
            project = await asyncio.to_thread(
                remote_repositories.connect, url, uuid.uuid4().hex
            )
            store.put("projects", project)
            team.own_project(project["id"])
            team.preference(project["id"], opened=True)
            team.audit(project["id"], "project.connected", detail={"remote_url": url})
            return {**project, "role": "owner"}

    @app.post("/api/projects/{identity}/sync")
    async def sync_project(identity: str):
        team.require(identity, "maintainer")
        async with project_lock:
            project = store.get("projects", identity)
            if not project.get("remote_url"):
                raise ValueError("This project uses a local checkout")
            project["synced_commit"] = await asyncio.to_thread(
                remote_repositories.sync, project
            )
            store.put("projects", project)
            team.audit(
                identity, "project.synced", detail={"commit": project["synced_commit"]}
            )
            return {**project, "role": team.role(identity)}

    @app.delete("/api/projects/{identity}")
    async def remove_project(identity: str):
        async with project_lock:
            store.get("projects", identity)
            # Keep the identity for workflows and restore its history when the
            # same canonical path is connected again. Never touch its files.
            team.preference(identity, hidden=True)
            return {"ok": True}

    @app.post("/api/projects")
    async def add_project(body: NewProject):
        team.require_admin()
        async with project_lock:
            return await connect_project(body)

    async def connect_project(body: NewProject):
        path = await asyncio.to_thread(repositories.directory, body.path)
        try:
            actual = (
                await asyncio.to_thread(
                    workspaces.git, path, "rev-parse", "--show-toplevel"
                )
            ).strip()
        except ValueError:
            raise ValueError(
                "This folder is not inside a Git repository. Choose a repository or one of its subfolders."
            ) from None
        actual = str(Path(actual).resolve())
        for item in store.all("projects"):
            if item["path"] == actual:
                team.require(item["id"])
                item["last_opened"] = time.time()
                item.pop("removed", None)
                store.put("projects", item)
                team.preference(item["id"], hidden=False, opened=True)
                return item
        remote = await asyncio.to_thread(workspaces.git, path, "remote", "-v")
        # Do not persist embedded HTTP credentials in remote URLs.
        from urllib.parse import urlsplit

        origin = ""
        for line in remote.splitlines():
            if line.startswith("origin\t"):
                candidate = line.split()[1]
                if candidate.startswith("git@github.com:"):
                    origin = "https://github.com/" + candidate.split(":", 1)[
                        1
                    ].removesuffix(".git")
                elif (
                    candidate.startswith("https://github.com/")
                    and not urlsplit(candidate).username
                ):
                    origin = candidate.removesuffix(".git")
                break
        project = {
            "id": uuid.uuid4().hex,
            "name": Path(actual).name,
            "path": actual,
            "github_url": origin,
            "last_opened": time.time(),
        }
        store.put("projects", project)
        team.own_project(project["id"])
        team.preference(project["id"], opened=True)
        team.audit(project["id"], "project.connected")
        return project

    @app.post("/api/demo")
    async def demo():
        path = await asyncio.to_thread(workspaces.demo_project)
        project = await add_project(NewProject(path=str(path)))
        project["demo"] = True
        store.put("projects", project)
        return project

    @app.post("/api/profiles")
    async def save_profile(body: SaveProfile):
        if body.provider not in registered_integrations():
            raise ValueError("Only OpenAI profiles can be added in this milestone")
        if body.provider == "compatible" and not body.base_url:
            raise ValueError("Compatible providers need a base URL")
        if body.base_url:
            from urllib.parse import urlsplit

            url = urlsplit(body.base_url)
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
            ):
                raise ValueError(
                    "Use an HTTP(S) provider URL without embedded credentials or query parameters"
                )
            if url.scheme == "http" and url.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Remote provider URLs must use HTTPS")
        identity = uuid.uuid4().hex
        credential = "env:" + body.env_var if body.env_var else ""
        if body.api_key:
            import keyring

            try:
                await asyncio.to_thread(
                    keyring.set_password, "sdlc-builder", identity, body.api_key
                )
            except Exception:
                raise ValueError(
                    "The OS keyring is unavailable. Use an environment variable reference instead."
                ) from None
            credential = "keyring:" + identity
        if not credential:
            raise ValueError("Provide an API key or environment variable name")
        profile = Profile(
            id=identity,
            label=body.label,
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            credential=credential,
            max_steps=body.max_steps,
        )
        store.put("profiles", profile.model_dump())
        return profile

    @app.post("/api/profiles/{identity}/test")
    async def test_profile(identity: str):
        from .provider_checks import check_profile

        if identity in removing_profiles:
            raise HTTPException(409, "Wait for this profile to finish being removed")
        profile = Profile.model_validate(store.get("profiles", identity))
        if profile.provider == "demo":
            raise ValueError("The demo profile does not connect to a provider")
        integration_for(profile)
        if identity in profile_checks:
            raise HTTPException(
                409, "A connection test for this profile is already running"
            )
        profile_checks.add(identity)
        try:
            return await check_profile(profile)
        finally:
            profile_checks.discard(identity)

    @app.delete("/api/profiles/{identity}")
    async def delete_profile(identity: str):
        if identity in removing_profiles:
            raise HTTPException(409, "This profile is already being removed")
        removing_profiles.add(identity)
        try:
            return await remove_profile(identity)
        finally:
            removing_profiles.discard(identity)

    async def remove_profile(identity: str):
        profile = store.get("profiles", identity)
        if identity in profile_checks:
            raise HTTPException(
                409, "Wait for the connection test before removing this profile"
            )
        if identity == "demo":
            raise ValueError("The built-in demo profile cannot be removed")
        for task in store.all("tasks"):
            turns = (
                (task.get("snapshot") or {})
                .get("conversation", {})
                .get("value", {})
                .get("turns", [])
            )
            active_profile = task.get("current_profile_id") or (
                turns[-1]["profile_id"] if turns else task.get("profile_id")
            )
            pending_profile = (
                task.get("pending_message", {})
                .get("payload", {})
                .get("profile", {})
                .get("id")
            )
            if pending_profile == identity:
                raise HTTPException(
                    409,
                    "Stop active tasks using this profile before removing its credential",
                )
            if active_profile == identity:
                async with task_locks.setdefault(task["id"], asyncio.Lock()):
                    try:
                        current = await get_task(task["id"])
                    except ValueError:  # Deleted while waiting for the task lock.
                        continue
                    if not current.get("can_message"):
                        raise HTTPException(
                            409,
                            "Stop active tasks using this profile before removing its credential; reconnect to Temporal if it is unavailable",
                        )
        if profile["credential"].startswith("keyring:"):
            import keyring

            try:
                await asyncio.to_thread(
                    keyring.delete_password, "sdlc-builder", profile["credential"][8:]
                )
            except keyring.errors.PasswordDeleteError:
                pass
            except Exception:
                raise ValueError(
                    "Could not remove the key from the OS keyring; the profile was kept"
                ) from None
        store.delete_profile(identity)
        return {"ok": True}

    @app.post("/api/tasks")
    async def create_task(body: NewTask):
        async with (
            project_workspace_locks.setdefault(body.project_id, asyncio.Lock()),
            task_locks.setdefault(body.id, asyncio.Lock()),
        ):
            previous = next((t for t in store.all("tasks") if t["id"] == body.id), None)
            if previous:
                team.require(previous["project_id"], "developer")
                if previous.get("dispatch") == "preparing":
                    raise ValueError(
                        "Workspace preparation was interrupted. Start a new task; the partial workspace is preserved for inspection."
                    )
                return previous
            project = store.get("projects", body.project_id)
            team.require(body.project_id, "developer")
            if team.enabled and body.engine != "v2":
                raise ValueError("Shared worktrees require the current task engine")
            if team.enabled and sandbox.configuration()["backend"] != "e2b":
                raise ValueError(
                    "Shared tasks require E2B workspaces. Set SDLC_WORKSPACE_BACKEND=e2b on the server."
                )
            if project.get("removed"):
                raise HTTPException(
                    409, "Add this repository again before starting a task"
                )
            if not sandbox.configuration()["ready"]:
                raise ValueError(
                    "Set E2B_API_KEY in the launch environment and restart boltzmann before starting a workspace."
                )
            if body.profile_id in removing_profiles:
                raise HTTPException(
                    409, "Wait for profile removal and select another profile"
                )
            profile = Profile.model_validate(store.get("profiles", body.profile_id))
            if profile.provider != "demo":
                integration_for(profile)
            if profile.provider == "demo" and not project.get("demo"):
                raise ValueError(
                    "The demo profile only operates on the demo repository. Configure a provider for your project."
                )
            prior = None
            if project.get("remote_url") and not body.parent_task_id:
                async with project_lock:
                    project["synced_commit"] = await asyncio.to_thread(
                        remote_repositories.sync, project
                    )
                    store.put("projects", project)
            if body.parent_task_id:
                parent = store.get("tasks", body.parent_task_id)
                team.require(parent["project_id"], "developer")
                if team.enabled and parent.get("workspace_backend") != "e2b":
                    raise HTTPException(
                        409,
                        "Start a new E2B task instead of forking a legacy local workspace",
                    )
                prior = await get_task(body.parent_task_id)
                prior_state = (prior.get("snapshot") or {}).get("value", {})
                if (
                    prior["project_id"] != project["id"]
                    or prior_state.get("status")
                    not in {"review", "accepted", "failed", "cancelled"}
                    or not prior.get("can_message")
                ):
                    raise ValueError(
                        "Continue from a finished task in the same repository"
                    )
            task_input = TaskInput(
                task_id=body.id,
                prompt=body.prompt,
                mode=body.mode,
                profile=profile,
                project_coordination=team.enabled,
            )
            if prior:
                task_input.continuation_context = (
                    "Prior request: "
                    + prior["title"]
                    + "\nPrior outcome: "
                    + prior_state.get("outcome", "")
                )[:8000]
            task = {
                "id": body.id,
                "workflow_id": "sdlc-" + body.id,
                "engine": body.engine,
                "project_id": project["id"],
                "project_name": project["name"],
                "created_by": user_identity(team.actor()),
                **(
                    {
                        "workspace_layout": "project-worktree",
                        "owner": user_identity(team.actor()),
                        "ownership_version": 1,
                    }
                    if team.enabled
                    else {}
                ),
                "workspace_backend": sandbox.configuration()["backend"],
                "title": body.prompt[:90],
                "mode": body.mode,
                "profile_id": profile.id,
                "created_at": time.time(),
                "dispatch": "preparing",
                "parent_task_id": body.parent_task_id,
                "input": task_input.model_dump(),
            }
            store.put("tasks", task)
            team.audit(project["id"], "task.created", task_id=task["id"])
            try:
                if prior:
                    async with task_locks.setdefault(prior["id"], asyncio.Lock()):
                        current_parent = await get_task(prior["id"])
                        if not current_parent.get("can_message"):
                            raise ValueError(
                                "Finish or stop the parent task before forking its workspace"
                            )
                        prepared = await asyncio.to_thread(
                            workspaces.continue_workspace, prior["id"], body.id
                        )
                else:
                    prepared = await asyncio.to_thread(
                        workspaces.prepare_workspace, project["path"], body.id
                    )
            except Exception as error:
                detail = (
                    str(error)
                    if isinstance(error, (ValueError, OSError))
                    else f"Workspace preparation failed ({type(error).__name__}). Retry setup to check its saved state."
                )
                task.update(dispatch="preparing", preparation_error=detail)
                store.put("tasks", task)
                raise ValueError(detail) from None
            task.update(prepared, dispatch="pending")
            store.put("tasks", task)
            return task

    @app.post("/api/tasks/{identity}/retry-setup")
    async def retry_setup(identity: str):
        lock = task_locks.setdefault(identity, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "Workspace setup is already in progress")
        async with lock:
            task = store.get("tasks", identity)
            require_writer(task)
            if task.get("dispatch") != "preparing":
                return task  # A previous retry succeeded; never initialize twice.
            project = store.get("projects", task["project_id"])
            if project.get("removed"):
                raise HTTPException(
                    409, "Add this repository again before retrying setup"
                )
            task.pop("preparation_error", None)
            store.put("tasks", task)
            try:
                if sandbox.is_remote(workspace(identity)):
                    prepared = await asyncio.to_thread(sandbox.retry_preparation, task)
                elif task.get("parent_task_id"):
                    parent_id = task["parent_task_id"]
                    async with task_locks.setdefault(parent_id, asyncio.Lock()):
                        parent = await get_task(parent_id)
                        if not parent.get("can_message"):
                            raise ValueError(
                                "Finish or stop the parent task before retrying its workspace"
                            )
                        prepared = await asyncio.to_thread(
                            workspaces.continue_workspace, parent_id, identity
                        )
                elif task["input"].get("continuation_context"):
                    raise ValueError(
                        "Start a new task from the original parent; this older task has no saved parent reference"
                    )
                else:
                    prepared = await asyncio.to_thread(
                        workspaces.prepare_workspace, project["path"], identity
                    )
            except Exception as error:
                detail = (
                    str(error)
                    if isinstance(error, (ValueError, OSError))
                    else f"Workspace preparation failed ({type(error).__name__}). Retry setup to check its saved state."
                )
                task["preparation_error"] = detail
                store.put("tasks", task)
                raise ValueError(detail) from None
            task.update(prepared, dispatch="pending")
            store.put("tasks", task)
            return task

    @app.get("/api/tasks/{identity}")
    async def get_task(identity: str):
        task = store.get("tasks", identity)
        if task.get("dispatch") == "preparing":
            return {**preparation_state(task), "live": False, "can_message": False}
        try:
            envelope = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).query("progress", rpc_timeout=timedelta(seconds=3))
            store.archive(identity, envelope)
            task = store.get("tasks", identity)
            task["live"] = True
            task["can_message"] = bool(envelope.get("can_message")) and not task.get(
                "pending_message"
            )
            task["next_turn"] = envelope.get("next_turn", 2)
        except Exception:
            task["live"] = False
            task["can_message"] = False
        return task_permissions(task)

    @app.get("/api/tasks/{identity}/coordination")
    async def task_coordination(identity: str):
        store.get("tasks", identity)
        return await asyncio.to_thread(coordination.board, identity)

    @app.post("/api/tasks/{identity}/coordination")
    async def post_coordination(identity: str, body: CoordinationNote):
        task = store.get("tasks", identity)
        team.require(task["project_id"], "developer")
        target = store.get("tasks", body.target_id)
        if target["project_id"] != task["project_id"]:
            raise HTTPException(404, "Target task not found in this project")
        await asyncio.to_thread(
            coordination.post,
            identity,
            body.target_id,
            body.message,
            identity + ":human:" + body.id,
            user_identity(team.actor()),
        )
        return {"ok": True}

    async def require_idle(task: dict):
        if task.get("pending_message"):
            raise HTTPException(
                409,
                "Wait for the pending message before changing workspace ownership or recovery",
            )
        if task.get("dispatch") == "preparing":
            return
        current = await get_task(task["id"])
        status = (current.get("snapshot") or {}).get("value", {}).get("status")
        if not current.get("live"):
            raise HTTPException(
                503, "Reconnect to the worker so all tasks can be confirmed stopped"
            )
        if not current.get("can_message") or status not in {
            "review",
            "accepted",
            "failed",
            "cancelled",
        }:
            raise HTTPException(
                409,
                "Finish or stop this task and wait for its current action before handing off or recovering",
            )

    @app.post("/api/tasks/{identity}/handoff")
    async def handoff(identity: str, body: Handoff):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            role = team.require(task["project_id"], "developer")
            if task.get("workspace_layout") != "project-worktree":
                raise HTTPException(409, "Handoffs require a shared project worktree")
            require_available(task)
            if (task.get("owner") or {}).get("id") != team.actor()[
                "id"
            ] and role not in {"maintainer", "owner"}:
                raise HTTPException(
                    403,
                    "Only the task owner or a project maintainer can hand off this task",
                )
            if task.get("ownership_version", 1) != body.expected_ownership_version:
                raise HTTPException(
                    409, "Task ownership changed. Refresh before handing it off"
                )
            member = next(
                (
                    m
                    for m in team.members(task["project_id"])
                    if m["id"] == body.user_id
                ),
                None,
            )
            if not member or member["role"] not in {"developer", "maintainer", "owner"}:
                raise HTTPException(
                    400, "Choose a current project developer, maintainer, or owner"
                )
            await require_idle(task)
            with writer_guard(identity):
                if previews.saved(workspace(identity)):
                    await preview_operation(previews.stop, workspace(identity))
                if (
                    project_workspaces.shared(workspace(identity))
                    and project_workspaces.task_meta(workspace(identity)).get("status")
                    == "ready"
                ):
                    await asyncio.to_thread(
                        project_workspaces.checkpoint, workspace(identity)
                    )
                # Store owner and audit atomically; no started agent or queued follow-up.
                with store.connect() as db:
                    import json

                    row = db.execute(
                        "SELECT value FROM tasks WHERE id=?", (identity,)
                    ).fetchone()
                    value = json.loads(row["value"])
                    previous = value.get("owner")
                    value.update(
                        owner=user_identity(member),
                        ownership_version=value.get("ownership_version", 1) + 1,
                    )
                    db.execute(
                        "UPDATE tasks SET value=?,updated=? WHERE id=?",
                        (json.dumps(value), time.time(), identity),
                    )
                    db.execute(
                        "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
                        (
                            uuid.uuid4().hex,
                            task["project_id"],
                            identity,
                            json.dumps(user_identity(team.actor())),
                            "task.handoff",
                            json.dumps({"from": previous, "to": value["owner"]}),
                            "completed",
                            time.time(),
                        ),
                    )
            return task_permissions(store.get("tasks", identity))

    @app.get("/api/projects/{project_id}/sandbox")
    async def project_sandbox(project_id: str):
        team.require(project_id)
        state = project_workspaces.project_state(project_id)
        return {
            "status": state.get("status", "not_created"),
            "sandbox_id": state.get("sandbox_id", ""),
            "can_recover": team.role(project_id) in {"maintainer", "owner"},
            "tasks": [
                {
                    "id": identity,
                    "checkpoint_at": project_workspaces.checkpoint_info(identity).get(
                        "saved_at"
                    ),
                }
                for identity in state.get("tasks", {})
            ],
        }

    @app.post("/api/projects/{project_id}/sandbox/recover")
    async def recover_project_sandbox(project_id: str):
        team.require(project_id, "maintainer")
        async with (
            project_workspace_locks.setdefault(project_id, asyncio.Lock()),
            contextlib.AsyncExitStack() as locks,
        ):
            state = project_workspaces.project_state(project_id)
            if not state:
                raise HTTPException(409, "This project has no shared sandbox")
            tasks = {
                task["id"]: task
                for task in store.all("tasks")
                if task["project_id"] == project_id
                and task.get("workspace_layout") == "project-worktree"
            }
            for identity in sorted(set(state["tasks"]) | set(tasks)):
                await locks.enter_async_context(
                    task_locks.setdefault(identity, asyncio.Lock())
                )
            for task in tasks.values():
                await require_idle(store.get("tasks", task["id"]))
            for identity in state["tasks"]:
                locks.enter_context(
                    project_workspaces.writer_lease(
                        identity, "recovery:" + team.actor()["id"]
                    )
                )
            event = team.audit(project_id, "sandbox.recover", status="pending")
            try:
                # Capture the freshest code when the VM is reachable. A missing VM
                # restores its saved checkpoint; ambiguous transport failures stop.
                if state.get("status") == "ready":
                    from e2b import NotFoundException

                    for identity in state["tasks"]:
                        root = workspace(identity)
                        if project_workspaces.task_meta(root).get("status") != "ready":
                            continue
                        try:
                            if previews.saved(root):
                                await asyncio.to_thread(previews.stop, root)
                            await asyncio.to_thread(project_workspaces.checkpoint, root)
                        except NotFoundException:
                            break
                restored = await asyncio.to_thread(
                    project_workspaces.recover, project_id
                )
                for recovered in restored["tasks"]:
                    identity = recovered["id"]
                    if identity not in tasks:  # retained worktree from a deleted task
                        continue
                    task = store.get("tasks", identity)
                    if task.get("dispatch") != "preparing":
                        result = await app.state.client.get_workflow_handle(
                            task["workflow_id"]
                        ).execute_update(
                            "control", Control(command="workspace_restored")
                        )
                        if not result["ok"]:
                            raise HTTPException(409, result["error"])
                    task.update(recovered)
                    store.put("tasks", task)
                await asyncio.to_thread(project_workspaces.finish_recovery, project_id)
            except Exception as error:
                team.finish_audit(event, "unconfirmed")
                if isinstance(error, (HTTPException, ValueError)):
                    raise
                raise HTTPException(
                    503,
                    f"Sandbox recovery could not finish ({type(error).__name__}). Check connectivity and retry recovery; saved checkpoints were kept.",
                ) from None
            team.finish_audit(event, "completed")
            return {"ok": True, "sandbox_id": restored["sandbox_id"]}

    @app.post("/api/tasks/{identity}/messages")
    async def send_message(identity: str, body: NewMessage):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            require_writer(task)
            fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
            previous = task.get("message_receipts", {}).get(body.id)
            pending = task.get("pending_message")
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise HTTPException(
                        409, "This message ID was already used for different content"
                    )
                return previous
            if pending:
                if pending["id"] != body.id:
                    raise HTTPException(
                        409, "Wait for the pending message to be delivered"
                    )
                if pending["request_hash"] != fingerprint:
                    raise HTTPException(
                        409, "This message ID was already used for different content"
                    )
                return await dispatch_followup(app, task)
            if not body.prompt.strip():
                raise HTTPException(422, "Write a message before sending")
            current = await get_task(identity)
            if not current["live"]:
                raise HTTPException(
                    503, "Reconnect boltzmann to Temporal before sending a message"
                )
            if not current["can_message"]:
                raise HTTPException(
                    409, "Finish or stop the current message before sending a follow-up"
                )
            if body.expected_turn != current["next_turn"]:
                raise HTTPException(
                    409,
                    "Another message was sent from this task. Refresh and review it before sending again",
                )
            if body.profile_id in removing_profiles:
                raise HTTPException(
                    409, "Wait for profile removal and select another profile"
                )
            profile = Profile.model_validate(store.get("profiles", body.profile_id))
            if profile.provider != "demo":
                integration_for(profile)
            elif not store.get("projects", task["project_id"]).get("demo"):
                raise ValueError(
                    "The demo profile only operates on the demo repository"
                )
            profile = profile.model_copy(update={"max_steps": body.max_steps})
            payload = FollowUpInput(
                message_id=body.id,
                prompt=body.prompt,
                mode=body.mode,
                profile=profile,
            )
            task["pending_message"] = {
                "id": body.id,
                "request_hash": fingerprint,
                "expected_turn": body.expected_turn,
                "payload": payload.model_dump(),
                "audit_id": team.audit(
                    task["project_id"],
                    "task.message",
                    task_id=identity,
                    detail={"message_id": body.id, "expected_turn": body.expected_turn},
                    status="pending",
                ),
            }
            task.pop("message_error", None)
            # Persist before delivery. Reconciliation retries the same Temporal
            # update ID after a disconnect/restart, including a lost receipt.
            store.put("tasks", task)
            return await dispatch_followup(app, task)

    @app.post("/api/tasks/{identity}/control")
    async def control(identity: str, body: HttpControl):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            team.require(
                task["project_id"],
                "maintainer" if body.command == "accept" else "developer",
            )
            require_available(task)
            if body.command == "workspace_restored":
                raise HTTPException(403, "Use Recover sandbox to restore a workspace")
            if body.command != "accept" and not (
                body.command == "cancel"
                and team.role(task["project_id"]) in {"maintainer", "owner"}
            ):
                require_writer(task)
            if team.enabled and body.command == "accept":
                task = await get_task(identity)
                if not task["live"]:
                    raise HTTPException(
                        503, "Reconnect to the worker before accepting changes"
                    )
                if (
                    body.expected_version is None
                    or body.expected_version != task["version"]
                ):
                    raise HTTPException(
                        409,
                        "This task changed since you opened the decision. Review the latest changes and accept again.",
                    )
            if task.get("pending_message"):
                raise HTTPException(
                    409, "Wait for message delivery before using task controls"
                )
            event = team.audit(
                task["project_id"],
                "task." + body.command,
                task_id=identity,
                detail={
                    "gate_id": body.gate_id,
                    "approved": body.approved,
                    "revision": (task.get("snapshot") or {})
                    .get("value", {})
                    .get("revision", ""),
                },
                status="pending",
            )
            try:
                result = await app.state.client.get_workflow_handle(
                    task["workflow_id"]
                ).execute_update("control", Control.model_validate(body.model_dump()))
            except Exception:
                team.finish_audit(event, "unconfirmed")
                raise
            team.finish_audit(event, "completed" if result["ok"] else "rejected")
            if not result["ok"]:
                raise HTTPException(409, result["error"])
            return result

    @app.delete("/api/tasks/{identity}")
    async def delete_task(identity: str, body: DeleteTask = DeleteTask()):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            if task.get("pending_message"):
                raise HTTPException(
                    409,
                    "Wait for message delivery, then stop the task before deleting it",
                )
            # Never trust the catalogue's path for a destructive operation.
            task_workspace = workspace(identity)
            if task.get("dispatch") != "preparing":
                handle = app.state.client.get_workflow_handle(task["workflow_id"])
                try:
                    state = await handle.query(
                        "progress", rpc_timeout=timedelta(seconds=3)
                    )
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise HTTPException(
                            503,
                            "Could not verify that this task has stopped. Check Temporal and try deleting again; the task and workspace were kept.",
                        ) from None
                    # No execution remains (including a previous deletion whose
                    # filesystem cleanup failed). A fresh pending dispatch must
                    # first be resolved by the collector.
                    if task.get("dispatch") != "submitted":
                        raise HTTPException(
                            409,
                            "Wait for task dispatch, then stop the task before deleting it",
                        ) from None
                except Exception:
                    raise HTTPException(
                        503,
                        "Could not verify that this task has stopped. Check Temporal and try deleting again; the task and workspace were kept.",
                    ) from None
                else:
                    if state.get("can_message") is False or state["value"].get(
                        "status"
                    ) not in {
                        "review",
                        "accepted",
                        "failed",
                        "cancelled",
                    }:
                        raise HTTPException(
                            409,
                            "Stop this task and wait for its current action to finish before deleting it",
                        )
                    store.archive(identity, state)
                try:
                    await handle.terminate(
                        reason="Deleted in boltzmann",
                        rpc_timeout=timedelta(seconds=3),
                    )
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise HTTPException(
                            503,
                            "Could not close this task in Temporal. Try deleting again; the task and workspace were kept.",
                        ) from None
                except Exception:
                    raise HTTPException(
                        503,
                        "Could not close this task in Temporal. Try deleting again; the task and workspace were kept.",
                    ) from None
            try:
                if (
                    not body.remove_workspace
                    or project_workspaces.shared(task_workspace)
                ) and previews.saved(task_workspace):
                    await preview_operation(previews.stop, task_workspace)
                await asyncio.to_thread(
                    sandbox.release, task_workspace, remove=body.remove_workspace
                )
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            if body.remove_workspace:
                try:
                    if task_workspace.exists():
                        await asyncio.to_thread(shutil.rmtree, task_workspace)
                except OSError:
                    raise HTTPException(
                        409,
                        "The task is stopped, but its workspace could not be fully removed. Check file permissions and retry, or delete with the workspace option unchecked to keep the remaining files.",
                    ) from None
            previews.forget(task_workspace)
            store.delete_task(identity)
            return {
                "ok": True,
                "workspace_removed": body.remove_workspace,
                "workspace": task.get("workspace", str(task_workspace)),
            }

    async def preview_operation(operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except ValueError:
            raise
        except Exception:
            raise HTTPException(
                503,
                "Could not reach the sandbox preview. Refresh its status or check your E2B connection.",
            ) from None

    @app.get("/api/tasks/{identity}/preview")
    async def get_preview(identity: str):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            root = workspace(identity)
            result = await preview_operation(previews.inspect, root)
            preview_state = previews.saved(root)
            if (
                not preview_state.get("checkpointed")
                and task.get("workspace_layout") == "project-worktree"
                and result.get("status") in {"running", "stopped"}
            ):
                try:
                    with writer_guard(identity):
                        await asyncio.to_thread(project_workspaces.checkpoint, root)
                        await asyncio.to_thread(
                            previews.mark_checkpointed,
                            root,
                            preview_state.get("run_id", ""),
                        )
                except ValueError as error:
                    # Preview installation can briefly still own the remote lock.
                    # Check again on the next poll; never label an unsaved revision saved.
                    if "active writer" not in str(error) and "in-flight" not in str(
                        error
                    ):
                        result["checkpoint_error"] = str(error)
                except Exception as error:
                    result["checkpoint_error"] = (
                        "Code checkpoint unavailable ("
                        + type(error).__name__
                        + "). Retry status before closing the preview."
                    )
            return result

    @app.post("/api/tasks/{identity}/preview")
    async def start_preview(identity: str, body: StartPreview):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = await get_task(identity)
            require_writer(task)
            if task.get("workspace_layout") == "project-worktree" and (
                not task.get("live") or not task.get("can_message")
            ):
                raise HTTPException(
                    409,
                    "Finish or stop the agent before starting the preview manually. The agent can verify it while working.",
                )
            with writer_guard(identity):
                return await preview_operation(
                    previews.start,
                    workspace(identity),
                    body.command,
                    body.cwd,
                    body.port,
                )

    @app.delete("/api/tasks/{identity}/preview")
    async def stop_preview(identity: str):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            require_writer(task)
            with writer_guard(identity):
                result = await preview_operation(previews.stop, workspace(identity))
                if project_workspaces.shared(workspace(identity)):
                    await asyncio.to_thread(
                        project_workspaces.checkpoint, workspace(identity)
                    )
                return result

    @app.get("/api/tasks/{identity}/files")
    async def list_files(identity: str):
        store.get("tasks", identity)
        return await asyncio.to_thread(workspaces.files, workspace(identity))

    @app.get("/api/tasks/{identity}/file")
    async def get_file(identity: str, path: str):
        store.get("tasks", identity)
        return await asyncio.to_thread(workspaces.read_file, workspace(identity), path)

    @app.put("/api/tasks/{identity}/file")
    async def edit_file(identity: str, body: FileEdit):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = await get_task(identity)
            require_writer(task)
            state = (task.get("snapshot") or {}).get("value", {})
            if (
                task.get("pending_message")
                or (state.get("status") != "paused" and not task.get("can_message"))
                or not task["live"]
                or state.get("status")
                not in {
                    "paused",
                    "review",
                    "failed",
                    "cancelled",
                }
            ):
                raise HTTPException(
                    409, "Pause the agent at an action boundary before editing"
                )
            handle = app.state.client.get_workflow_handle(task["workflow_id"])
            invalidated = await handle.execute_update(
                "control", Control(command="edit_started")
            )
            if not invalidated["ok"]:
                raise HTTPException(409, invalidated["error"])
            with writer_guard(identity):
                result = await asyncio.to_thread(
                    workspaces.write_file,
                    workspace(identity),
                    body.path,
                    body.content,
                    body.expected_hash,
                )
            await handle.execute_update(
                "control", Control(command="edit_finished", text=body.path)
            )
            return result

    @app.get("/api/tasks/{identity}/diff")
    async def get_diff(identity: str):
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            workspaces.diff, workspace(identity), reviews.base_revision(task)
        )

    @app.get("/api/tasks/{identity}/review")
    async def get_review(identity: str):
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            reviews.summary,
            workspace(identity),
            task,
            team.review_view(store.review(identity)),
        )

    @app.get("/api/tasks/{identity}/review/file")
    async def review_file(identity: str, path: str, comparison: str = "all"):
        if comparison not in {"all", "reviewed"}:
            raise HTTPException(422, "Unknown review comparison")
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            reviews.detail,
            workspace(identity),
            task,
            team.review_view(store.review(identity)),
            path,
            comparison,
        )

    @app.put("/api/tasks/{identity}/review/checkpoint")
    async def review_checkpoint(identity: str, body: reviews.ReviewFile):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            review = store.review(identity)
            personal = team.review_view(review)
            await asyncio.to_thread(
                reviews.checkpoint, workspace(identity), task, personal, body
            )
            team.save_checkpoints(review, personal["checkpoints"])
            store.save_review(identity, review)
            return {"ok": True}

    @app.post("/api/tasks/{identity}/review/comments")
    async def add_review_comment(identity: str, body: reviews.AddComment):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            review = store.review(identity)
            if await asyncio.to_thread(
                reviews.add_comment,
                workspace(identity),
                task,
                team.review_view(review),
                body,
                user_identity(team.actor()),
            ):
                store.save_review(identity, review)
            return {"ok": True}

    @app.put("/api/tasks/{identity}/review/comments/{comment_id}")
    async def resolve_review_comment(
        identity: str, comment_id: str, body: reviews.ResolveComment
    ):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            store.get("tasks", identity)
            review = store.review(identity)
            comment = next(
                (c for c in review["comments"] if c["id"] == comment_id), None
            )
            if comment is None:
                raise HTTPException(404, "Review comment not found")
            if comment["resolved"] != body.resolved:
                comment["resolved"] = body.resolved
                comment["resolved_by"] = (
                    user_identity(team.actor()) if body.resolved else None
                )
                store.save_review(identity, review)
            return {"ok": True}

    @app.post("/api/tasks/{identity}/review/fix-request")
    async def prepare_review_fixes(identity: str, body: reviews.FixRequest):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            store.get("tasks", identity)
            return {"prompt": reviews.fix_prompt(store.review(identity), body)}

    @app.post("/api/tasks/{identity}/merge")
    async def merge_task_into_project(
        identity: str, body: DecisionVersion = DecisionVersion()
    ):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = await get_task(identity)
            team.require(task["project_id"], "maintainer")
            require_available(task)
            if not task.get("live"):
                raise HTTPException(
                    503,
                    "Reconnect boltzmann to Temporal before merging this task",
                )
            if team.enabled and (
                body.expected_version is None
                or body.expected_version != task["version"]
            ):
                raise HTTPException(
                    409,
                    "This task changed since you opened the merge decision. Review it again before merging.",
                )
            if task.get("pending_message"):
                raise HTTPException(
                    409, "Wait for message delivery before merging this task"
                )
            if task.get("engine") != "v2":
                raise HTTPException(
                    409,
                    "Workflow-managed merge is available for Coding V2 tasks. Export this older task's patch instead.",
                )
            handle = app.state.client.get_workflow_handle(task["workflow_id"])
            event = team.audit(
                task["project_id"],
                "task.merge",
                task_id=identity,
                detail={
                    "revision": (task.get("snapshot") or {})
                    .get("value", {})
                    .get("revision", "")
                },
                status="pending",
            )
            try:
                result = await handle.execute_update("merge_project")
            except Exception:
                team.finish_audit(event, "unconfirmed")
                raise
            team.finish_audit(event, "completed" if result["ok"] else "rejected")
            if not result["ok"]:
                raise HTTPException(409, result["error"])
            # Make the workflow-owned receipt visible immediately when possible.
            # A failed refresh does not turn an already-completed merge into an error.
            try:
                store.archive(
                    identity,
                    await handle.query("progress", rpc_timeout=timedelta(seconds=3)),
                )
            except Exception:
                pass
            return result

    @app.get("/api/tasks/{identity}/patch")
    async def export_patch(identity: str):
        value = await get_diff(identity)
        if value["truncated"]:
            raise HTTPException(
                413, "Diff exceeds export limit; use Git in the workspace"
            )
        return Response(
            value["patch"],
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="sdlc-{identity[:8]}.patch"'
            },
        )

    @app.get("/harness/api/sessions")
    async def harness_sessions():
        return [
            {
                "workflow_id": t["workflow_id"],
                "created_at": t["created_at"],
                "label": t["title"],
                "agent_workflow_type": WORKFLOW_NAME_V2
                if t.get("engine") == "v2"
                else WORKFLOW_NAME,
                "is_message_queuing_enabled": False,
                "is_discovered": True,
            }
            for t in store.all("tasks")
            if t.get("dispatch") == "submitted"
        ]

    app.mount("/harness", harness)
    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (static / "index.html").exists():
            raise HTTPException(
                503,
                "Build the app UI first: cd apps/sdlc/ui && pnpm install && pnpm build",
            )
        return FileResponse(static / "index.html")

    return app
