"""
Provider plugin system — all non-core integrations live here.

Each subdirectory defines one pluggable capability:
  coding_engine/   — CodingEngine (Claude, Gemini, OpenCode, OpenHands…)
  notification/    — NotificationProvider (Telegram, Slack, PagerDuty…)
  issue_tracker/   — IssueTracker (GitHub, Jira, Linear…)
  sandbox/         — SandboxProvider (ProxmoxAI, Docker, local…)
  dashboard/       — DashboardProvider (built-in HTTP, Grafana…)
  embeddings/      — EmbeddingProvider (bge-m3, OpenAI, local…)
  env/             — EnvProvider (dotenv, HashiCorp Vault, AWS Secrets Manager…)
  workspace/       — WorkspaceProvider (git-worktree, Docker volume, cloud VM…)
  repomap/         — RepoMapProvider (ctags, tree-sitter, file listing…)

Adding a new provider: implement the base interface in a new file, register in swe_team.yaml.
No changes to core code required.
"""

from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider

__all__ = ["GitWorktreeProvider"]
