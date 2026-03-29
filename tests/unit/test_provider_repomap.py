"""Tests for repomap provider: CtagsRepoMapProvider + registry + protocol compliance."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.swe_team.providers.repomap.base import (
    RepoMap,
    RepoMapEntry,
    RepoMapProvider,
)
from src.swe_team.providers.repomap.ctags_provider import CtagsRepoMapProvider
from src.swe_team.providers.repomap import (
    create_repomap_provider,
    list_repomap_providers,
    register_repomap_provider,
)


# ---------------------------------------------------------------------------
# Data model tests — RepoMapEntry
# ---------------------------------------------------------------------------

class TestRepoMapEntry:
    """Tests for RepoMapEntry dataclass."""

    def test_basic_construction(self):
        entry = RepoMapEntry(file="src/foo.py", symbol_type="function", name="bar")
        assert entry.file == "src/foo.py"
        assert entry.symbol_type == "function"
        assert entry.name == "bar"
        assert entry.signature is None
        assert entry.line is None

    def test_full_construction(self):
        entry = RepoMapEntry(
            file="src/foo.py",
            symbol_type="method",
            name="run",
            signature="run(self, cmd: str) -> int",
            line=42,
        )
        assert entry.signature == "run(self, cmd: str) -> int"
        assert entry.line == 42


# ---------------------------------------------------------------------------
# Data model tests — RepoMap
# ---------------------------------------------------------------------------

class TestRepoMap:
    """Tests for RepoMap dataclass and to_prompt_string."""

    def test_empty_map(self):
        rm = RepoMap()
        assert rm.entries == []
        assert rm.to_prompt_string() == ""

    def test_prompt_string_basic(self):
        entries = [
            RepoMapEntry(file="src/a.py", symbol_type="class", name="Foo", line=10),
            RepoMapEntry(file="src/a.py", symbol_type="function", name="bar", signature="bar(x)", line=20),
        ]
        rm = RepoMap(entries=entries, repo_path="/repo")
        output = rm.to_prompt_string()

        assert "# Repo map: /repo" in output
        assert "## src/a.py" in output
        assert "class Foo" in output
        assert "def bar(x)" in output
        assert "(L10)" in output
        assert "(L20)" in output

    def test_prompt_string_method_without_signature(self):
        entries = [
            RepoMapEntry(file="mod.py", symbol_type="method", name="init"),
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string()
        assert "def init()" in output

    def test_prompt_string_variable(self):
        entries = [
            RepoMapEntry(file="mod.py", symbol_type="variable", name="MAX_SIZE", signature="int"),
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string()
        assert "variable MAX_SIZE: int" in output

    def test_prompt_string_variable_no_sig(self):
        entries = [
            RepoMapEntry(file="mod.py", symbol_type="variable", name="FOO"),
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string()
        assert "variable FOO" in output

    def test_prompt_string_groups_by_file_sorted(self):
        entries = [
            RepoMapEntry(file="z.py", symbol_type="class", name="Z"),
            RepoMapEntry(file="a.py", symbol_type="class", name="A"),
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string()
        # a.py should appear before z.py
        pos_a = output.index("## a.py")
        pos_z = output.index("## z.py")
        assert pos_a < pos_z

    def test_prompt_string_truncation(self):
        """Large maps should truncate and set the flag."""
        entries = [
            RepoMapEntry(file=f"file_{i}.py", symbol_type="class", name=f"BigClassName_{i}")
            for i in range(500)
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string(max_chars=200)
        assert rm.truncated is True
        assert "truncated" in output

    def test_prompt_string_no_truncation_when_small(self):
        entries = [RepoMapEntry(file="a.py", symbol_type="class", name="A")]
        rm = RepoMap(entries=entries, repo_path="/r")
        rm.to_prompt_string(max_chars=100_000)
        assert rm.truncated is False

    def test_prompt_string_signature_starts_with_name(self):
        """When signature already starts with the name, don't duplicate."""
        entries = [
            RepoMapEntry(file="a.py", symbol_type="function", name="foo", signature="foo(x, y)"),
        ]
        rm = RepoMap(entries=entries, repo_path="/r")
        output = rm.to_prompt_string()
        # Should be "def foo(x, y)" not "def foofoo(x, y)"
        assert "def foo(x, y)" in output
        assert "foofoo" not in output


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    """CtagsRepoMapProvider must satisfy the RepoMapProvider protocol."""

    def test_is_instance_of_protocol(self):
        provider = CtagsRepoMapProvider()
        assert isinstance(provider, RepoMapProvider)

    def test_has_required_methods(self):
        provider = CtagsRepoMapProvider()
        assert callable(getattr(provider, "generate", None))
        assert callable(getattr(provider, "is_available", None))
        assert callable(getattr(provider, "health_check", None))


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider — construction
# ---------------------------------------------------------------------------

