"""Proxy model policy resolver.

Loads ``config/swe_team/proxy_model_policy.yaml`` and resolves model aliases to
full backend model IDs while avoiding known-failing models when possible.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = "config/swe_team/proxy_model_policy.yaml"
_HEALTHY = {"healthy", "healthy_with_limits"}


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ProxyModelPolicyResolver:
    """Resolve configured model names against proxy policy.

    Resolver is auto-enabled when ``ANTHROPIC_BASE_URL`` is set. This can be
    overridden via ``SWE_PROXY_POLICY_ENABLED=true|false``.
    """

    def __init__(self, policy_path: Optional[str] = None) -> None:
        env_enabled = os.environ.get("SWE_PROXY_POLICY_ENABLED")
        if env_enabled is None:
            self._enabled = bool(os.environ.get("ANTHROPIC_BASE_URL", "").strip())
        else:
            self._enabled = _as_bool(env_enabled)

        self._path = Path(policy_path or os.environ.get("SWE_PROXY_MODEL_POLICY", _DEFAULT_POLICY_PATH))
        self._policy: Dict[str, Any] = {}
        self._index: Dict[str, Dict[str, Any]] = {}
        if self._enabled:
            self._load()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _load(self) -> None:
        if not self._path.is_file():
            logger.warning("proxy policy enabled but file not found: %s", self._path)
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                self._policy = yaml.safe_load(fh) or {}
            for row in self._policy.get("models", []):
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("short_name", "")).strip().lower()
                mid = str(row.get("model_id", "")).strip().lower()
                if sid:
                    self._index[sid] = row
                if mid:
                    self._index[mid] = row
        except Exception as exc:
            logger.warning("failed loading proxy model policy %s: %s", self._path, exc)

    def resolve(self, model: str, *, tier: str = "t2_standard") -> str:
        """Resolve *model* to backend model ID and avoid failing entries.

        When a mapped model is marked failing, returns the best healthy fallback
        for the requested tier.
        """
        if not self._enabled or not model:
            return model

        key = model.strip().lower()
        row = self._index.get(key)
        if not row:
            return model

        status = str(row.get("status", "")).strip().lower()
        model_id = self._normalize_model_id(str(row.get("model_id", model)).strip() or model, row)
        if status in _HEALTHY:
            return model_id

        fallback = self._fallback_for_tier(tier)
        if fallback:
            logger.warning(
                "proxy policy: model '%s' is '%s' — falling back to '%s' for %s",
                model,
                status or "unknown",
                fallback,
                tier,
            )
            return fallback
        return model_id

    def _normalize_model_id(self, model_id: str, row: Dict[str, Any]) -> str:
        """Return model ID normalized to provider/model form when needed.

        LiteLLM-backed providers often require explicit provider prefixes
        (e.g. ``openai/qwen3-coder:480b-cloud``). If ``model_id`` already
        contains a provider prefix, it is returned unchanged.
        """
        if not model_id or "/" in model_id:
            return model_id

        provider = (
            str(row.get("provider", "")).strip()
            or str(self._policy.get("defaults", {}).get("model_provider", "")).strip()
            or str(os.environ.get("CLAUDE_PROXY_MODEL_PROVIDER", "")).strip()
        )
        if provider:
            return f"{provider}/{model_id}"
        return model_id

    def _fallback_for_tier(self, tier: str) -> Optional[str]:
        rows = [r for r in self._policy.get("models", []) if isinstance(r, dict)]
        healthy = [r for r in rows if str(r.get("status", "")).strip().lower() in _HEALTHY]
        if not healthy:
            return None

        # Prefer coder model for standard/fast, reasoning fallback for heavy tier.
        if tier == "t1_heavy":
            for r in healthy:
                hint = str(r.get("role_hint", "")).lower()
                if "reason" in hint:
                    model_id = str(r.get("model_id", "")).strip()
                    return self._normalize_model_id(model_id, r) or None

        for r in healthy:
            hint = str(r.get("role_hint", "")).lower()
            if "coder" in hint:
                model_id = str(r.get("model_id", "")).strip()
                return self._normalize_model_id(model_id, r) or None

        model_id = str(healthy[0].get("model_id", "")).strip()
        return self._normalize_model_id(model_id, healthy[0]) or None
