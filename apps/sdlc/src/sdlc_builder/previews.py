"""User-started web previews in an existing E2B workspace."""

import asyncio
import fcntl
import functools
import hashlib
import json
import re
import shlex
import time
import uuid
from pathlib import Path

import httpx

from . import preview_runtime, sandbox, workspaces
from .store import data_dir


def descriptor(root: Path) -> Path:
    return data_dir() / "previews" / f"{root.name}.json"


def saved(root: Path) -> dict:
    path = descriptor(root)
    return json.loads(path.read_text()) if path.exists() else {}


def _save(root: Path, value: dict):
    path = descriptor(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def forget(root: Path):
    descriptor(root).unlink(missing_ok=True)


def serialized(operation):
    @functools.wraps(operation)
    def wrapped(root, *args, **kwargs):
        path = descriptor(root).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return operation(root, *args, **kwargs)

    return wrapped


def _public(value: dict) -> dict:
    value = {**value, "launch_unconfirmed": bool(value.get("uncertain"))}
    return {
        key: item
        for key, item in value.items()
        if key
        in {
            "supported",
            "command",
            "cwd",
            "port",
            "status",
            "url",
            "logs",
            "exit_code",
            "launch_unconfirmed",
            "phase",
            "detail",
            "http_status",
            "healthy",
            "automatic",
            "detected",
            "framework",
        }
    }


def _client(root: Path):
    if not sandbox.is_remote(root):
        raise ValueError("Live previews require an E2B task workspace")
    meta = json.loads(sandbox.descriptor(root).read_text())
    if meta.get("status") != "ready":
        raise ValueError("Wait for the sandbox workspace to finish preparing")
    return sandbox._client(meta)


def _paths(value: dict) -> tuple[str, str]:
    return value["script"], f"{sandbox.CONTROL_ROOT}/preview-{value['run_id']}.json"


def _operation(client, value: dict, operation: str) -> dict:
    script, config = _paths(value)
    result = client.commands.run(
        shlex.join(["python3", "-I", script, operation, config]), timeout=12
    )
    reply = json.loads(result.stdout)
    if "error" in reply:
        raise ValueError(reply["error"])
    return reply


def suggestions(root: Path) -> dict:
    if not sandbox.is_remote(root):
        return {"supported": False, "command": "", "cwd": ".", "port": 3000}
    names = workspaces.files(root)
    candidates = sorted(
        (name for name in names if Path(name).name == "package.json"),
        key=lambda name: (len(Path(name).parts), name),
    )[:12]
    packages = {}
    for name in candidates:
        try:
            packages[name] = json.loads(workspaces.read_file(root, name)["content"])
        except (ValueError, TypeError):
            continue
    for name, package in packages.items():
        try:
            scripts = package.get("scripts", {})
            script = (
                "dev" if "dev" in scripts else "start" if "start" in scripts else ""
            )
            if not script:
                continue
            directory = str(Path(name).parent)
            manager, version, install_cwd = "npm", "", directory
            ancestors = [Path(directory), *Path(directory).parents]
            # A workspace root owns its package manager and dependency install.
            for parent in reversed(ancestors):
                manifest = packages.get(str(parent / "package.json"), {})
                locks = [
                    (lock, tool)
                    for lock, tool in [
                        ("pnpm-lock.yaml", "pnpm"),
                        ("yarn.lock", "yarn"),
                        ("bun.lock", "bun"),
                        ("bun.lockb", "bun"),
                        ("package-lock.json", "npm"),
                    ]
                    if str(parent / lock) in names
                ]
                pin = re.fullmatch(
                    r"(npm|pnpm|yarn|bun)@(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?)(?:\+sha\d+\.[a-zA-Z0-9]+)?",
                    manifest.get("packageManager", ""),
                )
                if locks or pin:
                    manager, version = (pin[1], pin[2]) if pin else (locks[0][1], "")
                    install_cwd = str(parent)
                    break
            content = scripts[script]
            framework = (
                "Vite"
                if "vite" in content
                else "Next.js"
                if "next" in content
                else "Astro"
                if "astro" in content
                else "Web app"
            )
            port, flags = (
                (5173, ' --host 0.0.0.0 --port "$PORT" --strictPort')
                if "vite" in content
                else (3000, ' --hostname 0.0.0.0 --port "$PORT"')
                if "next" in content
                else (4321, ' --host 0.0.0.0 --port "$PORT"')
                if "astro" in content
                else (3000, "")
            )
            separator = " --" if manager == "npm" and flags else ""
            return {
                "supported": True,
                "command": f"{manager} run {shlex.quote(script)}{separator}{flags}",
                "cwd": directory,
                "port": port,
                "detected": True,
                "framework": framework,
                "setup": {
                    "manager": manager,
                    "version": version,
                    "install_cwd": install_cwd,
                },
            }
        except (ValueError, TypeError, AttributeError):
            continue
    if "index.html" in names:
        return {
            "supported": True,
            "detected": True,
            "framework": "Static site",
            "command": 'python3 -m http.server "$PORT" --bind 0.0.0.0',
            "cwd": ".",
            "port": 3000,
        }
    return {
        "supported": True,
        "detected": False,
        "command": "",
        "cwd": ".",
        "port": 3000,
    }


def inspect(root: Path) -> dict:
    value = saved(root)
    if not value:
        return _public(
            {
                **suggestions(root),
                "automatic": True,
                "status": "idle",
                "url": "",
                "logs": "",
            }
        )
    if value.get("stopped"):
        return _public({**value, "supported": True, "status": "stopped", "url": ""})
    client = _client(root)
    client.set_timeout(sandbox.IDLE_TIMEOUT)
    result = _operation(client, value, "status")
    return _public({**value, **result, "supported": True})


@serialized
def start(
    root: Path, command: str = "", cwd: str | None = None, port: int | None = None
) -> dict:
    automatic = not command.strip()
    plan = suggestions(root) if automatic else {}
    if automatic:
        if not plan.get("supported"):
            raise ValueError("Live previews require an E2B task workspace")
        if not plan.get("detected"):
            raise ValueError(
                "No web app was detected. Ask the agent to configure the app's dev script, or use Advanced settings."
            )
        command = plan["command"]
    cwd = cwd if cwd is not None else plan.get("cwd", ".")
    port = port if port is not None else plan.get("port", 3000)
    command, cwd = command.strip(), cwd.strip() or "."
    if not command or len(command) > 4000 or "\0" in command:
        raise ValueError("Enter a start command of at most 4,000 characters")
    if Path(cwd).is_absolute() or ".." in Path(cwd).parts or "\0" in cwd:
        raise ValueError("App directory must be a relative workspace path")
    if not 1024 <= port <= 65535 or port in {49983, 49999}:
        raise ValueError("Use an application port between 1024 and 65535")
    client = _client(root)
    # Direct browser previews cannot attach the E2B traffic authentication header.
    if getattr(client, "traffic_access_token", None):
        raise ValueError(
            "This sandbox requires authenticated network access. Direct browser previews are unavailable for it."
        )
    previous = saved(root)
    if previous and not previous.get("stopped"):
        state = _operation(client, previous, "status")
        if state["status"] in {"starting", "running"}:
            raise ValueError("Stop the current preview before starting another")
        # A lost start response must not cause another untracked server to start.
        if previous.get("uncertain"):
            raise ValueError(
                "The previous preview launch is unconfirmed. Check its logs and stop it before trying again."
            )
    host = client.get_host(port)
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        raise ValueError("E2B returned an invalid preview hostname")
    source = Path(preview_runtime.__file__).read_text()
    script = f"{sandbox.CONTROL_ROOT}/preview-runtime-{hashlib.sha256(source.encode()).hexdigest()[:16]}.py"
    value = {
        "run_id": uuid.uuid4().hex,
        "command": command,
        "cwd": cwd,
        "port": port,
        "script": script,
        "url": f"https://{host}",
        "started_at": time.time(),
        "uncertain": True,
        "automatic": automatic,
        "framework": plan.get("framework", "Custom command"),
        "detected": plan.get("detected", True),
    }
    _, config = _paths(value)
    client.files.write(script, source)
    client.files.write(
        config,
        json.dumps(
            {
                "root": sandbox.REMOTE_ROOT,
                "command": command,
                "cwd": cwd,
                "port": port,
                "host": host,
                "setup": plan.get("setup"),
            }
        ),
    )
    _save(root, value)
    client.set_timeout(sandbox.IDLE_TIMEOUT)
    try:
        handle = client.commands.run(
            shlex.join(["python3", "-I", script, "run", config]),
            background=True,
            timeout=0,
        )
        value["pid"] = handle.pid
        value["uncertain"] = False
        _save(root, value)
        handle.disconnect()
    except Exception as error:
        raise ValueError(
            f"Preview launch could not be confirmed ({type(error).__name__}). Refresh its status before trying again."
        ) from None
    return _public(
        {
            **value,
            "supported": True,
            "status": "starting",
            "phase": "tooling" if plan.get("setup") else "starting",
            "logs": "",
        }
    )


@serialized
def stop(root: Path) -> dict:
    value = saved(root)
    if not value:
        return {
            "supported": sandbox.is_remote(root),
            "status": "idle",
            "url": "",
            "logs": "",
        }
    if value.get("stopped"):
        return _public({**value, "supported": True, "status": "stopped", "url": ""})
    result = _operation(_client(root), value, "stop")
    value.update(
        stopped=True,
        uncertain=False,
        logs=result["logs"],
        exit_code=result.get("exit_code"),
    )
    _save(root, value)
    return _public({**value, **result, "supported": True, "url": ""})


async def verify(root: Path, *, timeout: float = 570, restart: bool = False) -> dict:
    """Prepare and HTTP-smoke-test a detected app, with bounded waiting and no host execution."""
    if not sandbox.is_remote(root):
        return {"ok": True, "applicable": False, "detail": "Preview requires E2B"}
    prior = saved(root)
    if prior.get("stopped"):
        return {
            "ok": False,
            "applicable": True,
            "detail": "Preview was stopped by you. Start it in Preview to enable the check.",
        }
    if restart and prior:
        await asyncio.to_thread(stop, root)
        await asyncio.to_thread(start, root)
    value = await asyncio.to_thread(inspect, root)
    if value["status"] not in {"starting", "running"}:
        plan = await asyncio.to_thread(suggestions, root)
        if not plan.get("detected"):
            return {
                "ok": True,
                "applicable": False,
                "detail": "No supported web app detected",
            }
        try:
            value = await asyncio.to_thread(start, root)
        except ValueError:
            # A user may have started it while the agent was detecting the app.
            value = await asyncio.to_thread(inspect, root)
            if value["status"] not in {"starting", "running"}:
                raise
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            from temporalio import activity

            if activity.in_activity():
                activity.heartbeat("Checking sandbox preview")
            value = await asyncio.to_thread(inspect, root)
            if value["status"] == "stopped":
                return {
                    **value,
                    "ok": False,
                    "applicable": True,
                    "detail": "Preview startup failed. Inspect the logs and repair the app before retrying.",
                }
            if value["status"] == "running" and value.get("healthy", True):
                try:
                    async with client.stream("GET", value["url"]) as response:
                        if 200 <= response.status_code < 400:
                            return {
                                **value,
                                "ok": True,
                                "applicable": True,
                                "public_http_status": response.status_code,
                                "detail": "Public preview HTTP smoke check passed. This does not verify browser rendering or interactions.",
                            }
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(2)
    return {
        **value,
        "ok": False,
        "applicable": True,
        "detail": "Preview did not pass its HTTP smoke check before the startup deadline.",
    }
