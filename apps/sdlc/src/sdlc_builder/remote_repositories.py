"""Managed GitHub checkouts; tokens stay out of Git configuration and task input."""

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .store import data_dir


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", parsed.path)
    ):
        raise ValueError(
            "Use a GitHub repository URL such as https://github.com/owner/repository"
        )
    path = parsed.path.rstrip("/").removesuffix(".git")
    if any(part in {".", "..", ""} for part in path[1:].split("/")):
        raise ValueError("Enter a GitHub owner and repository name")
    return "https://github.com" + path.lower()


def git(root: Path, *args: str) -> str:
    with tempfile.TemporaryDirectory(prefix="boltzmann-git-") as temporary:
        home = Path(temporary)
        askpass = home / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token ;;\n*Password*) printf "%s\\n" "$BOLTZMANN_GIT_TOKEN" ;;\nesac\n'
        )
        askpass.chmod(0o700)
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(askpass),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "https",
            "BOLTZMANN_GIT_TOKEN": os.environ.get("SDLC_GITHUB_TOKEN", ""),
        }
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "http.followRedirects=false",
                    *args,
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                "GitHub did not respond in time. Retry connecting after checking the server connection."
            ) from None
        if result.returncode:
            raise ValueError(
                "Could not access this Git repository. Check its URL and the server's GitHub access."
            )
        return result.stdout.strip()


def connect(url: str, project_id: str) -> dict:
    url = canonical_url(url)
    root = data_dir() / "repositories" / project_id
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".connecting-", dir=root.parent
    ) as temporary:
        checkout = Path(temporary) / "checkout"
        git(root.parent, "clone", "--single-branch", "--", url + ".git", str(checkout))
        git(checkout, "config", "core.hooksPath", "/dev/null")
        branch = git(checkout, "branch", "--show-current")
        if not branch:
            raise ValueError(
                "Choose a repository with a default branch and at least one commit"
            )
        revision = git(checkout, "rev-parse", "HEAD")
        checkout.rename(root)
    return {
        "id": project_id,
        "name": url.rsplit("/", 1)[1],
        "path": str(root),
        "github_url": url,
        "remote_url": url,
        "default_branch": branch,
        "source": "github",
        "synced_commit": revision,
    }


def sync(project: dict) -> str:
    root = Path(project["path"])
    # Share the host merge lock: fetching a branch must not race with accepted
    # patches being applied into this server-owned checkout.
    locks = data_dir() / "project-merge-locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = locks / (hashlib.sha256(str(root.resolve()).encode()).hexdigest() + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if git(root, "status", "--porcelain"):
            raise ValueError(
                "This project's checkout has uncommitted changes. Commit or export them before syncing; your changes were preserved."
            )
        branch = project["default_branch"]
        if git(root, "branch", "--show-current") != branch:
            raise ValueError(
                "The project checkout changed branches. Restore its configured default branch before syncing."
            )
        git(root, "fetch", "--", "origin", f"refs/heads/{branch}")
        git(root, "merge", "--ff-only", "FETCH_HEAD")
        return git(root, "rev-parse", "HEAD")
