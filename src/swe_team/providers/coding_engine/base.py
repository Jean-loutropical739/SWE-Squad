"""
CodingEngine interface — pluggable coding agent backend.

Implement this to swap Claude Code CLI for any other agent
(Gemini CLI, OpenCode, OpenHands, GitHub Copilot, etc.)
without changing any core investigator or developer logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class EngineResult:
    stdout: str
    stderr: str
    returncode: int
    cost_usd: Optional[float] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    num_turns: Optional[int] = None
    duration_api_ms: Optional[int] = None
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """True when the engine exited cleanly (returncode == 0)."""
        return self.returncode == 0


@runtime_checkable
class CodingEngine(Protocol):
    """
    Interface all coding agent backends must implement.

    Registered in swe_team.yaml under providers.coding_engine.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'claude', 'gemini', 'opencode')."""
        ...

    def run(self, prompt: str, *, model: str, timeout: int, cwd: Optional[str] = None) -> EngineResult:
        """
        Run a prompt through the coding agent. Returns structured result.
        Raise RuntimeError on unrecoverable failure.
        Raise subprocess.TimeoutExpired on timeout.
        """
        ...

    def health_check(self) -> bool:
        """Return True if the engine binary/API is reachable."""
        ...
