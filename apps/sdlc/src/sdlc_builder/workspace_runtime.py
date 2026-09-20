"""Standard-library workspace operations, shared by local and E2B execution.

This module runs inside the sandbox without installing the app or its providers.
"""

import asyncio
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

MAX_SNAPSHOT = 256_000_000

MAX_FILE = 100_000
DENIED_PARTS = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".ssh",
    ".aws",
    ".gnupg",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[:1000] or "Git command failed")
    return result.stdout


def allowed(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        bool(parts)
        and not Path(relative).is_absolute()
        and all(
            p not in DENIED_PARTS
            and p not in {"..", "."}
            and not p.lower().startswith(".env")
            and not p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            and p.lower()
            not in {
                "credentials",
                "credentials.json",
                "secrets.json",
                "secrets.yaml",
                "secrets.yml",
                "secrets.toml",
                ".npmrc",
                ".pypirc",
                ".netrc",
                ".git-credentials",
                "id_rsa",
                "id_ed25519",
            }
            for p in parts
        )
    )


def safe_path(root: Path, relative: str, *, check_ignored: bool = True) -> Path:
    if not allowed(relative):
        raise ValueError(
            "Path is outside the workspace or excluded as a secret/generated file"
        )
    path = root / relative
    for ancestor in [path, *path.parents]:
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("Symlinks are not supported by workspace tools")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes workspace")
    # Ignored local files (including provider credentials) are never tool inputs.
    if not check_ignored:
        return path
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative],
        cwd=root,
        capture_output=True,
    )
    if ignored.returncode == 0:
        raise ValueError("Ignored files are excluded")
    return path


def files(root: Path) -> list[str]:
    names = git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ).split("\0")
    return sorted({p for p in names if allowed(p) and not (root / p).is_symlink()})[
        :3000
    ]


def read_file(root: Path, relative: str) -> dict:
    return _read_file(root, relative)


def _read_file(root: Path, relative: str, *, check_ignored: bool = True) -> dict:
    path = safe_path(root, relative, check_ignored=check_ignored)
    if not path.is_file():
        return {"path": relative, "exists": False, "hash": "", "content": ""}
    if path.stat().st_size > MAX_FILE:
        raise ValueError("File exceeds the 100 KB text limit")
    raw = path.read_bytes()
    if b"\0" in raw:
        raise ValueError("Binary file")
    return {
        "path": relative,
        "exists": True,
        "hash": hashlib.sha256(raw).hexdigest(),
        "content": raw.decode("utf-8"),
    }


