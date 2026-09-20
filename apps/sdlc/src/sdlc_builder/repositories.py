"""Local repository discovery and user-initiated folder selection."""

import os
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from .store import Store

SKIP = {
    "node_modules",
    "vendor",
    "venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    "Library",
}
MAX_DIRECTORIES = 1500
MAX_RESULTS = 200
MAX_DEPTH = 3


def directory(value: str) -> Path:
    if not value or len(value) > 4000 or "\0" in value:
        raise ValueError("Choose a folder on this computer.")
    try:
        path = Path(value).expanduser().resolve()
        available = path.is_dir()
    except (OSError, RuntimeError):
        available = False
    if not available:
        raise ValueError("That folder is no longer available. Choose another folder.")
    return path


def display_path(path: Path) -> str:
    try:
        relative = str(path.relative_to(Path.home()))
        return "~" if relative == "." else "~/" + relative
    except ValueError:
        return str(path)


def marker(path: Path) -> bool:
    # Worktrees use a .git file. Discovery never opens it or runs repository code.
    try:
        return (path / ".git").is_dir() or (path / ".git").is_file()
    except OSError:
        return False


def picker_command() -> list[str] | None:
    if sys.platform == "darwin":
        return [
            "/usr/bin/osascript",
            "-e",
            'POSIX path of (choose folder with prompt "Choose a project folder")',
        ]
    if sys.platform.startswith("linux") and (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        if executable := shutil.which("zenity"):
            return [
                executable,
                "--file-selection",
                "--directory",
                "--title=Choose a project folder",
            ]
        if executable := shutil.which("kdialog"):
            return [
                executable,
                "--getexistingdirectory",
                str(Path.home()),
                "--title",
                "Choose a project folder",
            ]
    return None


def pick_folder() -> dict:
    command = picker_command()
    if not command:
        return {"native": False, "path": None}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return {"native": False, "path": None}
    if result.returncode:
        cancelled = (
            "-128" in result.stderr
            if sys.platform == "darwin"
            else result.returncode == 1
        )
        return {"native": cancelled, "path": None}
    selected = result.stdout.rstrip("\r\n")
    return {"native": True, "path": str(directory(selected)) if selected else None}


def browse(value: str = "") -> dict:
    path = directory(value or str(Path.home()))
    folders = []
    truncated = False
    try:
        with os.scandir(path) as entries:
            for index, entry in enumerate(entries):
                if index >= 3000:
                    truncated = True
                    break
                if (
                    entry.name.startswith(".")
                    or entry.name in SKIP
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    continue
                folders.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "repository": marker(Path(entry.path)),
                    }
                )
    except OSError:
        raise ValueError(
            "This folder cannot be opened. Choose another folder."
        ) from None
    folders.sort(key=lambda item: item["name"].casefold())
    return {
        "path": str(path),
        "display_path": display_path(path),
        "parents": [
            {"name": item.name or "/", "path": str(item)}
            for item in reversed([path, *path.parents])
        ],
        "folders": folders[:MAX_RESULTS],
        "truncated": truncated or len(folders) > MAX_RESULTS,
    }


def discover(store: Store) -> dict:
    saved = store.setting("projects_folder", "")
    result = {
        "folder": saved,
        "display_path": display_path(Path(saved)) if saved else "",
        "suggestions": [],
        "truncated": False,
        "warning": "",
    }
    if not saved:
        return result
    try:
        root = directory(saved)
    except (ValueError, OSError):
        return {
            **result,
            "warning": "Your projects folder is unavailable. Choose another folder.",
        }
    connected = {
        item["path"]: item["id"]
        for item in store.all("projects")
        if not item.get("removed")
        if not item.get("demo")
    }
    queue = deque([(root, 0)])
    deadline = time.monotonic() + 2
    visited = 0
    while queue:
        if (
            visited >= MAX_DIRECTORIES
            or len(result["suggestions"]) >= MAX_RESULTS
            or time.monotonic() >= deadline
        ):
            result["truncated"] = True
            break
        path, depth = queue.popleft()
        visited += 1
        if marker(path):
            result["suggestions"].append(
                {
                    "name": path.name,
                    "path": str(path),
                    "display_path": display_path(path),
                    "connected_id": connected.get(str(path), ""),
                }
            )
            continue
        if depth >= MAX_DEPTH:
            continue
        try:
            with os.scandir(path) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORIES or time.monotonic() >= deadline:
                        result["truncated"] = True
                        break
                    if (
                        entry.name.startswith(".")
                        or entry.name in SKIP
                        or not entry.is_dir(follow_symlinks=False)
                    ):
                        continue
                    if len(queue) + visited >= MAX_DIRECTORIES:
                        result["truncated"] = True
                        break
                    queue.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    result["suggestions"].sort(key=lambda item: (item["name"].casefold(), item["path"]))
    return result
