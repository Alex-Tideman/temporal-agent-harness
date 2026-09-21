"""Exercise real preview processes behind a local E2B transport double."""

import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from sdlc_builder import preview_runtime, previews, sandbox
from sdlc_builder.server import create_app
from sdlc_builder.store import workspace

TASK_ID = "b" * 32


def eventually(read, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if condition(value):
            return value
        time.sleep(0.05)
    pytest.fail(f"Condition did not become true: {value}")


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path / "data"))
    root = workspace(TASK_ID)
    root.mkdir(parents=True)
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(sandbox, "REMOTE_ROOT", str(root))
    monkeypatch.setattr(sandbox, "CONTROL_ROOT", str(control))
    monkeypatch.setattr(sandbox, "is_remote", lambda _: True)
    descriptor = sandbox.descriptor(root)
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(json.dumps({"status": "ready"}))
    processes = []
    launches = []

    def run(command, *, background=False, **_kwargs):
        args = shlex.split(command)
        if args[0] == "mkdir":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="")
        args[0] = sys.executable
        if background:
            launches.append(command)
            process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            processes.append(process)
            return SimpleNamespace(pid=process.pid, disconnect=lambda: None)
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return SimpleNamespace(stdout=result.stdout)

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    client = SimpleNamespace(
        commands=SimpleNamespace(run=run),
        files=SimpleNamespace(
            write=lambda name, content: Path(name).write_text(content)
        ),
        get_host=lambda port: f"{port}-example.e2b.app",
        set_timeout=lambda _: None,
        traffic_access_token=None,
    )
    monkeypatch.setattr(sandbox, "_client", lambda _: client)
    yield SimpleNamespace(
        root=root, port=port, client=client, launches=launches, processes=processes
    )
    if previews.saved(root):
        previews.stop(root)
    for process in processes:
        process.wait(timeout=5)


def test_preview_serves_current_files_and_stops_process_group(remote):
    (remote.root / "index.html").write_text("Before the edit")
    # The shell waits for a child: stopping must also stop the child HTTP server.
    command = f"{shlex.quote(sys.executable)} -m http.server {remote.port} --bind 0.0.0.0 & wait"
    value = previews.start(remote.root, command, ".", remote.port)
    assert value["url"] == f"https://{remote.port}-example.e2b.app"
    assert "script" not in value and "pid" not in value
    eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "running",
    )
    url = f"http://127.0.0.1:{remote.port}"
    assert httpx.get(url).text == "Before the edit"
    (remote.root / "index.html").write_text("After the edit")
    assert httpx.get(url).text == "After the edit"
    with pytest.raises(ValueError, match="Stop the current"):
        previews.start(remote.root, command, ".", remote.port)
    assert len(remote.launches) == 1
    value = previews.stop(remote.root)
    assert value["status"] == "stopped" and value["url"] == ""
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", remote.port)) != 0
    assert (remote.root / "index.html").read_text() == "After the edit"
    assert previews.inspect(remote.root)["status"] == "stopped"
    assert previews.stop(remote.root)["status"] == "stopped"


def test_failure_keeps_bounded_logs_and_exit_code(remote):
    code = "import sys; print('x' * 80000); print('startup failed'); sys.exit(7)"
    previews.start(
        remote.root, shlex.join([sys.executable, "-c", code]), ".", remote.port
    )
    value = eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "stopped",
    )
    assert value["exit_code"] == 7, value["logs"]
    assert len(value["logs"]) <= preview_runtime.LOG_LIMIT
    assert "startup failed" in value["logs"]


def test_unknown_launch_can_be_stopped_without_duplicate_execution(remote):
    run = remote.client.commands.run

    def lost_reply(*args, **kwargs):
        result = run(*args, **kwargs)
        if kwargs.get("background"):
            raise TimeoutError("sensitive upstream data")
        return result

    remote.client.commands.run = lost_reply
    with pytest.raises(ValueError, match="could not be confirmed") as error:
        previews.start(remote.root, "sleep 60", ".", remote.port)
    assert "sensitive upstream data" not in str(error.value)
    with pytest.raises(ValueError):
        previews.start(remote.root, "sleep 60", ".", remote.port)
    assert len(remote.launches) == 1
    assert previews.stop(remote.root)["status"] == "stopped"


def test_stop_before_delayed_launch_prevents_server_from_starting(tmp_path):
    marker = tmp_path / "launched"
    config = tmp_path / "preview.json"
    config.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "cwd": ".",
                "port": 3000,
                "host": "example.e2b.app",
                "command": "touch " + shlex.quote(str(marker)),
            }
        )
    )
    preview_runtime.stop(config)
    preview_runtime.supervise(config)
    assert not marker.exists()
    assert preview_runtime.status(config)["status"] == "stopped"


def test_unconfirmed_failed_launch_remains_resettable(remote):
    run = remote.client.commands.run

    def lost_reply(*args, **kwargs):
        result = run(*args, **kwargs)
        if kwargs.get("background"):
            raise TimeoutError()
        return result

    remote.client.commands.run = lost_reply
    with pytest.raises(ValueError):
        previews.start(remote.root, "exit 2", ".", remote.port)
    state = eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "stopped",
    )
    assert state["launch_unconfirmed"] is True
    with pytest.raises(ValueError, match="unconfirmed"):
        previews.start(remote.root, "exit 0", ".", remote.port)
    assert previews.stop(remote.root)["launch_unconfirmed"] is False