class TestCtagsConstruction:
    """Constructor and config handling."""

    def test_default_config(self):
        provider = CtagsRepoMapProvider()
        assert provider._config == {}

    def test_custom_config(self):
        cfg = {"language": "Python", "extra_flag": True}
        provider = CtagsRepoMapProvider(config=cfg)
        assert provider._config == cfg

    def test_none_config_becomes_empty_dict(self):
        provider = CtagsRepoMapProvider(config=None)
        assert provider._config == {}


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider — is_available / health_check
# ---------------------------------------------------------------------------

class TestCtagsAvailability:
    """Mocked tests for is_available and health_check."""

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is True
        mock_run.assert_called_once()

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_false_bad_rc(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is False

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_false_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("ctags not found")
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is False

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_is_available_false_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ctags", timeout=10)
        provider = CtagsRepoMapProvider()
        assert provider.is_available() is False

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_health_check_delegates_to_is_available(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        provider = CtagsRepoMapProvider()
        assert provider.health_check() is True
        # health_check calls is_available internally
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider — generate (with ctags available)
# ---------------------------------------------------------------------------

class TestCtagsGenerate:
    """generate() with mocked subprocess calls."""

    def _make_ctags_output(self, tags: list[dict]) -> str:
        """Build ctags JSON output from a list of tag dicts."""
        lines = []
        for tag in tags:
            obj = {"_type": "tag", **tag}
            lines.append(json.dumps(obj))
        return "\n".join(lines)

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_basic(self, mock_run):
        """Parses ctags JSON output into RepoMapEntry objects."""
        tags = [
            {"name": "MyClass", "kind": "class", "path": "/repo/src/foo.py", "line": 10},
            {"name": "my_func", "kind": "function", "path": "/repo/src/foo.py", "line": 25, "signature": "(x, y)"},
        ]
        ctags_output = self._make_ctags_output(tags)

        # First call: is_available check
        avail_result = MagicMock(returncode=0)
        # Second call: actual ctags run
        gen_result = MagicMock(returncode=0, stdout=ctags_output, stderr="")

        mock_run.side_effect = [avail_result, gen_result]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/repo"))

        assert len(repo_map.entries) == 2
        assert repo_map.entries[0].name == "MyClass"
        assert repo_map.entries[0].symbol_type == "class"
        assert repo_map.entries[0].file == "src/foo.py"
        assert repo_map.entries[1].name == "my_func"
        assert repo_map.entries[1].symbol_type == "function"
        assert repo_map.entries[1].signature == "my_func(x, y)"
        assert repo_map.repo_path == "/repo"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_skips_non_tag_entries(self, mock_run):
        """Non-tag JSON lines are skipped."""
        raw = json.dumps({"_type": "ptag", "name": "JSON_OUTPUT_VERSION"}) + "\n"
        raw += json.dumps({"_type": "tag", "name": "Real", "kind": "class", "path": "/r/a.py", "line": 1})

        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=raw, stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert len(repo_map.entries) == 1
        assert repo_map.entries[0].name == "Real"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_skips_private_variables(self, mock_run):
        """Private variables (starting with _) are filtered out."""
        tags = [
            {"name": "_private", "kind": "variable", "path": "/r/a.py", "line": 1},
            {"name": "PUBLIC", "kind": "variable", "path": "/r/a.py", "line": 2},
        ]
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=self._make_ctags_output(tags), stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert len(repo_map.entries) == 1
        assert repo_map.entries[0].name == "PUBLIC"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_skips_ignored_paths(self, mock_run):
        """Files matching ignore patterns are excluded."""
        tags = [
            {"name": "A", "kind": "class", "path": "/r/__pycache__/foo.py", "line": 1},
            {"name": "B", "kind": "class", "path": "/r/src/good.py", "line": 1},
        ]
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=self._make_ctags_output(tags), stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert len(repo_map.entries) == 1
        assert repo_map.entries[0].name == "B"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_custom_ignore(self, mock_run):
        """Custom ignore patterns are appended to defaults."""
        tags = [
            {"name": "A", "kind": "class", "path": "/r/vendor/lib.py", "line": 1},
            {"name": "B", "kind": "class", "path": "/r/src/ok.py", "line": 1},
        ]
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=self._make_ctags_output(tags), stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"), ignore=["vendor"])
        assert len(repo_map.entries) == 1
        assert repo_map.entries[0].name == "B"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_handles_invalid_json_lines(self, mock_run):
        """Invalid JSON lines in ctags output are silently skipped."""
        raw = "not json at all\n"
        raw += json.dumps({"_type": "tag", "name": "Valid", "kind": "class", "path": "/r/a.py", "line": 1})

        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=raw, stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert len(repo_map.entries) == 1
        assert repo_map.entries[0].name == "Valid"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_empty_output(self, mock_run):
        """Empty ctags output returns empty entries list."""
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert repo_map.entries == []
        assert repo_map.repo_path == "/r"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_has_generated_at(self, mock_run):
        """generated_at timestamp is set."""
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/r"))
        assert repo_map.generated_at != ""


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider — fallback file listing
# ---------------------------------------------------------------------------

class TestCtagsFallback:
    """When ctags is unavailable, fallback to file listing."""

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_when_ctags_unavailable(self, mock_run, tmp_path):
        """Falls back to file listing when is_available returns False."""
        mock_run.return_value = MagicMock(returncode=1)  # ctags not available

        # Create test files
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "helper.py").write_text("x = 1")
        (tmp_path / "readme.txt").write_text("not python")

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(tmp_path)

        files = [e.file for e in repo_map.entries]
        assert "main.py" in files
        assert "sub/helper.py" in files
        # Non-.py files are excluded
        assert not any("readme" in f for f in files)

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_ignores_pycache(self, mock_run, tmp_path):
        """Fallback file listing prunes __pycache__ directories."""
        mock_run.return_value = MagicMock(returncode=1)

        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.cpython-311.pyc").write_text("")
        (tmp_path / "good.py").write_text("")

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(tmp_path)

        files = [e.file for e in repo_map.entries]
        assert len(files) == 1
        assert files[0] == "good.py"

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_on_ctags_timeout(self, mock_run, tmp_path):
        """Falls back to file listing on ctags subprocess timeout."""
        import subprocess as sp

        # First call (is_available): succeeds
        avail = MagicMock(returncode=0)
        # Second call (generate): timeout
        mock_run.side_effect = [avail, sp.TimeoutExpired(cmd="ctags", timeout=60)]

        (tmp_path / "a.py").write_text("")
        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(tmp_path)

        assert len(repo_map.entries) >= 1

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_on_ctags_error_returncode(self, mock_run, tmp_path):
        """Falls back when ctags exits with non-zero return code."""
        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=2, stderr="error: something broke")
        mock_run.side_effect = [avail, gen]

        (tmp_path / "a.py").write_text("")
        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(tmp_path)

        assert len(repo_map.entries) >= 1

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_fallback_includes_file_size(self, mock_run, tmp_path):
        """Fallback entries include file size in the name."""
        mock_run.return_value = MagicMock(returncode=1)

        content = "x = 42\n"
        (tmp_path / "mod.py").write_text(content)

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(tmp_path)

        assert len(repo_map.entries) == 1
        assert "mod.py" in repo_map.entries[0].name
        assert "bytes" in repo_map.entries[0].name

    def test_fallback_file_listing_empty_dir(self):
        """Fallback on empty directory returns empty entries."""
        provider = CtagsRepoMapProvider()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            rm = provider._fallback_file_listing(Path(td), provider._IGNORE_DEFAULTS)
            assert rm.entries == []

    def test_fallback_file_listing_nonexistent_dir(self):
        """Fallback on nonexistent directory returns empty entries gracefully."""
        provider = CtagsRepoMapProvider()
        rm = provider._fallback_file_listing(Path("/nonexistent/dir"), provider._IGNORE_DEFAULTS)
        assert rm.entries == []


