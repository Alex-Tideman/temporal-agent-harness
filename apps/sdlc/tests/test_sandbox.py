"""Exercise the real remote helper through an E2B transport double.

The helper runs as an isolated Python script with no app/SDK imports. These tests
verify the RPC contract, not E2B's VM isolation or availability.
"""

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_builder import reviews, sandbox, sandbox_templates
from sdlc_builder import workspaces as ws
from sdlc_builder.coding_models import FileChange, ToolCall
from sdlc_builder.coding_workspace import coding_operation, revision
from sdlc_builder.store import Store, workspace


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "e2b")
    monkeypatch.setenv("E2B_API_KEY", "test-control-plane-secret")
    sandbox._clients.clear()
    clients, created, connected, paused, killed = {}, [], [], [], []

    class Client:
        def __init__(self, identity):
            self.sandbox_id = identity
            self.root = tmp_path / identity
            self.files = SimpleNamespace(write=self.write, read=self.read)
            self.commands = SimpleNamespace(run=self.run)

        def set_timeout(self, timeout):
            assert timeout == sandbox.IDLE_TIMEOUT

        def connect(self, *, timeout):
            assert timeout == sandbox.IDLE_TIMEOUT
            return self

        def translate(self, path):
            return self.root / path.lstrip("/")

        def write(self, path, data):
            target = self.translate(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.endswith(".json"):
                payload = json.loads(data)
                if "operation" in payload:
                    assert payload["root"].startswith("/home/user/")
                    payload["root"] = str(self.translate(payload["root"]))
                    if payload["operation"] in {"initialize", "project_prepare"}:
                        payload["args"][0] = str(self.translate(payload["args"][0]))
                    elif payload["operation"] == "project_restore":
                        for field in ("archive", "bundle"):
                            payload["args"][0][field] = str(
                                self.translate(payload["args"][0][field])
                            )
                    data = json.dumps(payload)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())

        def read(self, path, **kwargs):
            target = (
                Path(path)
                if str(path).startswith(str(self.root))
                else self.translate(path)
            )
            assert target.resolve().is_relative_to(self.root.resolve())
            return target.read_bytes()

        def run(self, command, **kwargs):
            if self.sandbox_id in killed:
                from e2b import NotFoundException

                raise NotFoundException("Sandbox was killed")
            args = shlex.split(command)
            if args[0] == "mkdir":
                self.translate(args[-1]).mkdir(parents=True, exist_ok=True)
                return SimpleNamespace(stdout="")
            assert args[:2] == ["python3", "-I"]
            completed = subprocess.run(
                [*args[:2], *[str(self.translate(p)) for p in args[2:]]],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert completed.returncode == 0, completed.stderr
            return SimpleNamespace(stdout=completed.stdout)

    class SDK:
        @staticmethod
        def create(**kwargs):
            created.append(kwargs)
            client = Client(f"sandbox-{len(created)}")
            # Our automatically built templates provide this empty directory.
            client.translate(sandbox.REMOTE_ROOT).mkdir(parents=True)
            clients[client.sandbox_id] = client
            return client

        @staticmethod
        def connect(identity, **kwargs):
            if identity in killed:
                from e2b import NotFoundException

                raise NotFoundException("Sandbox was killed")
            connected.append(identity)
            return clients[identity]

        @staticmethod
        def pause(identity):
            paused.append(identity)

        @staticmethod
        def kill(identity):
            killed.append(identity)

    monkeypatch.setattr(sandbox, "_sdk", lambda: SDK)
    yield SimpleNamespace(
        clients=clients,
        created=created,
        connected=connected,
        paused=paused,
        killed=killed,
    )
    sandbox._clients.clear()


def make_task(remote, task_id="a" * 32):
    source = ws.demo_project()
    (source / ".env").write_text("TRACKED_SECRET=private")
    (source / "private.pem").write_text("PRIVATE KEY")
    (source / "link").symlink_to("greeting.py")
    ws.git(source, "add", ".")
    ws.git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "Tracked excluded content",
    )
    # Uncommitted files and contents never become part of a new workspace.
    (source / "greeting.py").write_text("unfinished source work\n")
    (source / "untracked.txt").write_text("local only")
    prepared = ws.prepare_workspace(str(source), task_id)
    Store().put("tasks", {"id": task_id, "engine": "v2", **prepared})
    return source, prepared, workspace(task_id)


def test_default_fails_closed_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SDLC_WORKSPACE_BACKEND", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_TEMPLATE", raising=False)
    assert sandbox.configuration() == {
        "backend": "e2b",
        "ready": False,
        "template": "auto",
    }
    with pytest.raises(ValueError, match="E2B_API_KEY"):
        ws.prepare_workspace(str(ws.demo_project()), "a" * 32)
    assert not workspace("a" * 32).exists()