def test_occupied_port_does_not_stop_an_existing_server(remote):
    with socket.socket() as existing:
        existing.bind(("127.0.0.1", remote.port))
        existing.listen()
        previews.start(remote.root, "exit 0", ".", remote.port)
        value = eventually(
            lambda: previews.inspect(remote.root),
            lambda value: value["status"] == "stopped",
        )
        assert "already in use" in value["logs"]
        previews.stop(remote.root)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", remote.port)) == 0


@pytest.mark.parametrize("cwd", ["../outside", "/tmp"])
def test_rejects_directory_escape_before_launch(remote, cwd):
    with pytest.raises(ValueError, match="relative workspace"):
        previews.start(remote.root, "echo unsafe", cwd, remote.port)
    assert not remote.launches


def test_supervisor_rejects_symlink_directory(remote, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (remote.root / "linked").symlink_to(outside)
    previews.start(remote.root, "touch escaped", "linked", remote.port)
    value = eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "stopped",
    )
    assert "workspace" in value["logs"]
    assert not (outside / "escaped").exists()


def test_rejects_private_ingress_without_launch(remote):
    remote.client.traffic_access_token = "private-token"
    with pytest.raises(ValueError, match="authenticated network"):
        previews.start(remote.root, "sleep 60", ".", remote.port)
    assert not remote.launches


def test_command_environment_excludes_host_credentials(remote, monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "never-forward-this")
    monkeypatch.setenv("OPENAI_API_KEY", "nor-this")
    previews.start(remote.root, "env", ".", remote.port)
    value = eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "stopped",
    )
    assert "never-forward-this" not in value["logs"]
    assert "nor-this" not in value["logs"]
    assert f"PORT={remote.port}" in value["logs"]
    assert "HOST=0.0.0.0" in value["logs"]


@pytest.mark.parametrize(
    "framework,manager,port", [("vite", "npm", 5173), ("next dev", "pnpm", 3000)]
)
def test_suggests_nested_app_launch_without_running_it(
    remote, monkeypatch, framework, manager, port
):
    names = (
        ["apps/web/package.json", "pnpm-lock.yaml"]
        if manager == "pnpm"
        else ["apps/web/package.json"]
    )
    monkeypatch.setattr(previews.workspaces, "files", lambda _: names)
    monkeypatch.setattr(
        previews.workspaces,
        "read_file",
        lambda *_: {"content": json.dumps({"scripts": {"dev": framework}})},
    )
    value = previews.inspect(remote.root)
    assert value["cwd"] == "apps/web" and value["port"] == port
    assert value["command"].startswith(f"{manager} run dev")
    assert value["automatic"] is True
    assert "0.0.0.0" in value["command"]
    assert not remote.launches


async def test_preview_routes_require_session_and_valid_task(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    app.state.store.put("tasks", {"id": TASK_ID})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        url = f"/api/tasks/{TASK_ID}/preview"
        assert (await client.get(url)).status_code == 401
        await client.post(
            "/api/unlock", json={"token": (tmp_path / "session-token").read_text()}
        )
        assert (await client.get(url)).json()["supported"] is False
        assert (
            await client.post(url, json={"command": "echo no", "port": 3000})
        ).status_code == 400
        assert (
            await client.post(url, json={"command": "echo no", "port": 80})
        ).status_code == 422
        assert (
            await client.get("/api/tasks/" + "c" * 32 + "/preview")
        ).status_code == 400
        calls = []
        monkeypatch.setattr(
            previews,
            "start",
            lambda *args: calls.append(args) or {"status": "starting"},
        )
        response = await client.post(
            url, json={"command": "npm run dev", "cwd": "web", "port": 3000}
        )
        assert response.status_code == 200 and calls == [
            (workspace(TASK_ID), "npm run dev", "web", 3000)
        ]
        assert (
            await client.post(
                url,
                json={"command": "echo no"},
                headers={"Origin": "https://other.example"},
            )
        ).status_code == 403
        assert len(calls) == 1
        response = await client.post(url, json={})
        assert response.status_code == 200
        assert calls[-1] == (workspace(TASK_ID), "", None, None)


def manifests(remote, monkeypatch, contents):
    for name, content in contents.items():
        path = remote.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content) if isinstance(content, dict) else content)
    monkeypatch.setattr(previews.workspaces, "files", lambda _: list(contents))
    monkeypatch.setattr(
        previews.workspaces,
        "read_file",
        lambda _, name: {"content": (remote.root / name).read_text()},
    )


