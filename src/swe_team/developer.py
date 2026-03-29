"""
Developer agent for the Autonomous SWE Team.

Uses a keep/discard loop with git as the state machine to attempt fixes
and only keep changes that pass tests and complexity gates.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from src.swe_team.governance import check_fix_complexity
from src.swe_team.model_boundary import enforce_code_generation_boundary
from src.swe_team.models import SWETicket, TicketStatus
from src.swe_team.preflight import PreflightCheck
from src.swe_team.rbac_middleware import require_permission
from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.env.base import EnvProvider, EnvSpec
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider
from src.swe_team.providers.notification.base import NotificationProvider
from src.swe_team.proxy_model_policy import ProxyModelPolicyResolver
from src.swe_team.rate_limiter import ExponentialBackoff, RateLimitExhausted, RateLimitTracker

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


# Type alias for fallback agent adapters (duck-typed — must have .invoke())
_FallbackAgent = Any

# Model tier defaults — always read from env so the orchestrator can override at runtime.
# These are ONLY used when no ModelConfig is injected (e.g. unit tests).
# Never add model name literals anywhere else in this file.
_MODEL_T1 = os.environ.get("SWE_MODEL_T1", "opus")
_MODEL_T2 = os.environ.get("SWE_MODEL_T2", "sonnet")
_MODEL_T3 = os.environ.get("SWE_MODEL_T3", "haiku")

_DEFAULT_PROGRAM_PATH = Path("config/swe_team/programs/fix.md")
_BUILD_PROGRAM_PATH = Path("config/swe_team/programs/build.md")
_DEFAULT_MAX_ATTEMPTS = 3
_BUG_TIMEBOX_SECONDS = 25 * 60   # 25 min — leaves headroom for tests after Claude CLI (issue #294)
_FEATURE_TIMEBOX_SECONDS = 45 * 60
_TEST_OUTPUT_MAX_CHARS = 300
_ERROR_DISPLAY_MAX_CHARS = 120




class DeveloperAgent:
    """Attempts ticket fixes using Claude Code CLI and a keep/discard loop."""

    AGENT_NAME = "swe_developer"

    def __init__(
        self,
        *,
        repo_root: Union[Path, str] = ".",
        program_path: Union[Path, str] = _DEFAULT_PROGRAM_PATH,
        claude_path: str = "",
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        test_command: Optional[List[str]] = None,
        model_config: Optional[object] = None,
        rate_limit_config: Optional[object] = None,
        rate_limit_tracker: Optional[RateLimitTracker] = None,
        fallback_agents: Optional[List[_FallbackAgent]] = None,
        use_worktree: bool = False,
        env_provider: Optional[EnvProvider] = None,
        repos_map: Optional[dict] = None,
        notifier: Optional[NotificationProvider] = None,
        engine: Optional[CodingEngine] = None,
        rbac_engine: Optional[object] = None,
        preflight: Optional[PreflightCheck] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._program_path = Path(program_path)
        self._max_attempts = max_attempts
        self._program_cache: Optional[str] = None
        self._test_command = test_command or self._default_test_command()
        self._model_config = model_config
        self._fallback_agents: List[_FallbackAgent] = fallback_agents or []
        self._use_worktree = use_worktree
        self._active_worktree: Optional[Path] = None
        self._env_provider: EnvProvider = env_provider or DotenvEnvProvider()
        self._repos_map: dict = {
            name: Path(local_path)
            for name, local_path in (repos_map or {}).items()
        }
        self._notifier: Optional[NotificationProvider] = notifier
        # RBAC — optional, backward compatible
        self._rbac_engine = rbac_engine
        self._agent_name: str = self.AGENT_NAME
        # Injected preflight — optional, backward compatible
        self._preflight_override: Optional[PreflightCheck] = preflight
        # Session store — lazy-initialized in _fix_loop
        self._session_store: Optional[object] = None
        if engine is not None:
            self._engine: CodingEngine = engine
        else:
            from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
            self._engine = ClaudeCodeEngine()
        self._proxy_policy = ProxyModelPolicyResolver()

        # Rate limit backoff
        rl = rate_limit_config
        self._backoff = ExponentialBackoff(
            max_retries=getattr(rl, "max_retries_on_429", 3) if rl else 3,
            initial_delay=getattr(rl, "initial_backoff_seconds", 30) if rl else 30,
            max_delay=getattr(rl, "max_backoff_seconds", 300) if rl else 300,
            tracker=rate_limit_tracker,
        )

    @require_permission("code_generation")
    def attempt_fix(self, ticket: SWETicket) -> bool:
        """Run the keep/discard loop for *ticket*."""
        # Preflight: validate execution context before doing any work
        preflight_result = self._run_preflight()
        if not preflight_result.passed:
            logger.warning(
                "Preflight FAILED for ticket %s: %s",
                ticket.ticket_id,
                preflight_result.summary(),
            )
            ticket.metadata["preflight_failure"] = preflight_result.failures
            ticket.metadata["blocked_reason"] = preflight_result.summary()
            return False

        if not self._eligible(ticket):
            return False

        ticket.transition(TicketStatus.IN_DEVELOPMENT)
        ticket.metadata["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
        if self._use_worktree:
            try:
                branch = self._ensure_worktree(ticket)
            except RuntimeError as exc:
                logger.warning(
                    "Worktree setup failed for %s (%s) — falling back to branch",
                    ticket.ticket_id, exc,
                )
                branch = self._ensure_branch(ticket)
        else:
            branch = self._ensure_branch(ticket)
        ticket.metadata["branch"] = branch
        ticket.metadata.setdefault("pr_number", None)
        attempts = list(ticket.metadata.get("attempts", []))

        try:
            return self._fix_loop(ticket, branch, attempts)
        finally:
            if self._active_worktree:
                self._cleanup_worktree(ticket)

    def _fix_loop(
        self, ticket: SWETicket, branch: str, attempts: list,
    ) -> bool:
        """Inner keep/discard loop — separated so worktree cleanup runs in finally."""
        last_error = None  # Feed failures into next attempt (Ralph Wiggum loop)
        # Fix 3: Resume from prior attempt count so daemon restarts don't
        # reset the counter and create a runaway retry loop.
        start_attempt = len(attempts)
        if start_attempt >= self._max_attempts:
            logger.warning(
                "Ticket %s already exhausted %d/%d attempts — skipping",
                ticket.ticket_id, start_attempt, self._max_attempts,
            )
            ticket.metadata["attempts"] = attempts
            ticket.metadata["attempt_count"] = start_attempt
            ticket.transition(TicketStatus.FAILED)
            ticket.metadata["failed_reason"] = (
                f"All {start_attempt} dev attempt(s) already exhausted (resume check). "
                "Ticket requires human review or re-investigation."
            )
            self._escalate(ticket)
            return False

        # Session lifecycle: lazy-init SessionStore and register / resume a dev session.
        # Resume priority:
        #   1. ticket.metadata["dev_session_id"] — set on a previous attempt in this run
        #   2. SessionStore.find_resumable() — persisted suspended session from a prior cycle
        #   3. Fork from investigator's claude_session_id on the very first attempt
        _session_record = None
        _stored_resume_sid: Optional[str] = None
        try:
            from src.swe_team.session_store import SessionStore
            if self._session_store is None:
                self._session_store = SessionStore()
            # Only resume a *suspended* developer session — active sessions may be
            # orphaned from a previous crashed run or test, and should not be picked up.
            # Also only do this when ticket.metadata has no dev session yet (i.e. first start).
            if not ticket.metadata.get("dev_session_id"):
                all_sessions = self._session_store.get_by_ticket(ticket.ticket_id)
                suspended_dev = [
                    s for s in all_sessions
                    if s.agent_type == "developer" and s.status == "suspended"
                ]
                if suspended_dev:
                    _session_record = suspended_dev[0]
                    _stored_resume_sid = _session_record.session_id
                    self._session_store.update_status(_stored_resume_sid, "active")
                    logger.info(
                        "Developer: resuming suspended session %s for ticket %s",
                        _stored_resume_sid, ticket.ticket_id,
                    )
            if _session_record is None:
                # Check if investigator left a session we can fork from
                _inv_session_id = ticket.metadata.get("claude_session_id")
                _session_record = self._session_store.create(
                    ticket.ticket_id, "developer",
                    metadata={"fork_from": _inv_session_id} if _inv_session_id else None,
                )
                logger.info(
                    "Developer: created session %s for ticket %s%s",
                    _session_record.session_id, ticket.ticket_id,
                    f" (fork from investigator session {_inv_session_id})" if _inv_session_id else "",
                )
        except Exception:
            logger.debug(
                "Session store unavailable for developer, proceeding without session tracking",
                exc_info=True,
            )

        for attempt_num in range(start_attempt, self._max_attempts):
            attempt_start = time.monotonic()
            attempt_timestamp = datetime.now(timezone.utc).isoformat()
            base_sha = ""

            attempt_record = {
                "timestamp": attempt_timestamp,
                "duration_s": 0,
                "result": "fail",
                "attempt": attempt_num + 1,
            }

            try:
                base_sha = self._git(["git", "rev-parse", "HEAD"]).strip()
                prompt = self._build_prompt(ticket, last_error=last_error, attempt=attempt_num + 1)
                if prompt is None:
                    attempt_record["error"] = "Fix prompt template missing"
                    attempts.append(attempt_record)
                    break

                timebox = self._timebox_seconds(ticket)
                deadline = time.monotonic() + timebox

                model = self._select_model(ticket)
                enforce_code_generation_boundary(model, task="develop")
                # Session continuity: prefer ticket.metadata["dev_session_id"] for
                # daemon restarts, then a stored resumable session, then fork from
                # investigator on the very first attempt.
                existing_sid: Optional[str] = None
                fork_sid: Optional[str] = None
                if attempt_num > 0:
                    existing_sid = ticket.metadata.get("dev_session_id")
                elif _stored_resume_sid:
                    existing_sid = _stored_resume_sid
                elif _session_record and _session_record.metadata.get("fork_from"):
                    # Branch from investigator context on the very first attempt
                    fork_sid = _session_record.metadata["fork_from"]
                logger.info(
                    "Dev attempt %d for %s (model=%s%s%s)",
                    attempt_num + 1, ticket.ticket_id, model,
                    f", resume_sid={existing_sid}" if existing_sid else "",
                    f", fork_from={fork_sid}" if fork_sid else "",
                )
                self._backoff.execute(
                    lambda: self._run_claude(
                        prompt, timeout=self._remaining(deadline), model=model,
                        resume_session_id=existing_sid,
                        fork_session_id=fork_sid,
                    ),
                    context=model,
                )

                # Persist session_id for resumption on next attempt / daemon cycle
                er = getattr(self, "_last_engine_result", None)
                if er and getattr(er, "session_id", None):
                    real_sid = er.session_id
                    ticket.metadata["dev_session_id"] = real_sid
                    logger.info(
                        "Developer: session %s saved for ticket %s — resumable on retry",
                        real_sid, ticket.ticket_id,
                    )
                    # Update SessionStore with real Claude session UUID and touch
                    if _session_record and self._session_store:
                        try:
                            if real_sid != _session_record.session_id:
                                _session_record = self._session_store.update_session_id(
                                    _session_record.session_id, real_sid
                                )
                            else:
                                self._session_store.touch(real_sid)
                        except Exception:
                            logger.debug(
                                "Session store touch/update failed for %s", real_sid, exc_info=True
                            )

                # Record token usage (best-effort, never blocks development)
                try:
                    from src.swe_team.token_tracker import TokenTracker
                    tracker = TokenTracker()
                    er = getattr(self, "_last_engine_result", None)
                    input_tok = er.input_tokens if (er and er.input_tokens is not None) else _estimate_tokens(prompt)
                    output_tok = er.output_tokens if (er and er.output_tokens is not None) else max(1, _estimate_tokens(prompt) // 2)
                    cache_read = er.cache_read_tokens if (er and er.cache_read_tokens is not None) else 0
                    cache_creation = er.cache_creation_tokens if (er and er.cache_creation_tokens is not None) else 0
                    tracker.record(
                        model=model,
                        input_tokens=input_tok,
                        output_tokens=output_tok,
                        task="develop",
                        ticket_id=ticket.ticket_id,
                        cache_read_tokens=cache_read,
                        cache_creation_tokens=cache_creation,
                    )
                except Exception:
                    pass

                # Detect if Claude CLI committed code during its session.
                # Claude runs with auto-permissions and often commits directly.
                # Compare HEAD to base_sha to find those commits.
                current_sha = self._git(["git", "rev-parse", "HEAD"]).strip()
                claude_committed = base_sha and current_sha != base_sha

                if claude_committed:
                    # Claude made commits — get diff stats from base_sha to HEAD
                    commit_files = [
                        f for f in self._git(
                            ["git", "diff", "--name-only", base_sha, "HEAD"]
                        ).splitlines() if f.strip()
                    ]
                    commit_lines = 0
                    for line in self._git(
                        ["git", "diff", "--numstat", base_sha, "HEAD"]
                    ).splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 2 and parts[0].isdigit():
                            commit_lines += int(parts[0])
                        if len(parts) >= 2 and parts[1].isdigit():
                            commit_lines += int(parts[1])
                    logger.info(
                        "Claude committed %d file(s), %d line(s) for %s (%s..%s)",
                        len(commit_files), commit_lines,
                        ticket.ticket_id, base_sha[:8], current_sha[:8],
                    )

                tests_ok, test_error = self._run_tests(deadline, source_module=ticket.source_module)
                if not tests_ok:
                    attempt_record["error"] = test_error
                    last_error = test_error  # Ralph Wiggum: feed failure forward
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                # If Claude already committed, use commit-based diff stats.
                # Otherwise, stage uncommitted changes and check staged diff.
                if claude_committed:
                    files_changed = commit_files
                    lines_changed = commit_lines
                else:
                    # Stage everything first so new files are visible to diff
                    self._git(["git", "add", "-A"])
                    lines_changed, files_changed = self._diff_stats(staged=True)

                # Feature/build tickets get relaxed complexity gates
                if self._is_feature_ticket(ticket):
                    ok, reason = check_fix_complexity(
                        files_changed,
                        lines_changed,
                        max_files=20,
                        max_lines=1000,
                        allowed_modules=None,  # features span modules
                        allow_dependency_changes=True,
                    )
                else:
                    allowed_modules = set()
                    if ticket.source_module:
                        allowed_modules.add(ticket.source_module)
                    ok, reason = check_fix_complexity(
                        files_changed,
                        lines_changed,
                        allowed_modules=allowed_modules or None,
                    )
                if not ok:
                    attempt_record["error"] = reason
                    last_error = reason  # Ralph Wiggum: feed failure forward
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                if not files_changed:
                    attempt_record["error"] = "No changes produced"
                    last_error = "No changes produced"
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                if claude_committed:
                    # Claude already committed — no need to stage/commit again
                    logger.info(
                        "Accepting Claude's commits for %s (%d files)",
                        ticket.ticket_id, len(files_changed),
                    )
                else:
                    if self._git(["git", "diff", "--cached", "--name-only"]).strip() == "":
                        attempt_record["error"] = "No staged changes to commit"
                        self._reset_to(base_sha)
                        attempts.append(attempt_record)
                        continue
                    self._git(["git", "commit", "-m", f"swe-fix: {ticket.ticket_id}"])
                self._record_automation(ticket)

                # Push branch to origin BEFORE worktree cleanup can destroy it
                try:
                    self._git(["git", "push", "--force-with-lease", "origin", branch])
                    attempt_record["pushed"] = True
                    logger.info("Pushed branch %s to origin for %s", branch, ticket.ticket_id)
                except Exception as push_exc:
                    logger.warning("Failed to push branch %s: %s — commit preserved locally", branch, push_exc)
                    attempt_record["push_error"] = str(push_exc)

                attempt_record["result"] = "pass"
                attempt_record["branch"] = branch
                attempt_record["files_changed"] = len(files_changed)
                attempt_record["lines_changed"] = lines_changed
                attempts.append(attempt_record)

                ticket.transition(TicketStatus.IN_REVIEW)
                ticket.metadata["attempts"] = attempts
                # Mark session as completed on success
                if _session_record and self._session_store:
                    try:
                        self._session_store.update_status(
                            _session_record.session_id, "completed"
                        )
                    except Exception:
                        logger.debug("Failed to mark session completed", exc_info=True)
                return True

            except RateLimitExhausted as exc:
                # Try fallback agents before giving up
                if prompt and self._try_fallback_agents(prompt, ticket, timebox):
                    # Fallback succeeded — check tests and complexity
                    tests_ok, test_error = self._run_tests(deadline, source_module=ticket.source_module)
                    if tests_ok:
                        lines_changed, files_changed = self._diff_stats()
                        if files_changed:
                            self._git(["git", "add", "-A"])
                            if self._git(["git", "diff", "--cached", "--name-only"]).strip():
                                self._git(["git", "commit", "-m", f"swe-fix: {ticket.ticket_id} (fallback)"])
                                self._record_automation(ticket)

                                # Push fallback branch to origin before worktree cleanup
                                try:
                                    self._git(["git", "push", "--force-with-lease", "origin", branch])
                                    attempt_record["pushed"] = True
                                    logger.info("Pushed fallback branch %s to origin for %s", branch, ticket.ticket_id)
                                except Exception as push_exc:
                                    logger.warning("Failed to push fallback branch %s: %s", branch, push_exc)
                                    attempt_record["push_error"] = str(push_exc)

                                attempt_record["result"] = "pass"
                                attempt_record["fallback_agent"] = ticket.metadata.get("fallback_agent_used")
                                attempts.append(attempt_record)
                                ticket.transition(TicketStatus.IN_REVIEW)
                                ticket.metadata["attempts"] = attempts
                                return True
                    # Fallback fix didn't pass tests — reset and continue
                    if base_sha:
                        self._reset_to(base_sha)

                attempt_record["error"] = str(exc)
                ticket.metadata["rate_limited"] = True
                ticket.metadata["rate_limited_at"] = datetime.now(timezone.utc).isoformat()
                if base_sha:
                    self._reset_to(base_sha)
                attempts.append(attempt_record)
                self._send_rate_limit_alert(ticket, exc)
                # Suspend session so it can be resumed on next cycle
                if _session_record and self._session_store:
                    try:
                        self._session_store.update_status(
                            _session_record.session_id, "suspended"
                        )
                    except Exception:
                        logger.debug("Failed to suspend session on rate limit", exc_info=True)
                break  # No point retrying more attempts after rate limit exhaustion
            except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
                attempt_record["error"] = str(exc)
                if base_sha and self._repo_root.exists():
                    self._reset_to(base_sha)
                attempts.append(attempt_record)
                # Suspend session on timeout so it can be resumed on next cycle
                if isinstance(exc, subprocess.TimeoutExpired) and _session_record and self._session_store:
                    try:
                        self._session_store.update_status(
                            _session_record.session_id, "suspended"
                        )
                    except Exception:
                        logger.debug("Failed to suspend session on timeout", exc_info=True)
            finally:
                attempt_record["duration_s"] = round(
                    time.monotonic() - attempt_start, 2
                )

        ticket.metadata["attempts"] = attempts
        # Fix A: Persist attempt_count to metadata for restart-safe tracking
        ticket.metadata["attempt_count"] = len(attempts)

        # Fix E: If ALL attempts failed due to "No files changed" / "No staged changes",
        # mark as blocked so the backlog query can exclude it visibly.
        no_change_errors = {
            "No changes produced",
            "No staged changes to commit",
        }
        all_no_change = attempts and all(
            a.get("error", "") in no_change_errors for a in attempts
        )
        if all_no_change:
            ticket.transition(TicketStatus.BLOCKED)
            ticket.metadata["blocked_reason"] = (
                f"All {len(attempts)} dev attempt(s) produced no file changes. "
                "Likely the fix template or Claude output is non-functional for this ticket. "
                "Requires human review."
            )
            logger.warning(
                "Ticket %s → BLOCKED: all %d attempt(s) yielded no file changes",
                ticket.ticket_id, len(attempts),
            )
        else:
            # Fix A: Set status=failed after exhausting all attempts (persisted to Supabase)
            ticket.transition(TicketStatus.FAILED)
            ticket.metadata["failed_reason"] = (
                f"All {len(attempts)} dev attempt(s) failed. "
                "Ticket requires human review or re-investigation."
            )
            logger.warning(
                "Ticket %s → FAILED after %d attempt(s)",
                ticket.ticket_id, len(attempts),
            )

        self._escalate(ticket)
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_preflight(self):
        """Run pre-flight validation before attempting a fix."""
        if self._preflight_override is not None:
            return self._preflight_override.run()
        import os
        sandbox_paths = [Path(p) for p in (self._repos_map or {}).values()] if self._repos_map else []
        preflight = PreflightCheck(
            expected_git_name=os.environ.get("SWE_EXPECTED_GIT_NAME"),
            expected_git_email=os.environ.get("SWE_EXPECTED_GIT_EMAIL"),
            expected_github_account=os.environ.get("SWE_GITHUB_ACCOUNT") or None,
            expected_repo_root=self._repo_root if os.environ.get("SWE_GITHUB_REPO") else None,
            required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO"],
            sandbox_paths=sandbox_paths,
        )
        return preflight.run()

    def _try_fallback_agents(
        self, prompt: str, ticket: SWETicket, timeout: int
    ) -> bool:
        """Attempt to use fallback agents when the primary agent is rate-limited.

        Returns True if a fallback agent succeeded, False otherwise.
        """
        if not self._fallback_agents:
            return False

        for agent in self._fallback_agents:
            agent_name = getattr(agent, "_name", getattr(agent, "name", "unknown"))
            try:
                if hasattr(agent, "is_available") and not agent.is_available():
                    logger.info("Fallback agent %s not available, skipping", agent_name)
                    continue

                logger.info(
                    "Attempting fallback agent %s for ticket %s",
                    agent_name, ticket.ticket_id,
                )
                agent.invoke(prompt, timeout=timeout)
                ticket.metadata["fallback_agent_used"] = agent_name
                logger.info(
                    "Fallback agent %s succeeded for ticket %s",
                    agent_name, ticket.ticket_id,
                )
                return True
            except Exception:
                logger.warning(
                    "Fallback agent %s failed for ticket %s",
                    agent_name, ticket.ticket_id,
                    exc_info=True,
                )
                continue
        return False

    def _eligible(self, ticket: SWETicket) -> bool:
        if not ticket.investigation_report:
            return False
        return ticket.status in (
            TicketStatus.INVESTIGATION_COMPLETE,
            TicketStatus.IN_DEVELOPMENT,
        )

    def _ensure_branch(self, ticket: SWETicket) -> str:
        branch = f"swe-fix/ticket-{ticket.ticket_id}"
        # Pre-flight: abort if index has unresolved merge conflicts.
        # Conflict markers (UU, AA, DD, etc.) prevent checkout from succeeding.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=self._repo_root, timeout=10,
            env=self._env_provider.build_env(EnvSpec(role="developer")),
        )
        conflict_codes = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
        conflicts = [
            line for line in status.stdout.splitlines()
            if line[:2] in conflict_codes
        ]
        if conflicts:
            # Attempt automatic recovery — reset merge state without touching commits
            logger.warning(
                "developer: unresolved index conflicts (%d files) — attempting git reset --merge",
                len(conflicts),
            )
            subprocess.run(
                ["git", "reset", "--merge"],
                capture_output=True, cwd=self._repo_root, timeout=15,
                env=self._env_provider.build_env(EnvSpec(role="developer")),
            )
        self._git(["git", "checkout", "-B", branch])
        return branch

    def _ensure_worktree(self, ticket: SWETicket) -> str:
        """Create an isolated git worktree for this ticket.

        Each agent gets its own worktree -- a real branch in a separate
        directory.  This eliminates serialisation when multiple tickets
        target the same repo (inspired by ClawTeam).

        When the ticket has metadata["repo"] set (e.g. "owner/repo")
        and that repo is present in self._repos_map, the worktree is created
        inside the matching local clone rather than the default repo_root.
        This prevents cross-repo file path mismatches for multi-repo tickets.

        Returns the branch name.  Sets self._active_worktree so that
        all subsequent git/test/claude operations run inside the worktree.
        """
        branch = f"swe-fix/ticket-{ticket.ticket_id}"
        worktree_dir = Path(f"/tmp/swe-agent-{ticket.ticket_id}")

        # Resolve the correct repo root based on the ticket's source repository
        repo_name = ticket.metadata.get("repo")
        if repo_name and repo_name in self._repos_map:
            base_repo_root = Path(self._repos_map[repo_name])
            logger.info(
                "Using cross-repo root %s for ticket %s (repo=%s)",
                base_repo_root,
                ticket.ticket_id,
                repo_name,
            )
        else:
            base_repo_root = self._repo_root

        # Remove stale worktree from a previous crashed run
        if worktree_dir.exists():
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_dir)],
                    cwd=base_repo_root,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                shutil.rmtree(worktree_dir, ignore_errors=True)

        # Prune dead worktree references before adding a new one
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=base_repo_root,
            capture_output=True,
            text=True,
        )

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_dir), "-b", branch],
            cwd=base_repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git worktree add failed")

        # Redirect all subsequent operations to the worktree directory
        self._active_worktree = worktree_dir
        self._original_repo_root = self._repo_root
        self._repo_root = worktree_dir
        logger.info(
            "Worktree created for %s at %s (branch %s, base=%s)",
            ticket.ticket_id,
            worktree_dir,
            branch,
            base_repo_root,
        )
        return branch

    def _cleanup_worktree(self, ticket: SWETicket) -> None:
        """Merge the worktree branch back and remove the worktree."""
        worktree_dir = self._active_worktree
        if not worktree_dir:
            return

        branch = f"swe-fix/ticket-{ticket.ticket_id}"

        # Restore original repo root first so git commands target the main repo
        self._repo_root = getattr(self, "_original_repo_root", self._repo_root)
        self._active_worktree = None

        try:
            self._git(["git", "worktree", "remove", "--force", str(worktree_dir)])
        except RuntimeError:
            logger.warning("Failed to remove worktree %s — cleaning up manually", worktree_dir)
            shutil.rmtree(worktree_dir, ignore_errors=True)
            try:
                self._git(["git", "worktree", "prune"])
            except RuntimeError:
                pass

        logger.info("Worktree cleaned up for %s", ticket.ticket_id)

    def _is_feature_ticket(self, ticket: SWETicket) -> bool:
        """Detect enhancement/feature tickets that need the build template."""
        labels = [l.lower() for l in getattr(ticket, "labels", []) or []]
        title_lower = (ticket.title or "").lower()
        # Enhancement labels or phase tags indicate feature work
        if "enhancement" in labels or "feature" in labels or "foundation" in labels:
            return True
        if any(tag in title_lower for tag in ("[foundation]", "[feature]", "[integration]")):
            return True
        return False

    def _build_prompt(
        self, ticket: SWETicket, *, last_error: Optional[str] = None, attempt: int = 1
    ) -> Optional[str]:
        # Select build template for feature/enhancement tickets
        if self._is_feature_ticket(ticket) and _BUILD_PROGRAM_PATH.is_file():
            template = _BUILD_PROGRAM_PATH.read_text()
        else:
            template = self._load_program()
        if not template:
            return None
        try:
            prompt = template.format(
                ticket_id=ticket.ticket_id,
                title=ticket.title,
                severity=ticket.severity.value,
                source_module=ticket.source_module or "unknown",
                investigation_report=ticket.investigation_report or "No report provided.",
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid fix.md template: %s", exc)
            return None

        # Include orchestration plan in fix prompt if available
        if ticket.metadata.get("orchestration_plan"):
            prompt += f"\n\n## Orchestration Plan\n{ticket.metadata['orchestration_plan']}\n"

        # Ralph Wiggum loop: feed previous failure into the next attempt
        if last_error and attempt > 1:
            prompt += (
                f"\n\n## Previous Attempt Failed (attempt {attempt - 1})\n"
                f"The last fix attempt failed with this error:\n"
                f"```\n{last_error[:500]}\n```\n"
                f"This is attempt {attempt}/{self._max_attempts}. "
                f"Analyze the failure above and try a different approach.\n"
            )
        return prompt

    def _load_program(self) -> Optional[str]:
        if self._program_cache is not None:
            return self._program_cache
        if not self._program_path.is_file():
            logger.warning("Fix program not found: %s", self._program_path)
            return None
        self._program_cache = self._program_path.read_text(encoding="utf-8")
        return self._program_cache

    def _select_model(self, ticket: SWETicket) -> str:
        """Select model from config tiers: t1_heavy for CRITICAL or escalation, t2_standard otherwise."""
        from src.swe_team.models import TicketSeverity
        heavy = self._model_config.t1_heavy if self._model_config else _MODEL_T1
        standard = self._model_config.t2_standard if self._model_config else _MODEL_T2
        heavy = self._proxy_policy.resolve(heavy, tier="t1_heavy")
        standard = self._proxy_policy.resolve(standard, tier="t2_standard")
        if ticket.severity == TicketSeverity.CRITICAL:
            chosen = heavy
        else:
            attempts = ticket.metadata.get("attempts", [])
            failed = sum(1 for a in attempts if a.get("result") == "fail")
            chosen = heavy if failed >= 2 else standard

        # Security boundary: code generation must use a Claude-authorized model.
        try:
            enforce_code_generation_boundary(chosen, task="develop")
            return chosen
        except ValueError:
            logger.warning(
                "Developer model '%s' blocked by code-generation boundary; "
                "falling back to sonnet",
                chosen,
            )
            return "sonnet"

    def _run_claude(
        self, prompt: str, *, timeout: int, model: str = _MODEL_T2,
        resume_session_id: Optional[str] = None,
        fork_session_id: Optional[str] = None,
    ) -> None:
        # Developer agent needs full tool access (Edit, Write, Bash) to make fixes.
        # Uses the injected CodingEngine — always available (default: ClaudeCodeEngine).
        # Opus orchestrates sub-agents for complex bugs, Sonnet for routine fixes.
        _active_tools = getattr(self._engine, "allowed_tools", None)
        logger.debug(
            "_run_claude: engine path — active tools: %s",
            _active_tools or "(none — read-only mode)",
        )
        # Resume existing session on retry to preserve prior context
        if resume_session_id and hasattr(self._engine, "resume"):
            try:
                result = self._engine.resume(
                    resume_session_id,
                    prompt,
                    model=model,
                    cwd=str(self._repo_root),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.warning(
                    "Session resume failed (sid=%s), falling back to fresh run: %s",
                    resume_session_id, exc,
                )
                result = self._engine.run(
                    prompt,
                    model=model,
                    cwd=str(self._repo_root),
                    timeout=timeout,
                )
        elif fork_session_id and hasattr(self._engine, "fork"):
            # Fork from investigator session to inherit its research context
            try:
                result = self._engine.fork(
                    fork_session_id,
                    prompt,
                    model=model,
                    cwd=str(self._repo_root),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.warning(
                    "Session fork failed (fork_sid=%s), falling back to fresh run: %s",
                    fork_session_id, exc,
                )
                result = self._engine.run(
                    prompt,
                    model=model,
                    cwd=str(self._repo_root),
                    timeout=timeout,
                )
        else:
            result = self._engine.run(
                prompt,
                model=model,
                cwd=str(self._repo_root),
                timeout=timeout,
            )
        if not result.success:
            raise RuntimeError(result.stderr.strip() or "Claude CLI failed")
        self._last_engine_result = result

    def _run_tests(
        self, deadline: float, *, source_module: Optional[str] = None,
    ) -> Tuple[bool, str]:
        remaining = self._remaining(deadline)
        test_cmd = self._targeted_test_command(source_module)
        result = subprocess.run(
            test_cmd,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            timeout=remaining,
            env=self._env_provider.build_env(EnvSpec(role="test_runner")),
        )
        if result.returncode == 0:
            return True, ""
        # rc=5 means "no tests collected" — treat as success for new repos
        if result.returncode == 5:
            logger.info("No tests collected (rc=5) — treating as pass")
            return True, ""
        output = (result.stdout + "\n" + result.stderr).strip()
        return False, output[-_TEST_OUTPUT_MAX_CHARS:] if output else "Tests failed"

    def _diff_stats(self, *, staged: bool = False) -> Tuple[int, List[str]]:
        flag = ["--cached"] if staged else []
        files = [
            line for line in self._git(["git", "diff", "--name-only"] + flag).splitlines()
            if line
        ]
        lines_changed = 0
        for line in self._git(["git", "diff", "--numstat"] + flag).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed = parts[0], parts[1]
            if added.isdigit():
                lines_changed += int(added)
            if removed.isdigit():
                lines_changed += int(removed)
        return lines_changed, files

    def _reset_to(self, sha: str) -> None:
        self._git(["git", "reset", "--hard", sha])
        self._git(["git", "clean", "-fd"])

    def _record_automation(self, ticket: SWETicket) -> None:
        """Store deterministic automation steps for successful fixes."""
        try:
            from src.swe_team.distiller import TrajectoryDistiller
        except Exception as exc:
            logger.warning("Trajectory distiller unavailable: %s", exc)
            return

        fingerprint = ticket.metadata.get("fingerprint")
        if not fingerprint:
            return

        patch = self._git(["git", "show", "--format=", "HEAD"]).strip()
        if not patch:
            return

        distiller = TrajectoryDistiller(repo_root=self._repo_root)
        distiller.record_patch(ticket, patch)

    def _git(self, cmd: List[str]) -> str:
        result = subprocess.run(
            cmd,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            env=self._env_provider.build_env(EnvSpec(role="developer")),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git command failed")
        return result.stdout

    def _timebox_seconds(self, ticket: SWETicket) -> int:
        if self._is_feature(ticket):
            return _FEATURE_TIMEBOX_SECONDS
        return _BUG_TIMEBOX_SECONDS

    @staticmethod
    def _is_feature(ticket: SWETicket) -> bool:
        labels = {label.lower() for label in (ticket.labels or [])}
        if labels & {"feature", "enhancement", "foundation", "integration"}:
            return True
        title = ticket.title.lower()
        return any(tag in title for tag in (
            "feature", "enhancement", "[foundation]", "[feature]", "[integration]",
        ))

    def _remaining(self, deadline: float) -> int:
        remaining = max(1, int(deadline - time.monotonic()))
        return remaining

    def _default_test_command(self) -> List[str]:
        venv_python = self._repo_root / ".venv" / "bin" / "python3"
        python = str(venv_python) if venv_python.exists() else sys.executable
        # If the repo has a pytest.ini/pyproject.toml, let pytest discover tests
        # automatically rather than hardcoding a path that may not exist.
        test_dir = self._repo_root / "tests" / "unit"
        if test_dir.is_dir():
            return [python, "-m", "pytest", "tests/unit/", "-x", "-q"]
        return [python, "-m", "pytest", "-x", "-q"]

    def _targeted_test_command(self, source_module: Optional[str] = None) -> List[str]:
        """Build a test command scoped to the ticket's source module when possible.

        If *source_module* is set and a matching test file exists
        (``tests/unit/test_{module}.py``), run only that file instead of the
        full suite.  This cuts test time from 60-120s to < 10s (issue #294).
        Falls back to the full ``self._test_command`` when no match is found.
        """
        if not source_module:
            return self._test_command

        # Normalise: strip .py suffix, then extract last dotted component
        module_name = source_module.replace("/", ".").replace("\\", ".")
        if module_name.endswith(".py"):
            module_name = module_name[:-3]
        module_name = module_name.rsplit(".", 1)[-1]  # e.g. "swe_team.developer" → "developer"

        targeted_path = self._repo_root / "tests" / "unit" / f"test_{module_name}.py"
        if targeted_path.is_file():
            # Replace test path in command while preserving the python/pytest prefix and flags
            base = self._test_command[:3]  # e.g. [python, -m, pytest]
            return base + [str(targeted_path), "-x", "-q", "--tb=short", "--timeout=30"]

        logger.debug(
            "No targeted test file %s — falling back to full suite", targeted_path,
        )
        return self._test_command

    def _targeted_test_command(self, source_module: Optional[str] = None) -> List[str]:
        """Build a test command scoped to the ticket's source module when possible.

        If *source_module* is set and a matching test file exists
        (``tests/unit/test_{module}.py``), run only that file instead of the
        full suite.  This cuts test time from 60-120s to < 10s (issue #294).
        Falls back to the full ``self._test_command`` when no match is found.
        """
        if not source_module:
            return self._test_command

        # Normalise: strip .py suffix, then extract last dotted component
        module_name = source_module.replace("/", ".").replace("\\", ".")
        if module_name.endswith(".py"):
            module_name = module_name[:-3]
        module_name = module_name.rsplit(".", 1)[-1]  # e.g. "swe_team.developer" → "developer"

        targeted_path = self._repo_root / "tests" / "unit" / f"test_{module_name}.py"
        if targeted_path.is_file():
            # Replace test path in command while preserving the python/pytest prefix and flags
            base = self._test_command[:3]  # e.g. [python, -m, pytest]
            return base + [str(targeted_path), "-x", "-q", "--tb=short", "--timeout=30"]

        logger.debug(
            "No targeted test file %s — falling back to full suite", targeted_path,
        )
        return self._test_command

    def _send_rate_limit_alert(self, ticket: SWETicket, exc: Exception) -> None:
        """Send a Telegram alert when rate limits are exhausted."""
        message = (
            "<b>Rate Limit Exhausted (Developer)</b>\n\n"
            f"Ticket: <code>{ticket.ticket_id}</code>\n"
            f"Title: {ticket.title[:80]}\n"
            f"Error: {str(exc)[:200]}"
        )
        if self._notifier:
            self._notifier.send_alert(message, level="critical")
        else:
            from src.swe_team.telegram import send_message  # noqa: PLC0415
            try:
                send_message(message, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send rate limit alert for %s", ticket.ticket_id)

    def _escalate(self, ticket: SWETicket) -> None:
        attempts = ticket.metadata.get("attempts", [])
        summary_lines = [
            f"Ticket {ticket.ticket_id} failed after {len(attempts)} attempt(s).",
            f"Module: {ticket.source_module or 'unknown'}",
        ]
        for attempt in attempts[-3:]:
            summary_lines.append(
                f"- {attempt.get('timestamp')} result={attempt.get('result')} "
                f"error={attempt.get('error', '')[:_ERROR_DISPLAY_MAX_CHARS]}"
            )
        message = "<b>🧑‍💻 SWE Dev escalation</b>\n" + "\n".join(summary_lines)
        self._send_telegram(message)

    def _send_telegram(self, message: str) -> None:
        if self._notifier:
            self._notifier.send_alert(message, level="info")
        else:
            from src.swe_team.telegram import send_message  # noqa: PLC0415
            try:
                send_message(message, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send developer escalation")