def test_retry_legacy_template_failure_reuses_sandbox_and_original_snapshot(
    remote, monkeypatch
):
    source = ws.demo_project()
    task_id = "b" * 32
    original_rpc = sandbox._rpc

    def old_initializer(client, operation, args):
        if operation == "initialize":
            raise ValueError("Workspace already exists")
        return original_rpc(client, operation, args)

    monkeypatch.setattr(sandbox, "_rpc", old_initializer)
    with pytest.raises(ValueError, match="Workspace already exists"):
        ws.prepare_workspace(str(source), task_id)
    root = workspace(task_id)
    meta = json.loads(sandbox.descriptor(root).read_text())
    # Reproduce the descriptor written by the old app in the reported failure.
    for field in ("source_dirty", "parent_task_id", "initialization_patch"):
        meta.pop(field)
    sandbox._save(root, meta)
    original_source = (source / "greeting.py").read_text()
    (source / "greeting.py").write_text("new local edits")
    monkeypatch.setattr(sandbox, "_rpc", original_rpc)
    prepared = sandbox.retry_preparation({"id": task_id, "input": {}})
    assert len(remote.created) == 1
    assert prepared["sandbox_id"] == meta["sandbox_id"]
    assert prepared["base_commit"] == meta["source_base"]
    assert ws.read_file(root, "greeting.py")["content"] == original_source
    assert (source / "greeting.py").read_text() == "new local edits"
    assert json.loads(sandbox.descriptor(root).read_text())["status"] == "ready"


def test_retry_lost_initialization_reply_uses_receipt_without_repeating_patch(
    remote, monkeypatch
):
    source = ws.demo_project()
    original_rpc = sandbox._rpc

    def lost_reply(client, operation, args):
        result = original_rpc(client, operation, args)
        if operation == "initialize":
            raise TimeoutError("reply lost after successful initialization")
        return result

    monkeypatch.setattr(sandbox, "_rpc", lost_reply)
    with pytest.raises(ValueError, match="TimeoutError"):
        ws.prepare_workspace(str(source), "c" * 32)
    client = remote.clients["sandbox-1"]
    path = client.translate(sandbox.REMOTE_ROOT) / "greeting.py"
    path.write_text("preserve an existing edit")
    monkeypatch.setattr(sandbox, "_rpc", original_rpc)
    prepared = sandbox.retry_preparation({"id": "c" * 32, "input": {}})
    assert prepared["sandbox_id"] == "sandbox-1" and len(remote.created) == 1
    assert path.read_text() == "preserve an existing edit"


@pytest.mark.parametrize("existing", ["files", "symlink", "file"])
def test_initialize_preserves_existing_content_and_rejects_links(
    remote, tmp_path, existing
):
    from sdlc_builder import workspace_runtime as runtime

    source = ws.demo_project()
    archive = tmp_path / "snapshot.tar.gz"
    archive.write_bytes(
        sandbox.source_archive(source, ws.git(source, "rev-parse", "HEAD").strip())
    )
    root = tmp_path / "destination"
    if existing == "symlink":
        root.symlink_to(source, target_is_directory=True)
    elif existing == "file":
        root.write_text("keep")
    else:
        root.mkdir()
        (root / "valuable.txt").write_text("keep")
    before = (source / "greeting.py").read_text()
    with pytest.raises(ValueError, match="preserved|symbolic link"):
        runtime.initialize(root, str(archive))
    assert (source / "greeting.py").read_text() == before
    if existing == "files":
        assert (root / "valuable.txt").read_text() == "keep"
    elif existing == "file":
        assert root.read_text() == "keep"


def test_template_selection_uses_committed_files_and_forks_preserve_it(
    remote, monkeypatch
):
    source = ws.demo_project()
    package = source / "package.json"
    package.write_text(json.dumps({"packageManager": "bun@1.2.3"}))
    ws.git(source, "add", "package.json")
    ws.git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "Add manifest",
    )
    package.write_text(json.dumps({"packageManager": "pnpm@10.5.2"}))
    selections = []

    def choose(archive, override):
        profile = sandbox_templates.detect(archive)
        selections.append(profile)
        return {
            "template": "bun-template",
            "profile": profile,
            "label": "Bun 1.2.3",
            "source": "automatic",
            "reason": "Detected Bun",
        }

    monkeypatch.setattr(sandbox_templates, "select", choose)
    root = workspace("a" * 32)
    prepared = ws.prepare_workspace(str(source), root.name)
    Store().put("tasks", {"id": root.name, "engine": "v2", **prepared})
    assert selections[0]["managers"] == {"bun": "1.2.3"}
    assert remote.created[0]["template"] == "bun-template"
    assert prepared["sandbox_environment"]["source"] == "automatic"
    assert (
        json.loads(sandbox.descriptor(root).read_text())["template_selection"][
            "template"
        ]
        == "bun-template"
    )
    monkeypatch.setenv("E2B_TEMPLATE", "different-default")
    fork = ws.continue_workspace(root.name, "b" * 32)
    assert fork["sandbox_template"] == "bun-template"
    assert len(selections) == 1 and remote.created[1]["template"] == "bun-template"