def test_missing_bun_is_installed_before_dependencies_and_server(
    remote, monkeypatch, tmp_path
):
    manifests(
        remote,
        monkeypatch,
        {
            "package.json": {"packageManager": "bun@1.2.3", "scripts": {"dev": "vite"}},
            "bun.lock": "fixture",
            "index.html": "Automatic setup works",
        },
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # The transport runs the real supervisor; only registry installation is faked.
    bun = f"""#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1] == '--version':
    print('1.2.3')
elif sys.argv[1] == 'install':
    Path('dependencies-installed').write_text('yes')
else:
    assert Path('dependencies-installed').exists()
    os.execv(sys.executable, [sys.executable, '-m', 'http.server', os.environ['PORT'], '--bind', '0.0.0.0'])
"""
    npm = fake_bin / "npm"
    npm.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
assert sys.argv[-1] == 'bun@1.2.3', sys.argv
prefix = Path(sys.argv[sys.argv.index('--prefix') + 1])
(prefix / 'bin').mkdir(parents=True, exist_ok=True)
target = prefix / 'bin/bun'
target.write_text({bun!r})
target.chmod(0o755)
""")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + ":/usr/bin:/bin")
    value = previews.start(remote.root, port=remote.port)
    assert value["automatic"] is True and value["phase"] == "tooling"
    state = eventually(
        lambda: previews.inspect(remote.root),
        lambda value: value["status"] == "running",
    )
    assert state["healthy"] and state["http_status"] == 200
    assert "Installing project dependencies" in state["logs"]
    assert (remote.root / "dependencies-installed").exists()
    assert httpx.get(f"http://127.0.0.1:{remote.port}").text == "Automatic setup works"


def test_monorepo_uses_pinned_root_manager_and_installs_at_root(remote, monkeypatch):
    manifests(
        remote,
        monkeypatch,
        {
            "package.json": {"packageManager": "pnpm@9.15.0", "workspaces": ["apps/*"]},
            "pnpm-lock.yaml": "fixture",
            "apps/web/package.json": {"scripts": {"dev": "vite"}},
        },
    )
    plan = previews.suggestions(remote.root)
    assert plan["cwd"] == "apps/web"
    assert plan["setup"] == {"manager": "pnpm", "version": "9.15.0", "install_cwd": "."}


def test_http_error_page_is_not_a_healthy_preview(remote):
    code = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b"Compilation failed")
HTTPServer(("0.0.0.0", int(__import__('os').environ['PORT'])), Handler).serve_forever()
"""
    previews.start(
        remote.root, shlex.join([sys.executable, "-c", code]), ".", remote.port
    )
    value = eventually(
        lambda: previews.inspect(remote.root), lambda value: value["http_status"] == 500
    )
    assert not value["healthy"] and value["status"] == "starting"
    assert "HTTP 500" in value["detail"]


@pytest.mark.parametrize("http_status,expected", [(200, True), (500, False)])
async def test_agent_checks_public_url_not_just_local_listener(
    remote, monkeypatch, http_status, expected
):
    monkeypatch.setattr(
        previews,
        "inspect",
        lambda _: {
            "status": "running",
            "healthy": True,
            "url": "https://3000-sandbox.e2b.app",
            "logs": "server ready",
        },
    )
    requests = []
    client_type = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(http_status, text="fixture")

    monkeypatch.setattr(
        previews.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw),
    )
    result = await previews.verify(remote.root, timeout=0.1)
    assert result["ok"] is expected
    assert str(requests[0].url) == "https://3000-sandbox.e2b.app"


async def test_automatic_verification_respects_explicit_stop(remote):
    previews.start(remote.root, "sleep 60", ".", remote.port)
    previews.stop(remote.root)
    result = await previews.verify(remote.root, restart=True)
    assert not result["ok"] and "stopped by you" in result["detail"]
    assert len(remote.launches) == 1


@pytest.mark.parametrize("publish_receipt", [True, False])
def test_stop_handles_supervisor_lock_before_receipt(
    tmp_path, monkeypatch, publish_receipt
):
    config = tmp_path / "preview.json"
    stopped = False
    signals = []
    monkeypatch.setattr(preview_runtime, "running", lambda _: not stopped)
    monkeypatch.setattr(preview_runtime, "start_time", lambda _: "same-process")

    def during_wait(_):
        nonlocal stopped
        assert config.with_suffix(".cancel").exists()
        if publish_receipt:
            config.with_suffix(".state").write_text(
                json.dumps({"pid": 12345, "start_time": "same-process"})
            )
        else:
            # The supervisor sees cancellation before writing its receipt.
            stopped = True

    def kill(pid, sig):
        nonlocal stopped
        signals.append(pid)
        stopped = True

    monkeypatch.setattr(preview_runtime.time, "sleep", during_wait)
    monkeypatch.setattr(preview_runtime.os, "kill", kill)
    preview_runtime.stop(config)
    assert signals == ([12345] if publish_receipt else [])


async def test_sdk_errors_are_not_disclosed(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    app.state.store.put("tasks", {"id": TASK_ID})

    def unavailable(_):
        raise RuntimeError("a private upstream message")

    monkeypatch.setattr(previews, "inspect", unavailable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        await client.post(
            "/api/unlock", json={"token": (tmp_path / "session-token").read_text()}
        )
        response = await client.get(f"/api/tasks/{TASK_ID}/preview")
        assert response.status_code == 503 and "private upstream" not in response.text
