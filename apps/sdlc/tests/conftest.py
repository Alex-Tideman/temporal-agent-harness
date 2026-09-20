import pytest


@pytest.fixture(autouse=True)
def local_test_workspaces(monkeypatch):
    """Existing local fixtures never create billable remote resources implicitly."""
    monkeypatch.setenv("SDLC_WORKSPACE_BACKEND", "local")