@pytest.mark.parametrize("missing", [True, False])
def test_only_definite_missing_template_retries_sandbox_creation(
    remote, monkeypatch, missing
):
    from e2b import NotFoundException

    sdk = sandbox._sdk()
    create = sdk.create
    attempts = []

    def create_with_failure(**kwargs):
        attempts.append(kwargs["template"])
        if kwargs["template"] != "base":
            raise (
                NotFoundException("removed")
                if missing
                else ConnectionError("lost response")
            )
        return create(**kwargs)

    monkeypatch.setattr(sdk, "create", create_with_failure)
    monkeypatch.setattr(
        sandbox_templates,
        "select",
        lambda *args: {"template": "gone", "source": "automatic"},
    )
    if missing:
        prepared = ws.prepare_workspace(str(ws.demo_project()), "a" * 32)
        assert prepared["sandbox_template"] == "base"
        assert attempts == ["gone", "base"]
    else:
        with pytest.raises(ValueError, match="preparation failed"):
            ws.prepare_workspace(str(ws.demo_project()), "a" * 32)
        assert attempts == ["gone"]


async def test_remote_workspace_full_lifecycle(remote, monkeypatch):
    source, prepared, root = make_task(remote)
    client = remote.clients[prepared["sandbox_id"]]
    actual = client.translate(sandbox.REMOTE_ROOT)
    assert prepared["workspace"].startswith("e2b://")
    assert prepared["source_dirty"]
    assert not list(root.iterdir())  # Only metadata lives on the host.
    assert not any(
        (actual / name).exists()
        for name in [".env", "private.pem", "link", "untracked.txt"]
    )
    assert "test-control-plane-secret" not in json.dumps(remote.created)
    assert remote.created[0]["secure"] is True
    assert remote.created[0]["lifecycle"] == {
        "on_timeout": "pause",
        "auto_resume": True,
    }
    assert ws.files(root) == ["README.md", "greeting.py", "test_greeting.py"]
    assert ws.search_files(root, "greet")[0]["path"]
    read = await coding_operation(
        root.name,
        "read-many",
        ToolCall(kind="read_many", paths=["greeting.py", "test_greeting.py"]),
    )
    assert len(read.views) == 2
    original = read.views[0]
    result = await coding_operation(
        root.name,
        "patch-1",
        ToolCall(
            kind="patch",
            changes=[
                FileChange(
                    path="greeting.py",
                    expected_hash=original.hash,
                    content='def greet(name):\n    return f"Hello, {name}!"\n',
                )
            ],
        ),
    )
    assert result.ok and result.before_revision != result.revision
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    check = ToolCall(
        kind="check",
        expected_revision=result.revision,
        argv=["python3", "-m", "unittest", "-v"],
    )
    checked = await coding_operation(root.name, "check-1", check)
    assert checked.ok and checked.exit_code == 0
    assert checked.before_revision == checked.revision == result.revision
    assert await coding_operation(root.name, "check-1", check) == checked
    env = await ws.execute_check(
        root,
        [
            "python3",
            "-c",
            "import os; print(os.environ.get('OPENAI_API_KEY')); print(os.environ.get('E2B_API_KEY'))",
        ],
    )
    assert env["output"].strip() == "None\nNone"
    home = await ws.execute_check(
        root, ["python3", "-c", "import os; print(os.environ['HOME'])"]
    )
    assert home["output"].strip().startswith(str(actual.parent) + "/")
    stale = await coding_operation(
        root.name,
        "stale",
        ToolCall(
            kind="check", expected_revision=result.before_revision, argv=["false"]
        ),
    )
    assert not stale.ok and "after command approval" in stale.error
    state = {"version": 0, "comments": [], "checkpoints": {}}
    summary = reviews.summary(root, prepared, state)
    assert [f["path"] for f in summary["files"]] == ["greeting.py"]
    assert reviews.detail(root, prepared, state, "greeting.py", "all")["changed"]
    assert ws.diff(root, prepared["base_commit"])["files"] == ["greeting.py"]
    assert (source / "greeting.py").read_text() == "unfinished source work\n"
    # Restart resumes the same ID and filesystem.
    sandbox._clients.clear()
    assert revision(root) == result.revision
    assert remote.connected == [prepared["sandbox_id"]]
    fork = ws.continue_workspace(root.name, "b" * 32)
    fork_root = workspace("b" * 32)
    assert fork["sandbox_id"] != prepared["sandbox_id"]
    assert ws.read_file(fork_root, "greeting.py") == ws.read_file(root, "greeting.py")
    # Return the source file to its committed baseline, then exercise the normal
    # guarded host merge using patches from the remote snapshot.
    ws.git(source, "checkout", "--", "greeting.py")
    merged = ws.merge_to_project(
        root, prepared["base_commit"], str(source), result.revision
    )
    assert merged["applied_files"] == ["greeting.py"]
    assert "Hello" in (source / "greeting.py").read_text()
    sandbox.release(root, remove=False)
    assert sandbox.is_remote(root) and remote.paused == [prepared["sandbox_id"]]
    sandbox.release(root, remove=True)
    assert not sandbox.is_remote(root) and remote.killed == [prepared["sandbox_id"]]


