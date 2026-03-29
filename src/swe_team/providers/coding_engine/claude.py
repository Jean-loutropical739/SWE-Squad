"""Claude Code CLI engine -- concrete CodingEngine implementation.

Wraps /usr/bin/claude (or a custom binary path) as a subprocess.
Registered in swe_team.yaml under providers.coding_engine.provider: claude.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

logger = logging.getLogger(__name__)


class ClaudeCodeEngine:
    """Runs Claude Code CLI as a subprocess.

    Implements the :class:`CodingEngine` protocol so it can be injected into
    :class:`InvestigatorAgent` and :class:`DeveloperAgent` without either
    module importing subprocess or knowing which binary is used.
    """

    def __init__(
        self,
        *,
        default_model: str = "sonnet",
        default_timeout: int = 300,
        binary: str | None = None,
        allowed_tools: str | None = None,
        dangerously_skip_permissions: bool = False,
        permission_mode: str = "auto",
    ) -> None:
        self._default_model = default_model
        self._default_timeout = default_timeout
        self._binary = binary or shutil.which("claude") or "/usr/bin/claude"
        self._allowed_tools = allowed_tools
        # permission_mode overrides the legacy dangerously_skip_permissions flag.
        # Values: "strict" (never skip), "auto" (skip only when allowed_tools set),
        #         "bypass" (always skip — requires explicit opt-in).
        if permission_mode not in ("strict", "auto", "bypass"):
            raise ValueError(
                f"permission_mode must be 'strict', 'auto', or 'bypass'; got {permission_mode!r}"
            )
        self._permission_mode = permission_mode
        # Legacy flag: only honoured when permission_mode is not set explicitly.
        # Kept for backwards compatibility but defaults to False (was True).
        self._skip_permissions = dangerously_skip_permissions

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "claude"

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Optional[str] = None,
        env: dict | None = None,
        session_id: str | None = None,
        raise_on_timeout: bool = True,
    ) -> EngineResult:
        """Execute Claude Code CLI with *prompt* and return an :class:`EngineResult`.

        Parameters match the :class:`CodingEngine` protocol. Extra keyword
        arguments (``env``) are forwarded to :func:`subprocess.run` so the
        caller can inject a scoped environment.

        When *session_id* is provided, ``--session <session_id>`` is appended
        to the CLI command so Claude Code can track conversation context.

        When *raise_on_timeout* is True (default), ``subprocess.TimeoutExpired``
        is re-raised so callers such as ``investigator.py``'s ``_record_timeout``
        adaptive path are reachable. Set to False to get an ``EngineResult``
        with ``returncode=-1`` instead (legacy behaviour).
        """
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout

        cmd = self._build_cmd(effective_model, session_id=session_id)

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=env,
            )
            data = self._parse_json_output(result.stdout)
            engine_result = self._build_engine_result(
                data, result.stdout, result.stderr, result.returncode, effective_model,
            )
            if result.returncode != 0:
                engine_result.metadata["error_type"] = self._classify_error(
                    result.stderr, result.returncode
                )
            return engine_result
        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI timed out after %ds", effective_timeout)
            if raise_on_timeout:
                raise
            return EngineResult(
                stdout="",
                stderr=f"Timeout after {effective_timeout}s",
                returncode=-1,
                model=effective_model,
                metadata={"error_type": "timeout"},
            )
        except FileNotFoundError:
            logger.error("Claude CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=effective_model,
            )

    def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Optional[str] = None,
        env: dict | None = None,
        raise_on_timeout: bool = True,
    ) -> EngineResult:
        """Resume an existing Claude Code session.

        Adds ``--session <session_id> --resume`` to the CLI command so Claude
        Code continues from the previous conversation state.

        When *raise_on_timeout* is True (default), ``subprocess.TimeoutExpired``
        is re-raised. Set to False to get an ``EngineResult`` with
        ``returncode=-1`` instead (legacy behaviour).
        """
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout

        cmd = self._build_cmd(effective_model, session_id=session_id, resume=True)

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=env,
            )
            data = self._parse_json_output(result.stdout)
            engine_result = self._build_engine_result(
                data, result.stdout, result.stderr, result.returncode, effective_model,
            )
            if result.returncode != 0:
                engine_result.metadata["error_type"] = self._classify_error(
                    result.stderr, result.returncode
                )
            return engine_result
        except subprocess.TimeoutExpired:
            logger.warning("Claude CLI resume timed out after %ds", effective_timeout)
            if raise_on_timeout:
                raise
            return EngineResult(
                stdout="",
                stderr=f"Timeout after {effective_timeout}s",
                returncode=-1,
                model=effective_model,
                metadata={"error_type": "timeout"},
            )
        except FileNotFoundError:
            logger.error("Claude CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=effective_model,
            )

    # -- Command builder -------------------------------------------------------

    def _should_skip_permissions(self) -> bool:
        """Return True if ``--dangerously-skip-permissions`` should be added.

        Decision matrix:
        - "bypass": always skip (explicit opt-in required).
        - "auto": skip only when ``_allowed_tools`` is set (developer sessions
          that have been granted an explicit tool list act as their own RBAC
          gate).
        - "strict": never skip.
        - Legacy fallback (permission_mode not set): honour ``_skip_permissions``
          bool, but that flag now defaults to False.
        """
        if self._permission_mode == "bypass":
            return True
        if self._permission_mode == "auto":
            return bool(self._allowed_tools)
        # "strict"
        return False

    def _build_cmd(
        self,
        model: str,
        *,
        session_id: str | None = None,
        resume: bool = False,
    ) -> list[str]:
        """Build the CLI argument list for a claude invocation."""
        cmd: list[str] = [self._binary]
        if self._should_skip_permissions():
            logger.warning(
                "SECURITY: --dangerously-skip-permissions is active for this session "
                "(permission_mode=%r, allowed_tools=%r)",
                self._permission_mode,
                self._allowed_tools,
            )
            cmd.append("--dangerously-skip-permissions")
        cmd.extend(["--model", model])
        if self._allowed_tools:
            cmd.extend(["--allowedTools", self._allowed_tools])
        # Claude CLI requires session IDs to be valid UUIDs
        _valid_sid = False
        if session_id:
            try:
                uuid.UUID(session_id)
                _valid_sid = True
            except (ValueError, AttributeError):
                logger.debug("Skipping non-UUID session_id: %s", session_id)
        if _valid_sid and not resume:
            # New session with a specific UUID
            cmd.extend(["--session-id", session_id])
        elif resume and _valid_sid:
            # Resume an existing session by UUID
            cmd.extend(["--resume", session_id])
        elif resume:
            # Resume most recent session
            cmd.append("--resume")
        cmd.append("--print")
        cmd.extend(["--output-format", "json"])
        return cmd

    def health_check(self) -> bool:
        """Return True if the claude binary is found on PATH or at the configured location."""
        return self.is_available()

    # -- JSON output parsing ---------------------------------------------------

    @staticmethod
    def _parse_json_output(raw_stdout: str) -> dict:
        """Parse JSON output from ``--output-format json --print``.

        The CLI emits a single JSON object on stdout with the structure::

            {"type":"result","subtype":"success","cost_usd":0.065,
             "duration_ms":2380,"duration_api_ms":2300,"num_turns":1,
             "result":"Hello!","session_id":"...","usage":{...}}

        Returns a dict with extracted fields.  On parse failure, returns
        ``{"result": raw_stdout}`` so the caller still gets the text.
        """
        text = raw_stdout.strip()
        if not text:
            return {"result": ""}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # Not valid JSON — fall back to treating stdout as plain text
            return {"result": text}
        if not isinstance(data, dict):
            return {"result": text}
        return data

    @staticmethod
    def _build_engine_result(
        data: dict,
        raw_stdout: str,
        stderr: str,
        returncode: int,
        model: str,
    ) -> EngineResult:
        """Build an :class:`EngineResult` from parsed JSON output dict."""
        # Extract the text result — nested under "result" key in JSON mode
        text_result = data.get("result", raw_stdout)
        if not isinstance(text_result, str):
            text_result = str(text_result) if text_result is not None else ""

        # Extract cost — prefer JSON field, fall back to stderr regex
        cost = data.get("cost_usd")
        if cost is None:
            cost = ClaudeCodeEngine._parse_cost_legacy(stderr) or ClaudeCodeEngine._parse_cost_legacy(raw_stdout)

        # Extract usage from "usage" dict (input/output tokens)
        usage = data.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens")
        cache_creation_tokens = usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens")

        return EngineResult(
            stdout=text_result,
            stderr=stderr,
            returncode=returncode,
            cost_usd=float(cost) if cost is not None else None,
            model=model,
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
            cache_read_tokens=int(cache_read_tokens) if cache_read_tokens is not None else None,
            cache_creation_tokens=int(cache_creation_tokens) if cache_creation_tokens is not None else None,
            num_turns=int(data["num_turns"]) if data.get("num_turns") is not None else None,
            duration_api_ms=int(data["duration_api_ms"]) if data.get("duration_api_ms") is not None else None,
            session_id=data.get("session_id"),
        )

    # -- Legacy cost parsing (fallback) ----------------------------------------

    @staticmethod
    def _parse_cost_legacy(text: str) -> Optional[float]:
        """Extract a dollar cost from Claude CLI verbose/stderr output.

        Legacy fallback for when JSON parsing does not yield a cost_usd field.
        """
        for line in text.splitlines():
            if "cost" not in line.lower():
                continue
            match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", line)
            if match:
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    return None
        return None

    @staticmethod
    def _parse_cost(text: str) -> Optional[float]:
        """Extract cost — kept for backwards compatibility."""
        return ClaudeCodeEngine._parse_cost_legacy(text)

    # -- Error classification --------------------------------------------------

    @staticmethod
    def _classify_error(stderr: str, returncode: int) -> str:
        """Classify a non-zero exit into a named error category.

        Used to populate ``EngineResult.metadata["error_type"]`` so callers
        and the rate-limiter can react to specific failure modes without
        parsing raw stderr text.

        Returns one of: ``"rate_limit"``, ``"overloaded"``, ``"server_error"``,
        ``"auth_error"``, ``"model_not_found"``, ``"timeout"``, ``"unknown"``.
        """
        msg = stderr.lower()
        if returncode == -1 or "timeout" in msg:
            return "timeout"
        if "rate limit" in msg or "rate_limit" in msg or "429" in msg:
            return "rate_limit"
        if "overloaded" in msg or "529" in msg or "capacity" in msg:
            return "overloaded"
        if "500" in msg or "internal server error" in msg or "server error" in msg:
            return "server_error"
        if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
            return "auth_error"
        if "model not found" in msg or "404" in msg:
            return "model_not_found"
        return "unknown"

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the claude binary exists."""
        return bool(shutil.which("claude") or Path(self._binary).exists())

    def model(self) -> str:
        """Return the default model name."""
        return self._default_model

    @property
    def allowed_tools(self) -> Optional[str]:
        """Return the comma-separated tool list granted to this engine, or None."""
        return self._allowed_tools
