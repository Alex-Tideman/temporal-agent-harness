import json
import shlex
import socket
import sys

import httpx
import pytest
from test_previews import eventually
from test_previews import remote as remote

from sdlc_builder import previews, sandbox
from sdlc_builder import project_workspaces as projects
from sdlc_builder import workspace_runtime as runtime
from sdlc_builder.store import workspace


def setup_project(remote):
    root = remote.root
    second = workspace("c" * 32)
    second.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        other_port = probe.getsockname()[1]
    ports = {root.name: remote.port, second.name: other_port}
    projects.save(
        projects.project_path("project"),
        {
            "status": "ready",
            "project_id": "project",
            "generation": "generation",
            "tasks": ports,
        },
    )
    for item in (root, second):
        sandbox._save(
            item,
            {
                "project_id": "project",
                "status": "ready",
                "generation": "generation",
                "remote_root": str(item),
                "preview_port": ports[item.name],
            },
        )
    return root, second, other_port


def test_two_previews_use_their_own_worktrees_homes_ports_and_processes(remote):
    root, second, other_port = setup_project(remote)
    (root / "index.html").write_text("First task")
    (second / "index.html").write_text("Second task")
    command = shlex.quote(sys.executable) + ' -m http.server "$PORT" --bind 0.0.0.0'
    try:
        one = previews.start(root, command)
        two = previews.start(second, command)
        assert one["port"] == remote.port and two["port"] == other_port
        for item in (root, second):
            eventually(
                lambda: previews.inspect(item),
                lambda value: value["status"] == "running",
            )
        assert httpx.get(f"http://127.0.0.1:{remote.port}").text == "First task"
        assert httpx.get(f"http://127.0.0.1:{other_port}").text == "Second task"
        configs = [
            json.loads(
                __import__("pathlib")
                .Path(previews._paths(previews.saved(item))[1])
                .read_text()
            )
            for item in (root, second)
        ]
        assert configs[0]["home"] != configs[1]["home"]
        assert configs[0]["root"] == str(root) and configs[1]["root"] == str(second)
        # An invalid duplicate start cannot silently move a live port reservation.
        with pytest.raises(ValueError, match="Stop the current"):
            previews.start(root, command, port=21000)
        assert projects.project_state("project")["tasks"][root.name] == remote.port
        previews.stop(root)
        assert previews.inspect(second)["status"] == "running"
        assert httpx.get(f"http://127.0.0.1:{other_port}").text == "Second task"
    finally:
        for item in (root, second):
            previews.stop(item)


def test_preview_setup_holds_remote_writer_lock_until_stopped(remote):
    root, _, _ = setup_project(remote)
    previews.start(root, "sleep 60")

    def locked():
        try:
            with runtime.workspace_lock(root, blocking=False):
                return False
        except ValueError:
            return True

    eventually(locked, bool)
    previews.stop(root)
    with runtime.workspace_lock(root, blocking=False):
        pass
