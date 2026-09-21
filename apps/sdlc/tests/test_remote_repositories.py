from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_builder import remote_repositories as remote


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/repo",
        "git@github.com:owner/repo",
        "https://token@github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "https://github.com/owner/repo#branch",
        "https://localhost/owner/repo",
        "https://github.com/../repo",
        "https://github.com/owner/.git",
        "https://github.com/owner/repo/tree/main",
    ],
)
def test_remote_urls_reject_local_paths_credentials_and_unexpected_hosts(value):
    with pytest.raises(ValueError):
        remote.canonical_url(value)


def test_canonical_urls_deduplicate():
    assert (
        remote.canonical_url(" https://github.com/Owner/Repo.git/ ")
        == "https://github.com/owner/repo"
    )


def test_credentials_are_scoped_to_askpass_and_errors_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_GITHUB_TOKEN", "test-git-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "test-model-secret")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert "test-git-secret" not in str(args)
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["BOLTZMANN_GIT_TOKEN"] == "test-git-secret"
        askpass = Path(kwargs["env"]["GIT_ASKPASS"])
        assert askpass.stat().st_mode & 0o777 == 0o700
        assert "test-git-secret" not in askpass.read_text()
        assert "credential.helper=" in args and "core.hooksPath=/dev/null" in args
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https"
        return SimpleNamespace(
            returncode=1, stdout="", stderr="test-git-secret is rejected"
        )

    monkeypatch.setattr(remote.subprocess, "run", run)
    with pytest.raises(ValueError, match="Could not access") as caught:
        remote.git(tmp_path, "fetch", "origin")
    assert "test-git-secret" not in str(caught.value)
    assert not Path(calls[0][1]["env"]["GIT_ASKPASS"]).exists()


@pytest.mark.parametrize("fails", [False, True])
def test_clone_publishes_only_a_complete_checkout(tmp_path, monkeypatch, fails):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))

    def git(root, *args):
        if args[0] == "clone":
            Path(args[-1]).mkdir()
            (Path(args[-1]) / "README.md").write_text("repository")
            if fails:
                raise ValueError("clone interrupted")
        if args[0] == "branch":
            return "main"
        if args[0] == "rev-parse":
            return "a" * 40
        return ""

    monkeypatch.setattr(remote, "git", git)
    if fails:
        with pytest.raises(ValueError):
            remote.connect("https://github.com/team/repo", "project")
        assert list((tmp_path / "repositories").iterdir()) == []
    else:
        project = remote.connect("https://github.com/team/repo", "project")
        assert (
            project["default_branch"] == "main" and project["synced_commit"] == "a" * 40
        )
        assert (Path(project["path"]) / "README.md").exists()
        assert [p.name for p in (tmp_path / "repositories").iterdir()] == ["project"]


@pytest.mark.parametrize("dirty,branch", [(True, "main"), (False, "another")])
def test_sync_preserves_uncommitted_work_and_changed_branches(
    tmp_path, monkeypatch, dirty, branch
):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    calls = []

    def git(root, *args):
        calls.append(args)
        return (
            " M edited.txt"
            if args[0] == "status" and dirty
            else branch
            if args[0] == "branch"
            else ""
        )

    monkeypatch.setattr(remote, "git", git)
    with pytest.raises(ValueError):
        remote.sync({"path": str(tmp_path), "default_branch": "main"})
    assert not any(args[0] in {"fetch", "merge"} for args in calls)
