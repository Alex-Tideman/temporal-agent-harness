import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sdlc_builder import sandbox_templates as templates


def archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, value in files.items():
            content = (json.dumps(value) if isinstance(value, dict) else value).encode()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("E2B_API_KEY", "private-test-key")
    builds = []
    states = ["ready"]
    checks = []

    class Template:
        def __getattr__(self, name):
            # Keep recipe serialization separate from network behavior.
            return lambda *args, **kwargs: self

        @staticmethod
        def build_in_background(recipe, **kwargs):
            builds.append(kwargs)
            return SimpleNamespace(template_id="managed-template", build_id="build-id")

        @staticmethod
        def get_build_status(build, **kwargs):
            checks.append(build)
            value = states.pop(0) if len(states) > 1 else states[0]
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(status=value)

    monkeypatch.setattr(templates, "_sdk", lambda: Template)
    monkeypatch.setattr(templates.time, "sleep", lambda seconds: None)
    return SimpleNamespace(builds=builds, states=states, checks=checks, path=tmp_path)


def test_monorepo_detects_runtime_pins_and_package_managers():
    profile = templates.detect(
        archive(
            {
                ".node-version": "v22.14.0\n",
                "package.json": {
                    "private": True,
                    "packageManager": "pnpm@10.5.2+sha512.abc123",
                },
                "pnpm-lock.yaml": "lockfileVersion: 9",
                "apps/web/package.json": {"scripts": {"dev": "vite"}},
                "apps/api/.python-version": "3.12.8",
                "apps/api/pyproject.toml": '[project]\nrequires-python = ">=3.11"',
            }
        )
    )
    assert profile == {
        "node": "22.14.0",
        "python": "3.12.8",
        "managers": {"pnpm": "10.5.2"},
    }


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("22.14.0", "22.14.0"),
        ("^22.12.0", "22"),
        ("~20.19.0", "20.19"),
        (">=22.12.0", "22.12.0"),
        ("22.x", "22"),
    ],
)
def test_node_engine_versions(engine, expected):
    assert (
        templates.detect(archive({"package.json": {"engines": {"node": engine}}}))[
            "node"
        ]
        == expected
    )


def test_bun_pin_and_python_lower_bound():
    profile = templates.detect(
        archive(
            {
                "ui/package.json": {"packageManager": "bun@1.2.3"},
                "ui/bun.lock": "",
                "pyproject.toml": '[project]\nrequires-python = ">=3.11.8,<3.12"',
            }
        )
    )
    assert profile == {"node": "24", "python": "3.11.8", "managers": {"bun": "1.2.3"}}


def test_malformed_or_hostile_manifests_do_not_become_build_commands():
    profile = templates.detect(
        archive(
            {
                ".nvmrc": "24; curl https://attacker.invalid",
                "package.json": {
                    "packageManager": "bun@1.2.3 && stolen-command",
                    "volta": {"node": "$(stolen-command)"},
                    "scripts": {"postinstall": "stolen-command"},
                },
                "apps/broken/package.json": "{invalid",
                "pyproject.toml": "[[invalid",
                "Dockerfile": "RUN stolen-command",
                "e2b.toml": 'template_id = "untrusted"',
            }
        )
    )
    from e2b import Template

    value = Template.to_json(templates.recipe(profile, Template))
    assert "stolen-command" not in value and "attacker.invalid" not in value
    assert "COPY" not in value.upper()
    assert "python3 --version" in value
    assert "node:24-bookworm" in value


def test_oversize_manifests_and_unknown_repositories_use_safe_defaults(registry):
    assert (
        templates.select(
            archive({"README.md": "Hello", "Dockerfile": "FROM arbitrary"})
        )["template"]
        == "base"
    )
    assert not registry.builds
    result = templates.detect(archive({"package.json": " " * 100_001}))
    assert result["node"] == "24"


def test_reuses_template_across_repositories_and_concurrent_tasks(registry):
    contents = archive({"package.json": {"packageManager": "bun@1.2.3"}})
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: templates.select(contents), range(3)))
    assert len(registry.builds) == 1
    assert all(item["template"] == "managed-template" for item in results)
    assert all(item["source"] == "automatic" for item in results)
    assert "private-test-key" not in json.dumps(results + registry.builds)
    cache = list(registry.path.glob("sandbox-templates/*/*.json"))
    assert len(cache) == 1 and cache[0].stat().st_mode & 0o777 == 0o600
    assert "private-test-key" not in cache[0].read_text()
    templates.select(
        archive({"apps/site/package.json": {"packageManager": "bun@1.2.3"}})
    )
    assert len(registry.builds) == 1


def test_version_and_account_changes_use_different_cache_entries(registry, monkeypatch):
    templates.select(archive({"package.json": {"packageManager": "bun@1.2.3"}}))
    templates.select(archive({"package.json": {"packageManager": "bun@1.2.4"}}))
    assert len(registry.builds) == 2
    monkeypatch.setenv("E2B_API_KEY", "another-account")
    templates.select(archive({"package.json": {"packageManager": "bun@1.2.4"}}))
    assert len(registry.builds) == 3


def test_pending_template_does_not_block_task_and_is_reused_later(
    registry, monkeypatch
):
    monkeypatch.setattr(templates, "BUILD_WAIT", 0)
    registry.states[:] = ["building"]
    contents = archive({"package.json": {}})
    result = templates.select(contents)
    assert result["template"] == "base" and result["source"] == "fallback"
    assert len(registry.builds) == 1
    registry.states[:] = ["ready"]
    assert templates.select(contents)["template"] == "managed-template"
    assert len(registry.builds) == 1


def test_failed_build_is_not_repeated_on_every_task(registry):
    registry.states[:] = ["error"]
    contents = archive({"package.json": {}})
    assert templates.select(contents)["source"] == "fallback"
    assert templates.select(contents)["source"] == "fallback"
    assert len(registry.builds) == 1


def test_network_error_does_not_leak_credentials_or_rebuild(registry):
    registry.states[:] = [ConnectionError("private-test-key")]
    contents = archive({"package.json": {}})
    result = templates.select(contents)
    assert result["source"] == "fallback" and "private-test-key" not in json.dumps(
        result
    )
    registry.states[:] = ["ready"]
    assert templates.select(contents)["template"] == "managed-template"
    assert len(registry.builds) == 1


def test_deleted_template_is_rebuilt(registry):
    from e2b import NotFoundException

    contents = archive({"package.json": {}})
    assert templates.select(contents)["source"] == "automatic"
    registry.states[:] = [NotFoundException("deleted")]
    assert templates.select(contents)["source"] == "fallback"
    registry.states[:] = ["ready"]
    assert templates.select(contents)["source"] == "automatic"
    assert len(registry.builds) == 2


def test_custom_launch_override_bypasses_builds(registry):
    result = templates.select(archive({"package.json": {}}), "team/custom")
    assert result["template"] == "team/custom" and result["source"] == "override"
    assert not registry.builds


def test_modern_yarn_uses_prebuilt_cache_with_sanitized_home():
    from e2b import Template

    profile = templates.detect(
        archive({"package.json": {"packageManager": "yarn@4.7.0"}})
    )
    value = Template.to_json(templates.recipe(profile, Template))
    assert "corepack prepare yarn@4.7.0" in value
    assert "COREPACK_HOME=/opt/boltzmann-corepack exec" in value
    assert "yarn --version" in value
