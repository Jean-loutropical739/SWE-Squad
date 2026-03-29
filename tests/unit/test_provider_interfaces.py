"""
Conformance tests for provider interfaces: EnvProvider, WorkspaceProvider, RepoMapProvider.

Verifies that each Protocol is runtime_checkable, that minimal concrete
implementations satisfy isinstance() checks, and that dataclass fields
and constants are correctly defined.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from src.swe_team.providers.env.base import (
    BLOCKED_ENV_VARS,
    EnvProvider,
    EnvSpec,
)
from src.swe_team.providers.workspace.base import (
    WorkspaceInfo,
    WorkspaceProvider,
    WorkspaceSpec,
)
from src.swe_team.providers.repomap.base import (
    RepoMap,
    RepoMapEntry,
    RepoMapProvider,
)


# ---------------------------------------------------------------------------
#  Minimal concrete implementations used for isinstance checks
# ---------------------------------------------------------------------------

class _StubEnvProvider:
    def build_env(self, spec: EnvSpec) -> dict[str, str]:
        return {}

    def allowed_keys(self, role: str) -> list[str]:
        return []

    def is_blocked(self, key: str) -> bool:
        return key in BLOCKED_ENV_VARS

    def health_check(self) -> bool:
        return True


class _StubWorkspaceProvider:
    def create(self, spec: WorkspaceSpec) -> WorkspaceInfo:
        return WorkspaceInfo(
            workspace_id="ws-1",
            ticket_id=spec.ticket_id,
            path=Path("/tmp/ws"),
            role=spec.role,
            created_at=datetime.now(),
        )

    def release(self, workspace_id: str) -> None:
        pass

    def get(self, workspace_id: str) -> WorkspaceInfo | None:
        return None

    def list_active(self) -> list[WorkspaceInfo]:
        return []

    def cleanup_stale(self, max_age_hours: int) -> int:
        return 0

    def health_check(self) -> bool:
        return True


class _StubRepoMapProvider:
    def generate(
        self,
        repo_path: Path,
        max_tokens: int = 2000,
        ignore: list[str] | None = None,
    ) -> RepoMap:
        return RepoMap(entries=[], repo_path=str(repo_path), generated_at="now")

    def is_available(self) -> bool:
        return True

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
#  EnvProvider tests
# ---------------------------------------------------------------------------

class TestEnvProvider:
    """Conformance tests for the EnvProvider interface."""

    def test_protocol_is_runtime_checkable(self):
        assert isinstance(_StubEnvProvider(), EnvProvider)

    def test_non_conforming_class_fails_isinstance(self):
        class _Bad:
            pass
        assert not isinstance(_Bad(), EnvProvider)

    def test_blocked_env_vars_contains_expected_keys(self):
        expected = {
            "SUPABASE_ANON_KEY",
            "BASE_LLM_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "WEBHOOK_SECRET",
            "ANTHROPIC_API_KEY",
            "PROXMOXAI_API_KEY",
        }
        assert expected == BLOCKED_ENV_VARS

    def test_blocked_env_vars_is_frozenset(self):
        assert isinstance(BLOCKED_ENV_VARS, frozenset)

    def test_env_spec_dataclass_fields(self):
        fields = {f.name: f.type for f in dataclasses.fields(EnvSpec)}
        assert "role" in fields
        assert "overrides" in fields
        assert "strip_blocked" in fields

    def test_env_spec_defaults(self):
        spec = EnvSpec(role="developer")
        assert spec.role == "developer"
        assert spec.overrides == {}
        assert spec.strip_blocked is True

    def test_build_env_returns_dict(self):
        provider = _StubEnvProvider()
        result = provider.build_env(EnvSpec(role="investigator"))
        assert isinstance(result, dict)

    def test_allowed_keys_returns_list(self):
        provider = _StubEnvProvider()
        result = provider.allowed_keys("developer")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
#  WorkspaceProvider tests
# ---------------------------------------------------------------------------

class TestWorkspaceProvider:
    """Conformance tests for the WorkspaceProvider interface."""

    def test_protocol_is_runtime_checkable(self):
        assert isinstance(_StubWorkspaceProvider(), WorkspaceProvider)

    def test_non_conforming_class_fails_isinstance(self):
        class _Bad:
            pass
        assert not isinstance(_Bad(), WorkspaceProvider)

    def test_workspace_spec_dataclass_fields(self):
        fields = {f.name for f in dataclasses.fields(WorkspaceSpec)}
        assert fields == {"ticket_id", "role", "base_dir", "ttl_hours", "env_overrides"}

    def test_workspace_spec_defaults(self):
        spec = WorkspaceSpec(ticket_id="T-001")
        assert spec.role == "developer"
        assert spec.base_dir is None
        assert spec.ttl_hours == 48
        assert spec.env_overrides == {}

    def test_workspace_info_dataclass_fields(self):
        fields = {f.name for f in dataclasses.fields(WorkspaceInfo)}
        assert fields == {
            "workspace_id", "ticket_id", "path", "role",
            "created_at", "env_path", "branch",
        }

    def test_workspace_info_optional_defaults(self):
        info = WorkspaceInfo(
            workspace_id="ws-1",
            ticket_id="T-001",
            path=Path("/tmp"),
            role="developer",
            created_at=datetime.now(),
        )
        assert info.env_path is None
        assert info.branch is None

    def test_create_returns_workspace_info(self):
        provider = _StubWorkspaceProvider()
        result = provider.create(WorkspaceSpec(ticket_id="T-002"))
        assert isinstance(result, WorkspaceInfo)
        assert result.ticket_id == "T-002"

    def test_cleanup_stale_returns_int(self):
        provider = _StubWorkspaceProvider()
        result = provider.cleanup_stale(max_age_hours=24)
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
#  RepoMapProvider tests
# ---------------------------------------------------------------------------

class TestRepoMapProvider:
    """Conformance tests for the RepoMapProvider interface."""

    def test_protocol_is_runtime_checkable(self):
        assert isinstance(_StubRepoMapProvider(), RepoMapProvider)

    def test_non_conforming_class_fails_isinstance(self):
        class _Bad:
            pass
        assert not isinstance(_Bad(), RepoMapProvider)

    def test_repo_map_entry_dataclass_fields(self):
        fields = {f.name for f in dataclasses.fields(RepoMapEntry)}
        assert fields == {"file", "symbol_type", "name", "signature", "line"}

    def test_repo_map_entry_optional_defaults(self):
        entry = RepoMapEntry(file="foo.py", symbol_type="function", name="bar")
        assert entry.signature is None
        assert entry.line is None

    def test_repo_map_dataclass_fields(self):
        fields = {f.name for f in dataclasses.fields(RepoMap)}
        assert fields == {"entries", "repo_path", "generated_at", "truncated"}

    def test_to_prompt_string_empty(self):
        rm = RepoMap(entries=[], repo_path="/tmp/repo", generated_at="now")
        result = rm.to_prompt_string()
        assert result == ""

    def test_to_prompt_string_with_entries(self):
        entries = [
            RepoMapEntry(file="src/main.py", symbol_type="function", name="main", signature="() -> None", line=10),
            RepoMapEntry(file="src/main.py", symbol_type="class", name="App", line=20),
            RepoMapEntry(file="src/utils.py", symbol_type="function", name="helper", signature="(x: int) -> str"),
        ]
        rm = RepoMap(entries=entries, repo_path="/repo", generated_at="2026-01-01")
        result = rm.to_prompt_string()
        assert "src/main.py" in result
        assert "def main" in result
        assert "() -> None" in result
        assert "L10" in result
        assert "class App" in result
        assert "src/utils.py" in result

    def test_to_prompt_string_truncation(self):
        entries = [
            RepoMapEntry(file=f"src/file_{i}.py", symbol_type="function", name=f"func_{i}", signature="(x: int) -> int")
            for i in range(500)
        ]
        rm = RepoMap(entries=entries, repo_path="/repo", generated_at="now")
        result = rm.to_prompt_string(max_chars=200)
        assert "truncated" in result
        assert rm.truncated is True

    def test_generate_returns_repo_map(self):
        provider = _StubRepoMapProvider()
        result = provider.generate(Path("/tmp/repo"))
        assert isinstance(result, RepoMap)