# ---------------------------------------------------------------------------
# CtagsRepoMapProvider — internal helpers
# ---------------------------------------------------------------------------

class TestCtagsHelpers:
    """Tests for static helper methods."""

    def test_should_ignore_direct_match(self):
        assert CtagsRepoMapProvider._should_ignore("foo.pyc", ["*.pyc"]) is True

    def test_should_ignore_directory_component(self):
        assert CtagsRepoMapProvider._should_ignore("a/__pycache__/b.py", ["__pycache__"]) is True

    def test_should_ignore_no_match(self):
        assert CtagsRepoMapProvider._should_ignore("src/main.py", ["*.pyc", "__pycache__"]) is False

    def test_should_ignore_nested_node_modules(self):
        assert CtagsRepoMapProvider._should_ignore("deps/node_modules/pkg/index.py", ["node_modules"]) is True

    def test_map_kind_known_types(self):
        assert CtagsRepoMapProvider._map_kind("class") == "class"
        assert CtagsRepoMapProvider._map_kind("function") == "function"
        assert CtagsRepoMapProvider._map_kind("method") == "method"
        assert CtagsRepoMapProvider._map_kind("member") == "method"
        assert CtagsRepoMapProvider._map_kind("variable") == "variable"
        assert CtagsRepoMapProvider._map_kind("import") == "variable"
        assert CtagsRepoMapProvider._map_kind("namespace") == "variable"
        assert CtagsRepoMapProvider._map_kind("module") == "variable"

    def test_map_kind_unknown_defaults_to_variable(self):
        assert CtagsRepoMapProvider._map_kind("something_else") == "variable"
        assert CtagsRepoMapProvider._map_kind("") == "variable"