async def test_remote_failures_never_execute_on_host(remote, monkeypatch):
    _, prepared, root = make_task(remote)
    client = remote.clients[prepared["sandbox_id"]]

    def disconnected(*args, **kwargs):
        raise ConnectionError("network interrupted")

    monkeypatch.setattr(client.commands, "run", disconnected)
    monkeypatch.setattr(
        ws.runtime, "execute_check", lambda *args: pytest.fail("host fallback")
    )
    result = await coding_operation(
        root.name,
        "lost-check",
        ToolCall(kind="check", argv=["true"], expected_revision="x"),
    )
    assert not result.ok and "E2B workspace unavailable" in result.error
    assert not list(root.iterdir())


async def test_lost_remote_command_reply_is_not_repeated(remote, monkeypatch):
    _, prepared, root = make_task(remote)
    expected = revision(root)
    client = remote.clients[prepared["sandbox_id"]]
    original = client.commands.run

    def lose_reply(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("completion was lost")

    monkeypatch.setattr(client.commands, "run", lose_reply)
    call = ToolCall(
        kind="check",
        expected_revision=expected,
        argv=[
            "python3",
            "-c",
            "from pathlib import Path; p=Path('counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
        ],
    )
    result = await coding_operation(root.name, "lost-result", call)
    assert not result.ok
    assert await coding_operation(root.name, "lost-result", call) == result
    assert (client.translate(sandbox.REMOTE_ROOT) / "counter").read_text() == "x"


def test_merge_rejects_patch_paths_misreported_by_sandbox(remote, monkeypatch):
    source, prepared, root = make_task(remote)
    for name in ("greeting.py", "README.md"):
        old = ws.read_file(root, name)
        ws.write_file(root, name, "modified\n", old["hash"])
    actual_patch = ws.diff(root)["patch"]
    monkeypatch.setattr(ws, "changed_paths", lambda *args: ["greeting.py"])
    monkeypatch.setattr(
        ws, "diff_entries", lambda *args: [("greeting.py", actual_patch)]
    )
    with pytest.raises(ValueError, match="unsupported patch"):
        ws.merge_to_project(root, prepared["base_commit"], str(source), revision(root))
    assert (source / "greeting.py").read_text() == "unfinished source work\n"


def test_deleted_remote_sandbox_never_becomes_a_local_workspace(remote, monkeypatch):
    _, _, root = make_task(remote)
    sandbox._clients.clear()
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "local")

    class Missing:
        @staticmethod
        def connect(*args, **kwargs):
            raise RuntimeError("sandbox no longer exists")

    monkeypatch.setattr(sandbox, "_sdk", lambda: Missing)
    with pytest.raises(ValueError, match="E2B workspace unavailable"):
        ws.files(root)


async def test_pre_batching_command_journal_still_blocks_reexecution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    identity = "a" * 32
    call = ToolCall(kind="check", argv=["false"], expected_revision="old")
    fingerprint = hashlib.sha256(
        call.model_dump_json(exclude={"paths"}).encode()
    ).hexdigest()
    journal = tmp_path / "coding-operations" / identity
    journal.mkdir(parents=True)
    (journal / "previous.json").write_text(json.dumps({"fingerprint": fingerprint}))
    result = await coding_operation(identity, "previous", call)
    assert not result.ok and "not run twice" in result.error


def test_large_repository_diff_and_search_use_bounded_git_processes(
    tmp_path, monkeypatch
):
    from sdlc_builder import workspace_runtime as runtime

    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    root = ws.demo_project()
    for i in range(150):
        (root / f"file-{i}.txt").write_text("unmatched content\n")
    ws.git(root, "add", ".")
    ws.git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "Many files",
    )
    (root / "greeting.py").write_text("changed\n")
    calls = []
    original = runtime.subprocess.run

    def track(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", track)
    assert ws.diff(root)["files"] == ["greeting.py"]
    assert len(calls) <= 6
    calls.clear()
    assert ws.search_files(root, "not present") == []
    assert len(calls) == 1
