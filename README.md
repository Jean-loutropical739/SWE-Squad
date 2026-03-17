<p align="center">
  <img src="assets/swe_squad_banner.png" alt="SWE Squad Banner" width="100%">
</p>

<h1 align="center">🛡️ SWE Squad</h1>

<p align="center">
  <em>Autonomous Software Engineering Agents That Fix Bugs While You Sleep</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white&style=for-the-badge" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Claude_Code-CLI-7C3AED?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJ3aGl0ZSI+PHBhdGggZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6Ii8+PC9zdmc+&style=for-the-badge" alt="Claude Code">
  <img src="https://img.shields.io/badge/A2A-Protocol-F97316?style=for-the-badge" alt="A2A Protocol">
  <img src="https://img.shields.io/badge/license-MIT-22C55E?style=for-the-badge" alt="MIT License">
</p>

<p align="center">
  <a href="https://github.com/ArtemisAI/SWE-Squad/stargazers">
    <img src="https://img.shields.io/github/stars/ArtemisAI/SWE-Squad?style=social" alt="GitHub Stars">
  </a>
  &nbsp;&nbsp;
  <a href="https://github.com/ArtemisAI/SWE-Squad/network/members">
    <img src="https://img.shields.io/github/forks/ArtemisAI/SWE-Squad?style=social" alt="GitHub Forks">
  </a>
</p>

<p align="center">
  Self-healing, self-diagnosing development agents that monitor production systems,<br>
  detect errors, investigate root causes, implement fixes, and learn from successes.
</p>

<p align="center">
  Built on <a href="https://docs.anthropic.com/en/docs/claude-code">Claude Code</a> &bull;
  <a href="https://github.com/google/A2A">A2A Protocol</a> &bull;
  <a href="https://supabase.com">Supabase</a>
</p>

<br>

---

## 🔍 Overview

SWE Squad is a team of AI agents that autonomously monitors your production systems, detects issues, and fixes them — with human oversight at every critical decision point.

Unlike single-agent coding tools, SWE Squad operates as a **coordinated team** where each agent has a specialized role, cost-optimized model routing keeps bills low, and a stability gate prevents regressions.

### ✨ Key Features

<table>
  <tr>
    <td align="center" width="33%">
      <h4>🧠 Semantic Memory</h4>
      <p>pgvector embeddings with mem0-style fact extraction — agents remember how past bugs were solved</p>
    </td>
    <td align="center" width="33%">
      <h4>🔗 A2A Protocol</h4>
      <p>Agent-to-Agent communication across machines on LAN, Tailscale, or any network</p>
    </td>
    <td align="center" width="33%">
      <h4>🔌 Multi-Provider</h4>
      <p>Claude Code, Gemini CLI, OpenCode — any coding agent can plug into the squad</p>
    </td>
  </tr>
  <tr>
    <td align="center" width="33%">
      <h4>🔎 Automated Detection</h4>
      <p>Scans logs for errors with fingerprint-based deduplication</p>
    </td>
    <td align="center" width="33%">
      <h4>💰 Smart Model Routing</h4>
      <p>T1/T2/T3 cost tiers — Haiku for cheap tasks, Sonnet for fixes, Opus only when critical</p>
    </td>
    <td align="center" width="33%">
      <h4>🔄 Keep/Discard Loop</h4>
      <p>Every fix lives on a git branch; tests fail → auto-revert</p>
    </td>
  </tr>
  <tr>
    <td align="center" width="33%">
      <h4>🚦 Ralph Wiggum Gate</h4>
      <p>Stability-first governance: bugs must be fixed before features ship</p>
    </td>
    <td align="center" width="33%">
      <h4>🔁 Closed-Loop Validation</h4>
      <p>Post-fix regression monitoring — catches fixes that don't hold in production</p>
    </td>
    <td align="center" width="33%">
      <h4>👥 Multi-Team Support</h4>
      <p>Multiple squads share a Supabase backend without overlap</p>
    </td>
  </tr>
