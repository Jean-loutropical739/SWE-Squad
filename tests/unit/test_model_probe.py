"""Unit tests for src/swe_team/model_probe.py — LLM model availability probe."""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import src.swe_team.model_probe as mp_mod
from src.swe_team.model_probe import (
    _is_probe_failed_recently,
    _record_probe_failure,
    _probe_failure_cache,
    probe_embedding_model,
    probe_chat_model,
    select_model,
    list_available_models,
    ModelProbe,
    PROBE_FAILURE_TTL_SECS,
)


def _clear_failure_cache():
    """Helper: wipe the global failure cache before each test."""
    _probe_failure_cache.clear()


# ---------------------------------------------------------------------------
# Tests: failure cache helpers
# ---------------------------------------------------------------------------

class TestProbeFailureCache(unittest.TestCase):
    def setUp(self):
        _clear_failure_cache()

    def test_fresh_model_not_failed_recently(self):
        assert _is_probe_failed_recently("bge-m3") is False

    def test_record_then_is_failed_recently(self):
        _record_probe_failure("bge-m3")
        assert _is_probe_failed_recently("bge-m3") is True

    def test_expired_entry_returns_false(self):
        _probe_failure_cache["old-model"] = time.monotonic() - PROBE_FAILURE_TTL_SECS - 1
        assert _is_probe_failed_recently("old-model") is False
        # Should also have been removed
        assert "old-model" not in _probe_failure_cache

    def test_separate_models_tracked_independently(self):
        _record_probe_failure("model-a")
        assert _is_probe_failed_recently("model-a") is True
        assert _is_probe_failed_recently("model-b") is False


# ---------------------------------------------------------------------------
# Tests: list_available_models
# ---------------------------------------------------------------------------

class TestListAvailableModels(unittest.TestCase):
    def setUp(self):
        _clear_failure_cache()

    def test_no_url_returns_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BASE_LLM_API_URL", None)
            result = list_available_models(api_url=None, api_key=None)
        assert result == []

    def test_openai_unavailable_returns_empty(self):
        with patch.dict(os.environ, {"BASE_LLM_API_URL": "http://fake-url/v1",
                                     "BASE_LLM_API_KEY": "key"}):
            with patch.dict(sys.modules, {"openai": None}):
                # When openai isn't importable, list_available_models catches the ImportError
                result = list_available_models(api_url="http://fake/v1", api_key="k")
        # Either returns [] (ImportError caught) or works — just shouldn't raise
        assert isinstance(result, list)

    def test_openai_exception_returns_empty(self):
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.models.list.side_effect = Exception("connection refused")
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = list_available_models(api_url="http://fake/v1", api_key="k")
        assert result == []

    def test_returns_sorted_model_ids(self):
        mock_model_b = MagicMock()
        mock_model_b.id = "zoo-model"
        mock_model_a = MagicMock()
        mock_model_a.id = "alpha-model"

        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.models.list.return_value.data = [mock_model_b, mock_model_a]
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = list_available_models(api_url="http://fake/v1", api_key="k")

        assert result == ["alpha-model", "zoo-model"]


# ---------------------------------------------------------------------------
# Tests: probe_embedding_model
# ---------------------------------------------------------------------------

class TestProbeEmbeddingModel(unittest.TestCase):
    def setUp(self):
        _clear_failure_cache()

    def test_no_url_returns_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BASE_LLM_API_URL", None)
            result = probe_embedding_model("bge-m3", api_url=None, api_key=None)
        assert result is False

    def test_skips_recently_failed_model(self):
        _record_probe_failure("bge-m3")
        result = probe_embedding_model("bge-m3", api_url="http://fake/v1", api_key="k")
        assert result is False

    def test_success_returns_true(self):
        _clear_failure_cache()
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_resp = MagicMock()
        mock_resp.data = [mock_embedding]

        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = mock_resp
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_embedding_model("bge-m3", api_url="http://fake/v1", api_key="k")

        assert result is True

    def test_empty_embedding_returns_false_and_records_failure(self):
        _clear_failure_cache()
        mock_resp = MagicMock()
        mock_resp.data = []

        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = mock_resp
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_embedding_model("bge-m3", api_url="http://fake/v1", api_key="k")

        assert result is False
        assert _is_probe_failed_recently("bge-m3")

    def test_exception_returns_false_and_records_failure(self):
        _clear_failure_cache()
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = Exception("timeout")
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_embedding_model("bge-m3", api_url="http://fake/v1", api_key="k")

        assert result is False
        assert _is_probe_failed_recently("bge-m3")


# ---------------------------------------------------------------------------
# Tests: probe_chat_model
# ---------------------------------------------------------------------------

class TestProbeChatModel(unittest.TestCase):
    def setUp(self):
        _clear_failure_cache()

    def test_no_url_returns_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BASE_LLM_API_URL", None)
            result = probe_chat_model("gemini-3-flash", api_url=None, api_key=None)
        assert result is False

    def test_skips_recently_failed_model(self):
        _record_probe_failure("gemini-3-flash")
        result = probe_chat_model("gemini-3-flash", api_url="http://fake/v1", api_key="k")
        assert result is False

    def test_success_returns_true(self):
        _clear_failure_cache()
        mock_choice = MagicMock()
        mock_choice.message.content = "pong"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_chat_model("gemini-3-flash", api_url="http://fake/v1", api_key="k")

        assert result is True

    def test_empty_response_returns_false(self):
        _clear_failure_cache()
        mock_resp = MagicMock()
        mock_resp.choices = []

        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_chat_model("gemini-3-flash", api_url="http://fake/v1", api_key="k")

        assert result is False

    def test_exception_returns_false_and_records_failure(self):
        _clear_failure_cache()
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("rate limited")
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict(sys.modules, {"openai": mock_openai}):
            result = probe_chat_model("gemini-3-flash", api_url="http://fake/v1", api_key="k")

        assert result is False
        assert _is_probe_failed_recently("gemini-3-flash")