# ---------------------------------------------------------------------------
# Registry — create_repomap_provider / list / register
# ---------------------------------------------------------------------------

class TestRepoMapRegistry:
    """Tests for the provider registry in __init__.py."""

    def test_ctags_registered_by_default(self):
        providers = list_repomap_providers()
        assert "ctags" in providers

    def test_create_ctags_provider(self):
        provider = create_repomap_provider("ctags")
        assert isinstance(provider, CtagsRepoMapProvider)
        assert isinstance(provider, RepoMapProvider)

    def test_create_ctags_with_config(self):
        cfg = {"language": "Go"}
        provider = create_repomap_provider("ctags", config=cfg)
        assert isinstance(provider, CtagsRepoMapProvider)
        assert provider._config == cfg

    def test_create_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown repo map provider 'nonexistent'"):
            create_repomap_provider("nonexistent")

    def test_create_unknown_lists_available(self):
        with pytest.raises(ValueError, match="ctags"):
            create_repomap_provider("nope")

    def test_register_custom_provider(self):
        """Can register and resolve a custom provider."""

        class DummyRepoMapProvider:
            def __init__(self, config=None):
                self.cfg = config

            def generate(self, repo_path, max_tokens=2000, ignore=None):
                return RepoMap()

            def is_available(self):
                return True

            def health_check(self):
                return True

        register_repomap_provider("dummy_test", lambda cfg: DummyRepoMapProvider(cfg))
        try:
            provider = create_repomap_provider("dummy_test", {"k": "v"})
            assert isinstance(provider, RepoMapProvider)
            assert provider.cfg == {"k": "v"}
        finally:
            # Clean up registry to avoid polluting other tests
            from src.swe_team.providers.repomap import _REGISTRY
            _REGISTRY.pop("dummy_test", None)

    def test_list_providers_returns_sorted(self):
        providers = list_repomap_providers()
        assert providers == sorted(providers)

    def test_create_with_none_config(self):
        """None config is converted to empty dict by the factory."""
        provider = create_repomap_provider("ctags", config=None)
        assert provider._config == {}


# ---------------------------------------------------------------------------
# Integration-style: generate + to_prompt_string
# ---------------------------------------------------------------------------

class TestGenerateToPrompt:
    """End-to-end flow: generate a map and format it as a prompt string."""

    @patch("src.swe_team.providers.repomap.ctags_provider.subprocess.run")
    def test_generate_and_format(self, mock_run):
        tags = [
            {"name": "Config", "kind": "class", "path": "/repo/config.py", "line": 5},
            {"name": "load", "kind": "function", "path": "/repo/config.py", "line": 20, "signature": "(path: str)"},
            {"name": "Server", "kind": "class", "path": "/repo/server.py", "line": 1},
        ]
        raw = "\n".join(json.dumps({"_type": "tag", **t}) for t in tags)

        avail = MagicMock(returncode=0)
        gen = MagicMock(returncode=0, stdout=raw, stderr="")
        mock_run.side_effect = [avail, gen]

        provider = CtagsRepoMapProvider()
        repo_map = provider.generate(Path("/repo"))
        prompt = repo_map.to_prompt_string()

        assert "# Repo map: /repo" in prompt
        assert "## config.py" in prompt
        assert "## server.py" in prompt
        assert "class Config" in prompt
        assert "class Server" in prompt
        assert "def load(path: str)" in prompt
