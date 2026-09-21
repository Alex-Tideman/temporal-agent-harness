"""Standalone supervisor uploaded to E2B; never used to run host previews."""

import fcntl
import http.client
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

LOG_LIMIT = 32_000


def probe(settings: dict) -> int | None:
    connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=2)
    try:
        connection.request("GET", "/", headers={"Host": settings["host"]})
        response = connection.getresponse()
        response.read(4096)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def setup(config: Path):
    """Install the detected package manager/dependencies, then replace this shell."""
    settings = json.loads(config.read_text())
    plan = settings["setup"]
    manager, version = plan["manager"], plan.get("version", "")
    if manager not in {"npm", "pnpm", "yarn", "bun"} or (
        version and not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version)
    ):
        raise ValueError("Unsupported package manager specification")
    tools = config.parent / "tools"
    tools.mkdir(exist_ok=True)

    def stage(name: str, detail: str):
        save(config.with_suffix(".phase"), {"phase": name, "detail": detail})
        print(f"[preview] {detail}", flush=True)

    def run(argv, cwd=None):
        subprocess.run(argv, cwd=cwd, check=True, stdin=subprocess.DEVNULL)

    stage("tooling", f"Preparing {manager}{' ' + version if version else ''}")
    available = shutil.which(manager)
    current = ""
    if available:
        checked = subprocess.run(
            [available, "--version"], capture_output=True, text=True, timeout=30
        )
        current = checked.stdout.strip() if checked.returncode == 0 else ""
    if not current or (version and current != version):
        if not shutil.which("npm"):
            raise ValueError(
                "The sandbox template needs Node.js and npm to install JavaScript tooling"
            )
        # Install into the sandbox's private tooling directory, never the source.
        if manager == "yarn" and version and int(version.split(".")[0]) >= 2:
            run(["npm", "install", "--global", "--prefix", str(tools), "corepack"])
            run(
                [
                    str(tools / "bin/corepack"),
                    "enable",
                    "--install-directory",
                    str(tools / "bin"),
                ]
            )
            run(
                [
                    str(tools / "bin/corepack"),
                    "prepare",
                    f"yarn@{version}",
                    "--activate",
                ]
            )
        else:
            spec = manager + ("@" + version if version else "")
            run(["npm", "install", "--global", "--prefix", str(tools), spec])
    run([manager, "--version"])
    root = Path(settings["root"]).resolve()
    install_dir = root / plan.get("install_cwd", settings["cwd"])
    if not install_dir.resolve().is_relative_to(root):
        raise ValueError("Install directory must stay inside the workspace")
    stage("installing", "Installing project dependencies")
    install = [manager, "install"]
    if manager == "pnpm":
        install.append("--no-frozen-lockfile")
    run(install, cwd=install_dir)
    stage("starting", "Starting the development server")
    os.execvpe("/bin/bash", ["/bin/bash", "-c", settings["command"]], os.environ)


def save(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def start_time(pid: int) -> str:
    path = Path(f"/proc/{pid}/stat")
    if not Path("/proc").exists():  # Allows local supervisor tests on macOS.
        return ""
    try:
        return path.read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "missing"


def running(config: Path) -> bool:
    with config.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def status(config: Path) -> dict:
    # Hold a shared lock while reading an idle receipt, so a delayed launch
    # cannot transition to running halfway through this status snapshot.
    with config.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
        state_path = config.with_suffix(".state")
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        ready = False
        http_status = None
        phase_path = config.with_suffix(".phase")
        phase = json.loads(phase_path.read_text()) if phase_path.exists() else {}
        if active:
            settings = json.loads(config.read_text())
            if state.get("launched") and phase.get("phase") not in {
                "tooling",
                "installing",
            }:
                http_status = probe(settings)
                ready = http_status is not None and 200 <= http_status < 400
        log = config.with_suffix(".log")
        output = ""
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - LOG_LIMIT))
                output = stream.read(LOG_LIMIT).decode("utf-8", errors="replace")
        pending = (
            not state
            and not log.exists()
            and not config.with_suffix(".cancel").exists()
        )
        return {
            "status": "running"
            if ready
            else "starting"
            if active or pending
            else "stopped",
            "exit_code": state.get("exit_code"),
            "logs": output,
            "phase": "ready"
            if ready
            else phase.get("phase", "starting")
            if active or pending
            else "stopped"
            if config.with_suffix(".cancel").exists()
            else "failed",
            "detail": f"HTTP {http_status} check passed"
            if ready
            else (
                f"Waiting for a successful response (HTTP {http_status})"
                if http_status
                else phase.get("detail", "Waiting for the app to respond")
            ),
            "http_status": http_status,
            "healthy": ready,
        }


def stop(config: Path):
    # A delayed/lost launch response must not start a server after Stop returns.
    config.with_suffix(".cancel").touch()
    state_path = config.with_suffix(".state")
    # The supervisor takes its lock before publishing the PID receipt. A Stop
    # in that small gap should wait for the receipt (or cancelled startup), not
    # require another click. The cancel marker also covers delayed launches.
    deadline = time.monotonic() + 2
    while True:
        if not running(config):
            return
        if state_path.exists():
            break
        if time.monotonic() >= deadline:
            raise ValueError(
                "Preview is still starting. Try stopping it again in a moment."
            )
        time.sleep(0.02)
    state = json.loads(state_path.read_text())
    pid = state["pid"]
    if start_time(pid) != state["start_time"]:
        raise ValueError("Preview process changed; refusing to stop another process")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(40):
        if not running(config):
            return
        time.sleep(0.1)
    raise ValueError("Preview is still shutting down. Try stopping it again.")