</table>

<br>

---

## 🏗️ Architecture

```
                    ┌─────────────────────────────────────┐
                    │         SWE Squad Runner             │
                    │     (cron / daemon / one-shot)       │
                    └──────────────┬──────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   ┌──────────────┐      ┌────────────────┐      ┌────────────────┐
   │   Monitor    │      │  GitHub Fetch   │      │  Remote Logs   │
   │  Agent       │      │  (assigned      │      │  (SSH/rsync)   │
   │  (log scan)  │      │   issues)       │      │                │
   └──────┬───────┘      └───────┬────────┘      └────────┬───────┘
          │                      │                         │
          └──────────────────────┼─────────────────────────┘
                                 ▼
                    ┌────────────────────────┐
                    │     Triage Agent       │
                    │  (severity + routing)  │
                    └───────────┬────────────┘
                                │
               ┌────────────────┼────────────────┐
               ▼                ▼                ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │  Trajectory  │  │ Investigator │  │   Notifier   │
     │  Distiller   │  │    Agent     │  │  (Telegram)  │
     │ (cache hit?) │  │ (Claude CLI) │  │              │
     └──────┬───────┘  └──────┬───────┘  └──────────────┘
            │                 │
            │    ┌────────────┘
            ▼    ▼
     ┌──────────────────┐
     │  Developer Agent  │
     │  (keep/discard    │
     │   fix loop)       │
     └────────┬─────────┘
              │
              ▼
     ┌──────────────────┐      ┌──────────────────┐
     │  Ralph Wiggum    │      │  Creative Agent   │
     │  Stability Gate  │─────▶│  (proposals,      │
     │  (bugs first)    │      │   only if stable) │
     └────────┬─────────┘      └──────────────────┘
              │
              ▼
     ┌──────────────────┐
     │  A2A Dispatch    │
     │  (event bus)     │
     └──────────────────┘
```

---

## 🧠 Semantic Memory — How Agents Learn

SWE Squad doesn't start from scratch every time. A **pgvector-backed semantic memory** system gives every investigator access to resolved tickets that are similar to the current problem — before it begins analysis.

```
New ticket arrives
       │
       ▼
┌─────────────────────────────┐
│  embed_ticket()             │  ← bge-m3 multilingual (1024 dims)
│  via OpenAI-compatible API  │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│  extract_memory_facts()     │  ← mem0-style: distil raw ticket into
│  via cheap T3 model         │    compact structured facts before
│  (gemini-3-flash)           │    embedding (root cause, fix, tags)
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│  match_similar_tickets()    │  ← pgvector cosine similarity search
│  Supabase RPC               │    against resolved/closed tickets
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│  Inject into agent prompt   │  ← "## Semantic Memory — Similar
│  as structured context      │     Resolved Tickets"
└─────────────────────────────┘
```

**Why this matters:**
- **Avoids redundant investigation** — the agent sees how a similar error was resolved last time
- **Reduces Opus escalation** — Sonnet can solve problems that previously required Opus, because it has the prior fix as context
- **Cross-repo patterns** — a fix from Repo A informs an investigation in Repo B (same Supabase backend)
- **Zero-cost for known issues** — if the similarity is high enough, the trajectory distiller replays the cached fix without any LLM call

The memory system is **best-effort and non-fatal** — if the embedding API is down, the database is unreachable, or the model returns garbage, the agent proceeds without memory context. No single failure breaks the pipeline.

### Memory Configuration

```yaml
# config/swe_team.yaml
memory:
  embedding_model: "bge-m3"       # Multilingual (EN/FR/ES), 1024 dimensions
  embedding_dimensions: 1024
  top_k: 5                        # Return top 5 similar tickets
  similarity_floor: 0.75          # Minimum cosine similarity threshold
  store_on_investigation_complete: true
```