def write_file(root: Path, relative: str, content: str, expected_hash: str) -> dict:
    path = safe_path(root, relative)
    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE:
        raise ValueError("File exceeds the 100 KB text limit")
    current = read_file(root, relative)
    digest = hashlib.sha256(raw).hexdigest()
    # Recovery after write succeeded but activity completion was lost.
    if current["exists"] and current["hash"] == digest:
        return {"path": relative, "hash": digest, "unchanged": True}
    if current["hash"] != expected_hash:
        raise ValueError(
            "File changed since it was read. Read it again before editing."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp = Path(tmp.name)
    if path.exists():
        temp.chmod(path.stat().st_mode & 0o777)
    temp.replace(path)
    return {"path": relative, "hash": digest}


def diff_entries(root: Path, base_revision: str = "HEAD") -> list[tuple[str, str]]:
    """Return one supported text patch per changed path."""
    output = []
    # Include files deleted by a later local commit when exporting from a task's
    # original base. Agent tools retain their existing HEAD comparison.
    names = changed_paths(root, base_revision)
    for name in sorted(name for name in names if allowed(name)):
        try:
            read_file(
                root, name
            )  # Enforce size, binary, secret, and symlink exclusions.
            base = subprocess.run(
                ["git", "show", f"{base_revision}:{name}"],
                cwd=root,
                capture_output=True,
                timeout=5,
            )
            if len(base.stdout) > MAX_FILE:
                continue
            if base.returncode == 0:
                patch = git(
                    root,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    base_revision,
                    "--",
                    name,
                )
            else:
                result = subprocess.run(
                    [
                        "git",
                        "diff",
                        "--no-index",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-color",
                        "--",
                        "/dev/null",
                        name,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                patch = result.stdout
            if patch:
                output.append((name, patch))
        except (ValueError, UnicodeError):
            continue
    return output


def changed_paths(root: Path, base_revision: str) -> list[str]:
    """List every tracked or untracked path changed from the task base."""
    tracked = git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "-z",
        base_revision,
        "--",
    ).split("\0")
    untracked = git(root, "ls-files", "-z", "--others", "--exclude-standard").split(
        "\0"
    )
    return sorted({name for name in [*tracked, *untracked] if name})


def diff(root: Path, base_revision: str = "HEAD") -> dict:
    # Include untracked files without mutating the index; filter secrets even if tracked.
    entries = diff_entries(root, base_revision)
    patch = "".join(value for _, value in entries)
    return {
        "files": [name for name, _ in entries],
        "patch": patch[:500_000],
        "truncated": len(patch) > 500_000,
    }


def git_apply(root: Path, patch: str, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "apply",
            "--whitespace=nowarn",
            *options,
            "--",
        ],
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        timeout=45,
    )


async def execute_check(
    root: Path, argv: list[str], scratch: Path | None = None
) -> dict:
    if not argv or len(argv) > 100 or any("\0" in arg for arg in argv):
        raise ValueError("Expected a nonempty argument vector")
    # E2B commands run as the unprivileged user, who cannot write under /home.
    # Local callers explicitly supply their historical command-home location.
    scratch = scratch or root.parent / ".boltzmann" / "command-homes" / root.name
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Legacy local commands still require explicit approval. For E2B this entire
    # helper, process tree, home and filesystem live inside the VM.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks = bytearray()
    reason = ""
    try:
        async with asyncio.timeout(90):
            while block := await process.stdout.read(8192):
                chunks.extend(block)
                if len(chunks) > 100_000:
                    reason = "Stopped at 100 KB output limit"
                    break
            if not reason:
                await process.wait()
    except TimeoutError:
        reason = "Stopped at 90 second limit"
    finally:
        # Also remove surviving children if the main process exited successfully.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    return {
        "exit_code": process.returncode if not reason else -1,
        "output": chunks[:100_000].decode("utf-8", errors="replace")
        + ("\n" + reason if reason else ""),
    }


def search_files(root: Path, query: str) -> list[dict]:
    import time

    deadline = time.monotonic() + 20
    hits = []
    needle = query.lower()
    for name in files(root):
        if time.monotonic() > deadline or len(hits) >= 80:
            break
        try:
            for number, line in enumerate(
                _read_file(root, name, check_ignored=False)["content"].splitlines(), 1
            ):
                if needle in line.lower():
                    hits.append({"path": name, "line": number, "text": line[:400]})
                if len(hits) >= 80:
                    break
        except (ValueError, UnicodeError):
            pass
    return hits