def supervise(config: Path):
    settings = json.loads(config.read_text())
    root = Path(settings["root"]).resolve()
    relative = Path(settings["cwd"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("App directory must be inside the task workspace")
    directory = root / relative
    if not directory.resolve().is_relative_to(root) or not directory.is_dir():
        raise ValueError("App directory does not exist inside the task workspace")
    for path in [directory, *directory.parents]:
        if path == root:
            break
        if path.is_symlink():
            raise ValueError("App directory must not contain symlinks")

    with config.with_suffix(".lock").open("a") as lock:
        # Reusing a request never launches a second server.
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = config.with_suffix(".state")
        if state_path.exists():
            raise ValueError("This preview launch has already run")
        if config.with_suffix(".cancel").exists():
            return
        state = {"pid": os.getpid(), "start_time": start_time(os.getpid())}
        save(state_path, state)
        requested_stop = False

        def stopping(_signal, _frame):
            nonlocal requested_stop
            requested_stop = True

        signal.signal(signal.SIGTERM, stopping)
        signal.signal(signal.SIGINT, stopping)
        with socket.socket() as port_probe:
            port_probe.settimeout(0.5)
            if port_probe.connect_ex(("127.0.0.1", settings["port"])) == 0:
                raise ValueError(
                    "This port is already in use. Choose another preview port."
                )
        # Do not inherit credentials from a template or the SDK control channel.
        home = Path(settings.get("home") or root.parent)
        (home / ".tmp").mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": os.pathsep.join(
                [
                    str(config.parent / "tools/bin"),
                    str(home / ".bun/bin"),
                    str(home / ".local/share/pnpm"),
                    os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                ]
            ),
            "HOME": str(home),
            "TMPDIR": str(home / ".tmp"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "LANG": "C.UTF-8",
            "PORT": str(settings["port"]),
            "HOST": "0.0.0.0",
            "BROWSER": "none",
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": settings["host"],
            "COREPACK_HOME": str(config.parent / "corepack"),
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "COREPACK_ENABLE_AUTO_PIN": "0",
            "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
        }
        source_lock = None
        if settings.get("shared"):
            lock_dir = root.parent / ".locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            source_lock = (lock_dir / (root.name + ".lock")).open("a")
            try:
                fcntl.flock(source_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                source_lock.close()
                raise ValueError(
                    "This worktree has an in-flight operation. Retry preview setup when it finishes."
                ) from None
        argv = (
            [sys.executable, "-I", __file__, "setup", str(config)]
            if settings.get("setup")
            else ["/bin/bash", "-c", settings["command"]]
        )
        child = subprocess.Popen(
            argv,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state["launched"] = True
        save(state_path, state)
        log = b""
        log_path = config.with_suffix(".log")
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        next_probe = started
        was_ready = False
        try:
            while not requested_stop:
                events = selector.select(0.2)
                if events:
                    chunk = os.read(child.stdout.fileno(), 4096)
                    if chunk:
                        log = (log + chunk)[-LOG_LIMIT:]
                        log_path.write_bytes(log)
                    else:
                        break
                if child.poll() is not None and not events:
                    break
                if not was_ready and time.monotonic() >= next_probe:
                    http_status = probe(settings)
                    was_ready = http_status is not None and 200 <= http_status < 400
                    if was_ready and source_lock is not None:
                        source_lock.close()
                        source_lock = None
                    next_probe = time.monotonic() + 3
                if not was_ready and time.monotonic() - started > settings.get(
                    "startup_timeout", 540
                ):
                    log = (
                        log
                        + b"\n[preview] Startup timed out before the app returned a successful HTTP response.\n"
                    )[-LOG_LIMIT:]
                    log_path.write_bytes(log)
                    break
        finally:
            # Stop the entire shell/npm/server process group, including children.
            for signum in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS local tests can reject signaling a departed group.
                    # E2B runs Linux; never suppress a cleanup error there.
                    if sys.platform != "darwin" or child.poll() is None:
                        raise
                    break
                if signum == signal.SIGTERM:
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            child.wait()
            if source_lock is not None:
                source_lock.close()
            child.stdout.close()
            selector.close()
            save(state_path, {**state, "exit_code": child.returncode})


if __name__ == "__main__":
    operation, filename = sys.argv[1:]
    config = Path(filename)
    try:
        if operation == "run":
            supervise(config)
        elif operation == "setup":
            setup(config)
        elif operation == "stop":
            stop(config)
            print(json.dumps(status(config)))
        elif operation == "status":
            print(json.dumps(status(config)))
        else:
            raise ValueError("Unknown preview operation")
    except Exception as error:
        if operation == "run":
            config.with_suffix(".log").write_text(str(error)[:LOG_LIMIT])
        elif operation == "setup":
            print(f"[preview] Setup failed: {error}", flush=True)
            sys.exit(1)
        else:
            print(json.dumps({"error": str(error)}))