```bash
# .env — embedding API (any OpenAI-compatible endpoint)
EMBEDDING_MODEL=bge-m3
EMBEDDING_API_URL=https://your-llm-proxy.example.com/v1
EMBEDDING_API_KEY=your_key
```

---

## 🔗 A2A Protocol — Agent-to-Agent Communication

SWE Squad implements Google's [A2A (Agent-to-Agent) protocol](https://github.com/google/A2A) for cross-agent coordination across machines. This means squads running on **different VMs on the same network** (LAN, Tailscale, WireGuard, etc.) can discover each other, share tickets, and coordinate work.

```
┌─────────────────────────┐     A2A / JSON-RPC 2.0     ┌─────────────────────────┐
│  VM-1 (webdev-1)        │◄────────────────────────────►│  VM-2 (worker-1)        │
│                         │     over LAN / Tailscale     │                         │
│  SWE Squad (team: alpha)│                              │  SWE Squad (team: beta) │
│  └─ Claude Code CLI     │                              │  └─ Gemini CLI          │
│  └─ Monitor Agent       │                              │  └─ Investigator Agent  │
│  └─ Developer Agent     │                              │  └─ Tester Agent        │
│                         │                              │                         │
│  A2A Hub (:18790)       │                              │  A2A Client             │
└─────────────────────────┘                              └─────────────────────────┘
              │                                                     │
              └──────────────────┬──────────────────────────────────┘
                                 │
                          ┌──────▼──────┐
                          │  Supabase   │  ← shared ticket store
                          │  (pgvector) │    both teams read/write
                          └─────────────┘
```

### How it works

1. Each SWE Squad exposes an **Agent Card** (A2A standard) describing its skills:
   - `monitor_scan` — scan logs and emit tickets
   - `triage_ticket` — classify and assign
   - `investigate_ticket` — run Claude Code diagnosis
   - `check_stability` — Ralph Wiggum gate check

2. Other agents on the network can **discover** the squad via the A2A hub and **invoke** these skills via JSON-RPC messages.

3. Events (issue detected, triage complete, investigation complete, fix deployed) are **dispatched** to the A2A hub for any listening agent to consume.

### Use cases

- **Distributed monitoring**: Squad on VM-1 monitors web services, squad on VM-2 monitors databases — both write to the same Supabase ticket store
- **Cross-team escalation**: Alpha squad can't fix a database issue → dispatches an A2A event → Beta squad (database specialist) picks it up
- **Heterogeneous agents**: Claude Code, Gemini CLI, and OpenCode agents coexist — each runs on its preferred VM but coordinates through A2A

---

## 🔌 Multi-Provider Agent Support

SWE Squad is designed to work with **any AI coding agent**, not just Claude Code. The architecture separates the **orchestration layer** (ticket management, routing, governance) from the **execution layer** (the actual coding agent).