def revision(root: Path) -> str:
    # No UI 3,000-file cap: incomplete snapshots must never count as verified.
    names = sorted(
        set(
            git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split("\0")
        )
        - {""}
    )
    if len(names) > 50000:
        raise ValueError("Repository exceeds the 50,000-file verification limit")
    digest = hashlib.sha256()
    total = 0
    for name in names:
        if not allowed(name):
            continue
        path = root / name
        # Include symlink identity, deleted files, modes and binary/large files,
        # without following symlinks or exposing their contents to the model.
        if any(
            p.is_symlink()
            for p in [path, *path.parents]
            if p != root and p.is_relative_to(root)
        ):
            raw = (
                ("symlink:" + os.readlink(path)).encode()
                if path.is_symlink()
                else b"symlink-parent"
            )
        elif path.is_file():
            size = path.stat().st_size
            total += size
            if total > 256_000_000:
                raise ValueError(
                    "Repository exceeds the 256 MB verification snapshot limit"
                )
            raw = path.read_bytes()
        else:
            raw = b"deleted"
        mode = (
            str(path.lstat().st_mode)
            if path.exists() or path.is_symlink()
            else "missing"
        )
        digest.update(
            json.dumps([name, mode, hashlib.sha256(raw).hexdigest()]).encode()
        )
    return digest.hexdigest()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(raw: bytes = b"", *, exists=False, mode="", issue="") -> dict:
    content = ""
    if exists and not issue:
        try:
            if b"\0" in raw:
                raise UnicodeError()
            content = raw.decode("utf-8")
        except UnicodeError:
            issue = "Binary or non-UTF-8 file. Inspect this file in your local editor."
    value = {
        "exists": exists,
        "mode": mode,
        "content": content,
        "issue": issue,
        "hash": hashlib.sha256(raw).hexdigest() if exists else "",
    }
    value["token"] = digest(value)
    return value


def source_snapshot(root: Path, path: str) -> dict:
    # Never dereference links, including links in parent directories.
    target = safe_path(root, path)
    if not target.exists():
        return snapshot()
    if not target.is_file():
        return snapshot(exists=True, issue="Not a regular file; inspect locally.")
    stat = target.stat()
    mode = "100755" if stat.st_mode & 0o111 else "100644"
    if stat.st_size > MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="File exceeds the 100 KB review limit."
        )
    with target.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="File exceeds the 100 KB review limit."
        )
    return snapshot(raw, exists=True, mode=mode)


