"""Choose and reuse runtime templates without sending repository code to builds.

Detection reads the same committed, sanitized archive uploaded to the workspace.
Only numeric runtime versions and known package-manager names enter build recipes;
repository Dockerfiles, scripts, credentials, and dependencies are never executed
or copied into a shared template.
"""

import fcntl
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time
import tomllib
from pathlib import PurePosixPath

from .store import data_dir

logger = logging.getLogger(__name__)
BUILD_WAIT = 90
RETRY_AFTER = 300
RECIPE_VERSION = 1
MANIFESTS = {
    "package.json",
    ".nvmrc",
    ".node-version",
    ".python-version",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "poetry.lock",
    "bun.lock",
    "bun.lockb",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
}


def _numeric(value, *, python=False) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().removeprefix("v")
    pattern = r"3\.1[1-9](?:\.\d{1,3})?" if python else r"\d{1,2}(?:\.\d{1,3}){0,2}"
    return value if re.fullmatch(pattern, value) else ""


def _object(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def detect(archive: bytes) -> dict:
    """Bounded manifest inspection, including packages below a monorepo root."""
    manifests = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            name = PurePosixPath(member.name).name
            if name not in MANIFESTS or not member.isfile() or len(manifests) >= 256:
                continue
            # Lockfiles only signal a package manager; do not parse their contents.
            contents = ""
            if (
                name
                in {
                    "package.json",
                    "pyproject.toml",
                    ".nvmrc",
                    ".node-version",
                    ".python-version",
                }
                and member.size <= 100_000
            ):
                contents = (
                    source.extractfile(member).read().decode("utf-8", errors="replace")
                )
            manifests[member.name] = contents
    paths = sorted(manifests, key=lambda path: (path.count("/"), path))
    packages = [(_object(manifests[p]), p) for p in paths if p.endswith("package.json")]
    node_files = [
        p for p in paths if PurePosixPath(p).name in {".nvmrc", ".node-version"}
    ]
    python_files = [p for p in paths if PurePosixPath(p).name == ".python-version"]
    has_python = any(
        PurePosixPath(p).name
        in {
            "pyproject.toml",
            "requirements.txt",
            "uv.lock",
            "poetry.lock",
            ".python-version",
        }
        for p in paths
    )
    has_node = bool(
        packages
        or node_files
        or any(
            PurePosixPath(p).name
            in {
                "bun.lock",
                "bun.lockb",
                "pnpm-lock.yaml",
                "yarn.lock",
                "package-lock.json",
            }
            for p in paths
        )
    )
    node = next(
        (_numeric(manifests[p]) for p in node_files if _numeric(manifests[p])), ""
    )
    python = next(
        (
            _numeric(manifests[p], python=True)
            for p in python_files
            if _numeric(manifests[p], python=True)
        ),
        "",
    )
    managers = {}
    for package, _ in packages:
        volta = package.get("volta")
        if not node and isinstance(volta, dict):
            node = _numeric(volta.get("node"))
        manager = package.get("packageManager", "")
        if isinstance(manager, str):
            match = re.fullmatch(
                r"(npm|pnpm|yarn|bun)@(\d{1,3}\.\d{1,3}\.\d{1,3})(?:\+sha\d+\.[a-fA-F0-9]+)?",
                manager,
            )
            if match:
                managers.setdefault(match[1], match[2])
    for manager, locks in {
        "bun": {"bun.lock", "bun.lockb"},
        "pnpm": {"pnpm-lock.yaml"},
        "yarn": {"yarn.lock"},
    }.items():
        if any(PurePosixPath(p).name in locks for p in paths):
            managers.setdefault(manager, "")
    # Exact pins take precedence. Common lower-bound declarations select their
    # runtime line; more complex constraints are left to project setup tooling.
    if has_node and not node:
        for package, _ in packages:
            engines = package.get("engines")
            constraint = engines.get("node", "") if isinstance(engines, dict) else ""
            if isinstance(constraint, str):
                match = re.fullmatch(
                    r"(\^|~|>=)?\s*(\d{1,2}(?:\.\d{1,3}){0,2})(?:\.[x*])?",
                    constraint.strip(),
                )
                if match:
                    # A tilde range stays on its minor line; exact pins stay exact.
                    node = match[2].split(".")[0] if match[1] == "^" else match[2]
                    if match[1] == "~":
                        node = ".".join(match[2].split(".")[:2])
                    break
        node = node or "24"
    if has_python and not python:
        for p in paths:
            if PurePosixPath(p).name != "pyproject.toml":
                continue
            try:
                project = tomllib.loads(manifests[p]).get("project", {})
                constraint = (
                    project.get("requires-python", "")
                    if isinstance(project, dict)
                    else ""
                )
                if isinstance(constraint, str):
                    match = re.fullmatch(
                        r"(?:>=|==|~=)\s*(3\.\d{1,2}(?:\.\d+)?)(?:\s*,\s*<\s*3\.\d{1,2}(?:\.\d+)?)?",
                        constraint,
                    )
                    if match:
                        python = _numeric(match[1], python=True)
                        if python:
                            break
            except (ValueError, TypeError):
                pass
        python = python or "3.12"
    return {"node": node, "python": python, "managers": dict(sorted(managers.items()))}


def label(profile: dict) -> str:
    parts = []
    if profile["node"]:
        parts.append(f"Node.js {profile['node']}")
    if profile["python"]:
        parts.append(f"Python {profile['python']}")
    parts.extend(
        f"{name.capitalize()} {version}".strip()
        for name, version in profile["managers"].items()
    )
    return " · ".join(parts) or "General purpose"


def recipe(profile: dict, sdk):
    """Fixed app-owned recipe; no repository build context or arbitrary commands."""
    if profile["node"]:
        builder = sdk().from_image(f"node:{profile['node']}-bookworm")
    else:
        builder = sdk().from_image(f"python:{profile['python']}-bookworm")
    builder = builder.run_cmd(
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        "git python3 python3-venv curl ca-certificates build-essential && "
        "rm -rf /var/lib/apt/lists/*",
        user="root",
    )
    if profile["python"]:
        # uv and its Python live outside HOME: command execution intentionally
        # provides a fresh HOME without inheriting credentials or shell config.
        builder = builder.run_cmd(
            "curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh && "
            "UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh && "
            "rm /tmp/uv-install.sh",
            user="root",
        )
        if profile["node"]:
            builder = builder.run_cmd(
                "UV_PYTHON_INSTALL_DIR=/opt/boltzmann-python UV_PYTHON_BIN_DIR=/usr/local/bin "
                f"uv python install {profile['python']} --default",
                user="root",
            )
    for manager, version in profile["managers"].items():
        if manager == "yarn" and version and int(version.split(".")[0]) >= 2:
            builder = builder.run_cmd(
                "npm install --global --force corepack && corepack enable && "
                f"COREPACK_HOME=/opt/boltzmann-corepack corepack prepare yarn@{version} --activate && "
                "chmod -R a+rX /opt/boltzmann-corepack && "
                # Keep the preinstalled version usable with the sanitized HOME
                # and COREPACK_HOME used by preview and verification commands.
                "rm /usr/local/bin/yarn && "
                "printf '%s\\n' '#!/bin/sh' 'COREPACK_HOME=/opt/boltzmann-corepack exec /usr/local/lib/node_modules/corepack/dist/yarn.js \"$@\"' > /usr/local/bin/yarn && "
                "chmod +x /usr/local/bin/yarn",
                user="root",
            )
        else:
            package = manager + (f"@{version}" if version else "")
            builder = builder.run_cmd(
                f"npm install --global --force {package}", user="root"
            )
    builder = (
        builder.run_cmd(
            "(id user >/dev/null 2>&1 || useradd --create-home --shell /bin/bash user) && "
            "mkdir -p /home/user/workspace && chown -R user:user /home/user && "
            "python3 --version && git --version",
            user="root",
        )
        .set_user("user")
        .set_workdir("/home/user")
    )
    checks = ["python3 --version", "git --version"]
    if profile["node"]:
        checks.extend(["node --version", "npm --version"])
    checks.extend(f"{manager} --version" for manager in profile["managers"])
    return builder.run_cmd(" && ".join(checks))


def _sdk():
    from e2b import Template

    return Template


def _save(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.chmod(0o600)
    temporary.replace(path)


def select(archive: bytes, override: str = "") -> dict:
    profile = detect(archive)
    result = {
        "template": "base",
        "profile": profile,
        "label": label(profile),
        "source": "automatic",
        "reason": "Selected from the repository's committed manifests.",
    }
    if override and override not in {"auto", "base"}:
        return {
            **result,
            "template": override,
            "label": override,
            "source": "override",
            "reason": "Launch environment override (E2B_TEMPLATE).",
        }
    if not profile["node"] and not profile["python"]:
        return {
            **result,
            "reason": "No supported runtime manifest detected; using the general-purpose environment.",
        }
    fingerprint = hashlib.sha256(
        json.dumps({"recipe": RECIPE_VERSION, **profile}, sort_keys=True).encode()
    ).hexdigest()[:20]
    name = f"boltzmann-auto-v{RECIPE_VERSION}-{fingerprint}"
    # Separate credentials/endpoints must never reuse each other's build IDs.
    scope = hashlib.sha256(
        (
            os.environ.get("E2B_API_KEY", "")
            + "\0"
            + os.environ.get("E2B_API_URL", "")
            + "\0"
            + os.environ.get("E2B_DOMAIN", "")
        ).encode()
    ).hexdigest()[:20]
    cache = data_dir() / "sandbox-templates" / scope
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = cache / f"{name}.json"
    deadline = time.monotonic() + BUILD_WAIT
    try:
        with path.with_suffix(".lock").open("a") as lock:
            # Coordinate parallel task requests and app processes, with a bounded wait.
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    time.sleep(0.25)
            try:
                record = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(record, dict):
                    record = {}
            except ValueError:
                record = {}
            if (
                record.get("status") == "error"
                and time.time() - record.get("updated", 0) < RETRY_AFTER
            ):
                return {
                    **result,
                    "source": "fallback",
                    "reason": "Runtime template is temporarily unavailable; using the general-purpose environment.",
                }
            sdk = _sdk()
            from e2b.template.types import BuildInfo

            if record.get("template_id") and record.get("status") != "error":
                build = BuildInfo(
                    template_id=record["template_id"],
                    build_id=record["build_id"],
                    name=name,
                    alias=name,
                )
            else:
                # Persist a receipt before the request. An uncertain response is
                # not retried immediately, which could create duplicate builds.
                _save(path, {"status": "error", "updated": time.time()})
                build = sdk.build_in_background(
                    recipe(profile, sdk),
                    name=name,
                    cpu_count=2,
                    memory_mb=2048,
                    request_timeout=15,
                )
                record = {
                    "template_id": build.template_id,
                    "build_id": build.build_id,
                    "status": "building",
                    "updated": time.time(),
                }
                _save(path, record)
            while True:
                try:
                    status = sdk.get_build_status(build, request_timeout=10)
                except Exception as error:
                    from e2b import NotFoundException

                    if isinstance(error, NotFoundException):
                        # A template deleted in E2B may be rebuilt on the next
                        # request; never keep a stale successful cache forever.
                        _save(path, {"status": "error", "updated": 0})
                    raise
                if status.status == "ready":
                    _save(path, {**record, "status": "ready", "updated": time.time()})
                    return {
                        **result,
                        "template": build.template_id,
                        "build_id": build.build_id,
                    }
                if status.status == "error":
                    _save(path, {**record, "status": "error", "updated": time.time()})
                    raise RuntimeError("Runtime template build failed")
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(2)
    except Exception as error:
        # Build errors must not break task creation or leak API credentials/logs.
        logger.warning(
            "Automatic sandbox template unavailable (%s)", type(error).__name__
        )
        return {
            **result,
            "source": "fallback",
            "reason": "Runtime template is not ready; using the general-purpose environment. A pending build can be reused by a later task.",
        }