| Provider | How it integrates | Status |
|----------|-------------------|--------|
| **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** | Native — invoked via CLI subprocess with prompt programs | ✅ Production |
| **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** | Via A2A protocol — register as an agent, receive investigation/fix tasks | 🔌 Ready to integrate |
| **[OpenCode](https://github.com/opencode-ai/opencode)** | Via A2A protocol — same as Gemini CLI | 🔌 Ready to integrate |
| **[Aider](https://github.com/paul-gauthier/aider)** | CLI subprocess — similar to Claude Code integration | 🔌 Ready to integrate |
| **[Goose](https://github.com/block/goose)** | CLI subprocess or A2A protocol | 🔌 Ready to integrate |
| **Custom agents** | Implement the `AgentAdapter` interface or connect via A2A | 🔌 Extensible |

### Adding a new agent provider

1. **Via A2A (recommended)**: Your agent registers with the A2A hub, publishes its Agent Card, and responds to `handle_message()` calls with investigation reports or fix results.

2. **Via CLI subprocess**: Add a new execution backend in the investigator/developer that calls your agent's CLI instead of `claude`. The prompt programs (`config/swe_team/programs/*.md`) are provider-agnostic markdown — they work with any agent that accepts text input.

---

## 🔄 How the Fix Loop Works

```
┌─────────────────────────────────────────────────────────────────┐
│  Attempt 1 (Sonnet)                                             │
│  try fix → run tests → FAIL → capture error message             │
├─────────────────────────────────────────────────────────────────┤
│  Attempt 2 (Sonnet)                                             │
│  try fix WITH previous error context → run tests → FAIL         │
├─────────────────────────────────────────────────────────────────┤
│  Attempt 3 (Opus)                                               │
│  escalate → orchestrate sub-agents → run tests → PASS → KEEP   │
├─────────────────────────────────────────────────────────────────┤
│  All 3 fail? → HITL escalation via Telegram                    │
└─────────────────────────────────────────────────────────────────┘
```

Each attempt runs on a **git branch**. Tests pass → commit. Tests fail → `git reset --hard` (auto-revert). No broken code ever reaches main.

---

## 🧠 Model Routing

SWE Squad routes to the cheapest model that can handle the job:

| Scenario | Model | Cost | Timeout |
|----------|-------|------|---------|
| Issue scanning, docs | **Haiku** | 💲 | 30s |
| Routine HIGH bugs | **Sonnet** | 💲💲 | 2 min |
| CRITICAL bugs | **Opus** | 💲💲💲 | 10 min |
| After 2 failed Sonnet attempts | **Opus** | 💲💲💲 | 10 min |
| Deterministic replay (cached) | **None** | 🆓 Free | < 1s |

---

## 🚀 Quick Start

### 1. Clone & install

```bash
git clone https://github.com/ArtemisAI/SWE-Squad.git
cd SWE-Squad
pip install python-dotenv pyyaml
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```bash
# Required
SWE_TEAM_ENABLED=true
SWE_TEAM_ID=my-squad
SWE_GITHUB_ACCOUNT=my-bot-account    # Dedicated GitHub account for the squad
SWE_GITHUB_REPO=owner/repo           # Repository to monitor
GH_TOKEN=ghp_...                     # GitHub PAT with repo scope

# Optional
TELEGRAM_BOT_TOKEN=...               # For alerts
TELEGRAM_CHAT_ID=...                 # For alerts
SUPABASE_URL=...                     # For shared ticket store
SUPABASE_ANON_KEY=...                # For shared ticket store
```

### 3. Run

```bash
# Bootstrap — acknowledge existing errors on first run
python scripts/ops/swe_team_runner.py --bootstrap -v

# Single scan cycle
python scripts/ops/swe_team_runner.py -v

# Daemon mode (continuous 30-minute cycles)
python scripts/ops/swe_team_runner.py --daemon -v

# Daily summary via Telegram
python scripts/ops/swe_team_runner.py --summary
```

### 4. Test

```bash
python -m pytest tests/unit/test_swe_team.py -v
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SWE_TEAM_ENABLED` | ✅ | Kill switch (`true`/`false`) |
| `SWE_TEAM_ID` | ✅ | Unique team identifier for ticket scoping |
| `SWE_GITHUB_ACCOUNT` | ✅ | Dedicated GitHub bot account for issue assignment |
| `SWE_GITHUB_REPO` | ✅ | Target repository (`owner/repo`) |
| `GH_TOKEN` | ✅ | GitHub PAT with `repo` scope |
| `SWE_TEAM_CONFIG` | — | Path to `swe_team.yaml` (default: `config/swe_team.yaml`) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for alerts |
| `SUPABASE_URL` | — | Enables Supabase ticket store + semantic memory |
| `SUPABASE_ANON_KEY` | — | Supabase authentication key |
| `EMBEDDING_MODEL` | — | Embedding model (default: `bge-m3`) |
| `EMBEDDING_API_URL` | — | OpenAI-compatible embedding endpoint (falls back to `BASE_LLM_API_URL`) |
| `EMBEDDING_API_KEY` | — | API key for embeddings (falls back to `BASE_LLM_API_KEY`) |
| `T1_MODEL` | — | Override for T1 heavy tier (default: `opus`) |
| `T2_MODEL` | — | Override for T2 standard tier (default: `sonnet`) |
| `T3_MODEL` | — | Override for T3 fast tier (default: `haiku`) |
| `SWE_REMOTE_NODES` | — | JSON array of SSH worker nodes for log collection |

### YAML Config (`config/swe_team.yaml`)

Controls governance thresholds, monitoring patterns, and agent definitions. See the included config file for full documentation.

---

## 🗄️ Ticket Store

Two backends are available — the runner auto-selects based on environment variables:

| Backend | When | Pros | Setup |
|---------|------|------|-------|
| **📁 JSON** | `SUPABASE_URL` not set | Zero deps, single file, works anywhere | Nothing — it's the default |
| **☁️ Supabase** | `SUPABASE_URL` + `SUPABASE_ANON_KEY` set | Multi-agent, queryable, audit trail, real-time | Run `scripts/ops/supabase_schema.sql` |

### Supabase Schema

```bash
psql $DATABASE_URL -f scripts/ops/supabase_schema.sql
```

Creates:
- `swe_tickets` — main work queue with team scoping
- `swe_ticket_events` — immutable audit trail
- Views: `v_backlog`, `v_queue_critical`, `v_queue_by_agent`, `v_stability`

---

## 👥 Multi-Team Support

Multiple SWE Squads can operate independently on the same infrastructure:

```
Squad Alpha (team_id: "alpha")  ──▶  Supabase  ◀──  Squad Beta (team_id: "beta")
     │                                                    │
     ▼                                                    ▼
  Repo A (issues assigned to @bot-alpha)            Repo B (issues assigned to @bot-beta)
```

Each squad:
- Has its own `team_id` scoping all tickets
- Uses a dedicated GitHub bot account
- Only picks up issues assigned to its account
- Shares the Supabase backend without overlap

---

## 📦 Components

### Agents

| Module | Role | What it does |
|--------|------|-------------|
| `monitor_agent.py` | Sentinel | Scans logs for errors, deduplicates by fingerprint, creates tickets |
| `triage_agent.py` | Dispatcher | Classifies severity, routes to specialist agents by module |
| `investigator.py` | Diagnostician | Root cause analysis via Claude Code CLI with semantic memory context |
| `developer.py` | Implementor | Keep/discard fix loop on git branches, 3-attempt escalation |
| `creative_agent.py` | Optimizer | Analyzes resolved tickets, proposes preventive improvements |
| `preflight.py` | Validator | Pre-flight checks (git identity, repo, env) before any agent executes |

### Governance & Quality

| Module | Role | What it does |
|--------|------|-------------|
| `ralph_wiggum.py` | Stability Gate | Blocks all work if open critical bugs exceed threshold |
| `governance.py` | Deploy Governor | Validates fix complexity (max files, lines, module boundaries) |
| `distiller.py` | Knowledge Cache | Caches successful fixes by fingerprint for zero-cost deterministic replay |

### Memory & Storage

| Module | Role | What it does |
|--------|------|-------------|
| `embeddings.py` | Memory Encoder | bge-m3 embeddings + mem0-style fact extraction via cheap T3 model |
| `supabase_store.py` | Cloud Store | pgvector similarity search, audit trail, multi-team scoping (zero-dep) |
| `ticket_store.py` | Local Store | JSON file-backed with fingerprint dedup — works offline, no setup |

### Communication & Ops

| Module | Role | What it does |
|--------|------|-------------|
| `telegram.py` | Alerting | Standalone Bot API client (stdlib only) — alerts, summaries, HITL escalation |
| `notifier.py` | Notification Hub | Routes alerts: new tickets, gate blocks, regressions, daily/cycle reports |
| `github_integration.py` | GitHub Bridge | Creates issues, comments with investigation reports and fix results |
| `remote_logs.py` | Log Collector | SSH/rsync log aggregation from remote worker machines |
| `a2a/adapters/swe_team.py` | A2A Adapter | Exposes squad skills via A2A protocol for cross-agent coordination |

### Entry Points & Config

| File | Purpose |
|------|---------|
| `scripts/ops/swe_team_runner.py` | Main runner — cron, daemon, bootstrap, reports, keep-alive modes |
| `scripts/ops/swe_cli.py` | CLI tool — status, tickets, issues, repos, summary, report |
| `scripts/ops/supabase_schema.sql` | Database schema + pgvector migration |
| `config/swe_team.yaml` | Governance thresholds, model tiers, memory, agent definitions |
| `config/swe_team/programs/` | Markdown prompt programs (investigate, fix, orchestrate) |

---

## 📋 Requirements

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — the AI backbone
- **[GitHub CLI](https://cli.github.com/)** (`gh`) — authenticated for issue management
- **SSH access** to worker machines (optional, for remote log collection)
- **Telegram bot** (optional, for notifications)
- **Supabase project** (optional, for shared multi-team ticket store)

---

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Setup

```bash
git clone https://github.com/ArtemisAI/SWE-Squad.git
cd SWE-Squad
pip install python-dotenv pyyaml pytest
cp .env.example .env
# Edit .env with your test credentials
python -m pytest tests/unit/test_swe_team.py -v
```

### Areas We'd Love Help With

- 🔌 Additional ticket store backends (Redis, SQLite, PostgreSQL direct)
- ⚙️ CI/CD pipeline integration (GitHub Actions, GitLab CI)
- 📊 Web dashboard for ticket monitoring
- 💬 Additional notification channels (Slack, Discord, email)
- 🧪 Agent prompt optimization and benchmarking
- 📖 Documentation and tutorials

---

## 🗺️ Roadmap

| Status | Feature |
|--------|---------|
| ✅ | Core agent loop (monitor → triage → investigate → fix → validate) |
| ✅ | pgvector semantic memory with mem0-style fact extraction |
| ✅ | A2A protocol adapter for cross-machine agent coordination |
| ✅ | Ralph Wiggum stability gate |
| ✅ | Closed-loop regression detection and fix confidence scoring |
| ✅ | Trajectory distillation (cached fixes) |
| ✅ | Supabase ticket store with multi-team scoping |
| ✅ | T1/T2/T3 configurable model cost tiers |
| ✅ | Pre-flight validation gate (context checks before execution) |
| ✅ | Telegram notifications, cron reports, CLI tools |
| ✅ | Session progress logs and heartbeat stall detection |
| 🔲 | Gemini CLI / OpenCode direct integration |
| 🔲 | Web dashboard for ticket monitoring |
| 🔲 | GitHub Actions CI/CD integration |
| 🔲 | Slack/Discord notifications |
| 🔲 | Metrics and observability (Prometheus/Grafana) |

---

## 💖 Support & Sponsoring

If SWE Squad is useful to your team, consider supporting the project:

<p align="center">
  <a href="https://github.com/sponsors/ArtemisAI">
    <img src="https://img.shields.io/badge/Sponsor-ArtemisAI-ea4aaa?logo=github-sponsors&logoColor=white&style=for-the-badge" alt="Sponsor">
  </a>
</p>

- ⭐ **Star** this repo to help others discover it
- 🐛 **Report issues** — bug reports and feature requests are valuable contributions
- 📣 **Share** with your team — the more users, the better the project gets
- 🤝 **Contribute** — PRs are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md)

For enterprise support or custom deployments, reach out via [GitHub Discussions](https://github.com/ArtemisAI/SWE-Squad/discussions).

---

## 📄 License

[MIT](LICENSE) — use it, fork it, build on it.

---

<p align="center">
  <sub>Made with ❤️ by <a href="https://github.com/ArtemisAI">ArtemisAI</a></sub>
</p>
