# Operational Monitoring Guide

## Health Check Protocol

Both alpha and beta teams run 30-minute health checks monitoring:
1. **Daemon status** — is `swe_team_runner.py` running?
2. **Claude process count** — >5 concurrent = suspicious (rogue agent)
3. **Error patterns** — ERROR/CRITICAL/TimeoutError in last log lines
4. **Pipeline progression** — tickets moving from investigation → development → PR

## Key Metrics

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Daemon processes | 1 | 0 (restart) | >1 (duplicates) |
| Claude processes | 0-4 | 5-8 | >8 (rogue) |
| Cycle errors | 0 | 1-2 per hour | >5 per hour |
| Investigation timeout rate | <10% | 10-30% | >30% |
| Development success rate | >50% | 20-50% | <20% |

## Error Classification

The pipeline classifies errors for appropriate retry behavior:

| Category | Examples | Action |
|----------|----------|--------|
| **Transient — backoff** | 429, 529 overloaded, 500 server error | Exponential backoff (30s-300s) |
| **Transient — retry** | Timeout, empty output | 1-2 retries with fresh session |
| **Session error** | Stale session, resume failure | Fresh session retry |
| **Permanent** | 401 unauthorized, model not found | Alert + fail immediately |

## Circuit Breaker

When development failure rate exceeds 80% (rolling window of 10), the daemon
pauses for 30 minutes. This prevents token burn during systemic failures
(e.g., API outage, quota exhaustion).