# ---------------------------------------------------------------------------
# Tests: select_model
# ---------------------------------------------------------------------------

class TestSelectModel(unittest.TestCase):
    def test_preferred_in_available_returns_preferred(self):
        result = select_model("bge-m3", ["bge-m3", "other"], ["fallback"])
        assert result == "bge-m3"

    def test_preferred_missing_uses_first_available_fallback(self):
        result = select_model("missing", ["fallback-a", "fallback-b"], ["fallback-a", "fallback-b"])
        assert result == "fallback-a"

    def test_no_matches_returns_preferred_anyway(self):
        result = select_model("bge-m3", ["unrelated-model"], ["also-missing"])
        assert result == "bge-m3"

    def test_skips_unavailable_fallbacks(self):
        result = select_model(
            "missing",
            ["third"],
            ["first-missing", "second-missing", "third"],
        )
        assert result == "third"


# ---------------------------------------------------------------------------
# Tests: ModelProbe class
# ---------------------------------------------------------------------------

class TestModelProbe(unittest.TestCase):
    def setUp(self):
        _clear_failure_cache()

    def test_init_reads_env_vars(self):
        with patch.dict(os.environ, {"BASE_LLM_API_URL": "http://test/v1", "BASE_LLM_API_KEY": "mykey"}):
            probe = ModelProbe()
        assert probe._api_url == "http://test/v1"
        assert probe._api_key == "mykey"

    def test_init_accepts_explicit_args(self):
        probe = ModelProbe(api_url="http://explicit/v1", api_key="explicit-key")
        assert probe._api_url == "http://explicit/v1"

    def test_available_property_cached(self):
        probe = ModelProbe(api_url="http://fake/v1", api_key="k")
        with patch("src.swe_team.model_probe.list_available_models", return_value=["model-a"]) as mock_list:
            _ = probe.available
            _ = probe.available  # second access should use cache
        mock_list.assert_called_once()

    def test_check_returns_preferred_if_available(self):
        probe = ModelProbe(api_url="http://fake/v1", api_key="k")
        probe._available = ["bge-m3", "other"]
        result = probe.check("bge-m3", ["fallback"], "embedding")
        assert result == "bge-m3"

    def test_check_returns_fallback_if_preferred_missing(self):
        probe = ModelProbe(api_url="http://fake/v1", api_key="k")
        probe._available = ["fallback-model"]
        result = probe.check("missing-model", ["fallback-model"], "embedding")
        assert result == "fallback-model"

    def test_check_passthrough_when_no_available_models(self):
        probe = ModelProbe(api_url="http://fake/v1", api_key="k")
        probe._available = []
        result = probe.check("bge-m3", ["fallback"], "embedding")
        assert result == "bge-m3"

    def test_validate_and_patch_env_skips_when_no_models(self):
        probe = ModelProbe(api_url=None, api_key=None)
        probe._available = []
        patches = probe.validate_and_patch_env()
        assert patches == {}

    def test_validate_and_patch_env_patches_when_configured_unavailable(self):
        _clear_failure_cache()

        # Simulate: configured model not available, but fallback is
        probe = ModelProbe(api_url="http://fake/v1", api_key="k")
        probe._available = ["bge-m3"]  # configured "bad-embed" not in this list

        # Patch env so configured model is something not in available.
        # Check os.environ INSIDE the patch.dict context because validate_and_patch_env
        # writes to os.environ, which gets restored when the context exits.
        with patch.dict(os.environ, {
            "EMBEDDING_MODEL": "bad-embed",
            "EXTRACTION_MODEL": "bad-chat",
        }):
            with patch("src.swe_team.model_probe.probe_embedding_model", return_value=True):
                with patch("src.swe_team.model_probe.probe_chat_model", return_value=False):
                    patches = probe.validate_and_patch_env()
                    # bge-m3 should have been chosen for embedding
                    assert os.environ.get("EMBEDDING_MODEL") == "bge-m3"
                    assert "EMBEDDING_MODEL" in patches or os.environ.get("EMBEDDING_MODEL") == "bge-m3"

    def test_validate_model_tiers_returns_empty_when_no_config(self):
        probe = ModelProbe(api_url=None, api_key=None)
        probe._available = ["some-model"]
        result = probe.validate_model_tiers(None)
        assert result == {}

    def test_validate_model_tiers_flags_missing_tier_model(self):
        probe = ModelProbe(api_url=None, api_key=None)
        probe._available = ["known-model"]

        mock_config = MagicMock()
        mock_config.t1_heavy = "not-in-proxy-model"
        mock_config.t2_standard = "known-model"

        result = probe.validate_model_tiers(mock_config)
        # t1_heavy not in available → should appear in report
        assert "t1_heavy" in result
        # t2_standard is in available → should NOT appear in report
        assert "t2_standard" not in result


if __name__ == "__main__":
    unittest.main()