def base_snapshot(root: Path, base: str, path: str) -> dict:
    # --literal-pathspecs in git protects unusual Git filenames.
    record = git(root, "ls-tree", "-l", "-z", base, "--", path)
    if not record:
        return snapshot()
    meta, name = record.rstrip("\0").split("\t", 1)
    mode, kind, oid, size = meta.split()
    if name != path or kind != "blob" or mode == "120000":
        return snapshot(
            exists=True,
            mode=mode,
            issue="Link or non-regular base file; inspect locally.",
        )
    if int(size) > MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="Base file exceeds the 100 KB review limit."
        )
    raw = subprocess.run(
        ["git", "cat-file", "blob", oid],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    return snapshot(raw, exists=True, mode=mode)


def review_snapshots(root: Path, base: str, paths: list[str]) -> dict:
    """One remote request for a review, rather than two requests per file."""
    result = {}
    for path in paths[:500]:
        try:
            result[path] = [
                base_snapshot(root, base, path),
                source_snapshot(root, path),
            ]
        except ValueError as error:
            result[path] = {"error": str(error)}
    return result


def read_many(root: Path, paths: list[str]) -> list[dict]:
    if not 1 <= len(paths) <= 20:
        raise ValueError("Read between 1 and 20 files per batch")
    return [read_file(root, path) for path in paths]


def patch_files(root: Path, changes: list[dict]) -> list[str]:
    if not changes or len({c["path"] for c in changes}) != len(changes):
        raise ValueError("Patch needs unique file paths")
    if sum(len(c["content"].encode()) for c in changes) > 500000:
        raise ValueError("Patch exceeds 500 KB")
    for change in changes:
        current = read_file(root, change["path"])
        desired = hashlib.sha256(change["content"].encode()).hexdigest()
        if current["hash"] not in {change["expected_hash"], desired}:
            raise ValueError(
                f"{change['path']} changed since it was read; read it again"
            )
        if len(change["content"].encode()) > MAX_FILE:
            raise ValueError("File exceeds 100 KB")
    for change in changes:
        write_file(root, change["path"], change["content"], change["expected_hash"])
    return [c["path"] for c in changes]


def patch_operation(root: Path, changes: list[dict]) -> dict:
    before = revision(root)
    paths = patch_files(root, changes)
    return {"before_revision": before, "files": paths, "revision": revision(root)}


def check_operation(root: Path, argv: list[str], expected_revision: str) -> dict:
    before = revision(root)
    if before != expected_revision:
        raise ValueError(
            "Workspace changed after command approval. Review and approve the new revision."
        )
    checked = asyncio.run(execute_check(root, argv))
    return {**checked, "before_revision": before, "revision": revision(root)}


def initialize(root: Path, archive_path: str, patch: str = "") -> str:
    """Unpack a bounded source snapshot; never extract links or Git configuration."""
    # Templates may supply an empty working directory. Only a matching receipt
    # permits reusing a populated one after a lost initialization response.
    fingerprint = hashlib.sha256(
        Path(archive_path).read_bytes() + patch.encode()
    ).hexdigest()
    receipt = Path(archive_path).with_suffix(".initialized.json")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Workspace must be a directory, not a file or symbolic link")
    if receipt.is_file():
        previous = json.loads(receipt.read_text())
        if (
            previous.get("fingerprint") == fingerprint
            and previous.get("root") == str(root)
            and git(root, "rev-parse", "HEAD").strip() == previous["base"]
        ):
            return previous["base"]
        raise ValueError(
            "Workspace setup receipt does not match. The existing files were preserved; start a new task."
        )
    if root.exists() and any(root.iterdir()):
        raise ValueError(
            "Workspace already contains files. They were preserved; start a new task for a fresh workspace."
        )
    root.mkdir(parents=True, exist_ok=True)
    total = count = 0
    with tarfile.open(archive_path) as archive:
        for member in archive:
            if not member.isfile() or not allowed(member.name):
                continue
            total += member.size
            count += 1
            if total > MAX_SNAPSHOT or count > 50000:
                raise ValueError("Repository exceeds sandbox snapshot limits")
            path = safe_path(root, member.name, check_ignored=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as incoming, path.open("wb") as outgoing:
                import shutil

                shutil.copyfileobj(incoming, outgoing)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    git(root, "init", "-b", "main")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "add", "--force", ".")
    git(
        root,
        "-c",
        "user.name=boltzmann",
        "-c",
        "user.email=sandbox@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "Workspace base",
    )
    base = git(root, "rev-parse", "HEAD").strip()
    if patch and git_apply(root, patch).returncode:
        raise ValueError("Could not carry forward the previous patch")
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"fingerprint": fingerprint, "root": str(root), "base": base})
    )
    temporary.replace(receipt)
    return base


def export_base(root: Path, base: str = "HEAD") -> str:
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            ["git", "archive", "--format=tar.gz", base],
            cwd=root,
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
            timeout=45,
        )
        if output.tell() > MAX_SNAPSHOT:
            raise ValueError("Repository exceeds sandbox snapshot limits")
        output.seek(0)
        return base64.b64encode(output.read()).decode()


# The RPC surface is deliberately explicit. No arbitrary function or host path
# supplied by a model is ever used as an execution entry point.
OPERATIONS = {
    fn.__name__: fn
    for fn in (
        git,
        files,
        read_file,
        read_many,
        write_file,
        diff_entries,
        changed_paths,
        diff,
        search_files,
        revision,
        source_snapshot,
        base_snapshot,
        review_snapshots,
        patch_operation,
        check_operation,
        initialize,
        export_base,
    )
}


def main():
    request = Path(sys.argv[1])
    try:
        payload = json.loads(request.read_text())
        started = time.monotonic()
        if payload["operation"] == "execute_check":
            result = asyncio.run(execute_check(Path(payload["root"]), *payload["args"]))
        else:
            result = OPERATIONS[payload["operation"]](
                Path(payload["root"]), *payload["args"]
            )
        print(
            json.dumps(
                {
                    "result": result,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
        )
    except Exception as error:
        print(json.dumps({"error": str(error)[:1000]}))
    finally:
        request.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
