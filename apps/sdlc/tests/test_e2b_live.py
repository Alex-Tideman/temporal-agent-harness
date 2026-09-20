"""Opt-in E2B smoke test; creates and always attempts to delete a real sandbox."""

import os
import time

import httpx
import pytest

from sdlc_builder import previews, sandbox
from sdlc_builder import workspaces as ws
from sdlc_builder.coding_workspace import revision
from sdlc_builder.store import workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("SDLC_E2B_LIVE") != "1" or not os.environ.get("E2B_API_KEY"),
    reason="Set SDLC_E2B_LIVE=1 and E2B_API_KEY for the real E2B smoke test",
)


async def test_e2b_persistence_and_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "e2b")
    root = workspace("e" * 32)
    try:
        prepared = ws.prepare_workspace(str(ws.demo_project()), root.name)
        before = revision(root)
        file = ws.read_file(root, "greeting.py")
        ws.write_file(
            root,
            "greeting.py",
            'def greet(name):\n    return f"Hello, {name}!"\n',
            file["hash"],
        )
        changed = revision(root)
        assert before != changed and prepared["workspace_backend"] == "e2b"
        sandbox.release(root, remove=False)
        assert revision(root) == changed  # Reconnect resumes the same VM.
        checked = await ws.execute_check(root, ["python3", "-m", "unittest", "-v"])
        assert checked["exit_code"] == 0, checked["output"]
        assert "test_greeting" in checked["output"]
        assert ws.diff(root)["files"] == ["greeting.py"]
        ws.write_file(root, "preview.html", "Preview before edit", "")
        preview = previews.start(
            root, "python3 -m http.server 3000 --bind 0.0.0.0", ".", 3000
        )
        for _ in range(30):
            if previews.inspect(root)["status"] == "running":
                break
            time.sleep(1)
        assert previews.inspect(root)["status"] == "running"
        url = preview["url"] + "/preview.html"
        assert httpx.get(url, timeout=30).text == "Preview before edit"
        file = ws.read_file(root, "preview.html")
        ws.write_file(root, "preview.html", "Preview after edit", file["hash"])
        assert httpx.get(url, timeout=30).text == "Preview after edit"
        assert previews.stop(root)["status"] == "stopped"
    finally:
        sandbox.release(root, remove=True)
