"""
RepoMapProvider interface — pluggable structural repo mapping for LLM context.

Implement this to swap between ctags, tree-sitter, simple file listing,
or any other code structure extraction tool without touching core agent code.

Generates a compact map of file paths, symbols, types, and signatures
suitable for injection into LLM prompts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class RepoMapEntry:
    """A single symbol extracted from a source file."""

    file: str
    symbol_type: str                    # "class", "function", "method", "variable"
    name: str
    signature: str | None = None
    line: int | None = None


@dataclass
class RepoMap:
    """Complete structural map of a repository."""

    entries: list[RepoMapEntry] = field(default_factory=list)
    repo_path: str = ""
    generated_at: str = ""
    truncated: bool = False

    def to_prompt_string(self, max_chars: int = 8000) -> str:
        """Format as a readable string for injection into LLM prompts.

        Groups entries by file and formats each symbol with its type,
        name, and optional signature. Truncates if the output exceeds
        max_chars, setting the truncated flag.
        """
        if not self.entries:
            return ""

        # Group entries by file
        by_file: dict[str, list[RepoMapEntry]] = {}
        for entry in self.entries:
            by_file.setdefault(entry.file, []).append(entry)

        lines: list[str] = [f"# Repo map: {self.repo_path}"]
        was_truncated = False

        for filepath in sorted(by_file):
            file_header = f"\n## {filepath}"
            lines.append(file_header)

            for entry in by_file[filepath]:
                if entry.symbol_type == "class":
                    symbol_line = f"  class {entry.name}"
                elif entry.symbol_type in ("function", "method"):
                    sig = entry.signature or "()"
                    # If signature already starts with the name (ctags style), use as-is
                    # Otherwise prepend name (e.g. signature="() -> None" → "name() -> None")
                    if sig.startswith(entry.name):
                        symbol_line = f"    def {sig}"
                    else:
                        symbol_line = f"    def {entry.name}{sig}"
                else:
                    symbol_line = f"  {entry.symbol_type} {entry.name}"
                    if entry.signature:
                        symbol_line += f": {entry.signature}"
                if entry.line is not None:
                    symbol_line += f"  (L{entry.line})"
                lines.append(symbol_line)

            # Check length so far
            current = "\n".join(lines)
            if len(current) > max_chars:
                was_truncated = True
                # Remove the last file block that pushed us over
                # Walk back to find the last file header
                while lines and not lines[-1].startswith("## "):
                    lines.pop()
                if lines and lines[-1].startswith("## "):
                    lines.pop()
                lines.append(f"\n... truncated ({len(self.entries)} symbols total)")
                break

        self.truncated = was_truncated or self.truncated
        return "\n".join(lines)


@runtime_checkable
class RepoMapProvider(Protocol):
    """
    Interface all repo map providers must implement.

    Providers are registered in config/swe_team.yaml under providers.repomap.
    The active provider is loaded by name — no core code changes required
    when switching backends.
    """

    def generate(
        self,
        repo_path: Path,
        max_tokens: int = 2000,
        ignore: list[str] | None = None,
    ) -> RepoMap:
        """Generate a structural map of the repository at repo_path."""
        ...

    def is_available(self) -> bool:
        """Return True if the underlying tool (e.g. ctags) is installed and working."""
        ...

    def health_check(self) -> bool:
        """Return True if the provider is reachable and properly configured."""
        ...
