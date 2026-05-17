# CloudLens

**An observability lens for Cloud Run — built for agents.**

CloudLens is an MCP server that gives an LLM agent end-to-end visibility into a
GCP project's Cloud Run footprint: logs, metrics, traces, deployments, and
Error Reporting groups, all in token-efficient shapes designed for reasoning.

The point: instead of asking Claude to grep raw `gcloud` output (and burn
thousands of tokens on JSON envelopes), CloudLens returns compact,
request-correlated views — so the agent can actually answer *"is this service
healthy?"*, *"what changed since the deploy?"*, and *"why did this request
fail?"*.

CloudLens also ships a **terminal viewer** (`cloudlens-watch`) — a TUI for
tailing, searching, and trace-stitching Cloud Run logs across one, many, or
all services without leaving the terminal. See [Local viewer](#local-viewer).

A second viewer is being written in Go (Bubble Tea + Lip Gloss) for
single-binary distribution and streaming-RPC tail — see [`watch/`](watch/)
for the work-in-progress build.

## What you get

### Health & metrics
| Tool | Purpose |
|------|---------|
| `get_health` | Snapshot: requests, error rate (5xx/4xx), latency p50/p95/p99, instances, cold starts, active revisions, deploys in window |
| `get_metric` | Time series for one named metric (10 curated names) |
| `diff_windows` | Compare last `N` hours vs the previous `N` hours — best "what changed since…?" tool |
| `diff_revisions` | **Compare two revisions head-to-head** — per-revision request count, error rate, p95/p99, plus errors new in `rev_b` that didn't appear under `rev_a`. When either side has fewer than 30 requests in the window, percentile/rate `delta_pct` is suppressed (raw values stay) and a `sample_warning` is added — small-sample comparisons looked impressive but lied |

### Logs
| Tool | Purpose |
|------|---------|
| `search_logs` | **Free-text search across one, many, or all services in the project.** Each entry tagged with `svc` so you can tell which service matched. Pass `regex=True` for RE2 regex (server-side, restricted subset — see Notes) |
| `summarize_logs` | Severity/status mix, **top errors clustered across the full window** (two-query: separate scan for errors+5xx so rare failures aren't lost in the recent-sample bias) |
| `get_recent_traces` | Distinct traces with http summary, error count, 5xx count. `failing_only=True` queries failing entries directly — finds real failures even when recent activity is healthy |
| `get_logs_by_trace` | Every entry for a trace ID, oldest → newest, **stitched across services** |
| `get_logs` | Filtered fetch (severity, text, time) |
| `get_errors` | Failing entries: `severity>=ERROR OR httpRequest.status>=500` by default. The 5xx fallback matters because Cloud Run's request logger writes 5xx with INFO severity — pure severity filters miss them |
| `tail_logs` | Entries newer than ISO timestamp |
| `count_logs` | Capped count for a/b checks |
| `find_stalled_tasks` | **Find long-running tasks that went silent.** `group_by` resolves in order against jsonPayload, labels, then a key=value/key:value regex over the textPayload (so plain `group_by="job_id"` works for stdlib `logging` apps that don't emit structured fields). For arbitrary extraction (e.g. an ID embedded in a URL path), pass `group_by="regex:/api/sync/status/([a-f0-9-]{20,})"`. Flags candidates idle past threshold with the annotation `likely_killed_by_deploy` if a revision rollover landed within ~60s of the last entry. `sample_cap` (default 5000, max 10000) bounds how far back the scan reaches on busy services |

All log tools take `exclude_noise=True` by default — drops audit logs and
Cloud Run sidecar output (GCSFuse / Cloud SQL Proxy / OTel collector). Pass
`exclude_noise=False` to include them.

### Deployments & errors & traces
| Tool | Purpose |
|------|---------|
| `list_services` | All Cloud Run services in the project |
| `list_revisions` | Revision history with traffic split, image, scaling |
| `list_error_groups` | Clustered stack traces from Error Reporting (returns `dict` with `groups` + optional `error/fix` for graceful API-disabled handling) |
| `get_error_group` | Drill into a group with sample events |
| `get_trace` | Span tree for a trace ID with per-span timing |

## Recommended agent workflows

CloudLens injects these into the agent's system prompt via the MCP
`instructions` field, so most clients pick them up automatically.

**"Is X broken right now?"**
```
1. get_health(service)                          — snapshot
2. get_recent_traces(service, failing_only=T)   — which requests failed
3. get_logs_by_trace(trace_id)                  — cross-service drill-down
4. diff_windows(service)                        — what changed vs prior window
```

**"Did the new revision regress something?"**
```
1. list_revisions(service)                      — find the two revisions
2. diff_revisions(service, rev_a, rev_b)        — head-to-head metrics + new errors
```

**"Is my 30-min sync / background task still alive?"**
```
find_stalled_tasks(service, group_by="sync_id", idle_minutes=10)
  → matches sync_id in structured payload, labels, or
    regex(sync_id[=:]<value>) in the textPayload (stdlib logging).
    Flags multi-step tasks idle past threshold; annotated
    likely_killed_by_deploy if a revision rollover landed within
    ~60s of the last entry. For IDs embedded in URLs etc., pass
    group_by="regex:..." with a Python re capture group. Crank
    sample_cap if hunting historical stalls on a busy service.
```

**"Where did `<some token>` happen across my project?"**
```
search_logs(text="...", services=None)
  → matches across every Cloud Run service in the project,
    each entry tagged with `svc`
```

## Why each piece exists

**`failing_only=True` and `include_5xx=True` defaults** — Cloud Run's built-in
request logger writes 5xx response logs with `severity=INFO`. Python apps
often emit stdout without explicit severity routing too. A pure severity
filter misses real failures. CloudLens defaults to scanning by severity *or*
HTTP status, so the agent doesn't trip on this gotcha.

**Two-query `summarize_logs`** — A single 1000-entry sample on a busy service
is dominated by the most recent INFO/DEFAULT entries; rare 5xx from earlier
in the window never reach `top_errors`. CloudLens does a second targeted
scan for errors+5xx, so `top_errors` actually contains them.

**Compact entry shape** — Each log entry is `{ts, sev, msg, trace, span,
http, svc, rev}`. No resource labels repeated per line, no `insertId`,
no `logName`. Roughly 20× smaller than `gcloud logging read --format=json`.
The `svc` field is what makes `search_logs` legible across services.

**Graceful API-disabled handling** — If Error Reporting / Cloud Trace isn't
enabled in the project, the affected tool returns
`{"error": "api_disabled", "fix": "<console URL>"}` so the agent can tell
the user how to enable it instead of crashing the call.

**Default noise filter** — Cloud Run projects are full of `Services.GetService`
audit logs and Cloud Run-managed sidecar output (GCSFuse, Cloud SQL Proxy,
OTel). All log tools default to `exclude_noise=True` to drop these so the
agent doesn't waste tokens reasoning about them.

**Stalled-task detection** — `find_stalled_tasks` groups log entries by a
structured field and flags groups whose newest entry is older than
`idle_minutes` *and* whose duration ≥ `idle_minutes`. The duration floor
filters out short completed requests so only genuinely long-running tasks
qualify. If a deploy landed within ~60s after the last entry, the result
is annotated `likely_killed_by_deploy` — the classic Cloud Run failure
mode where a revision rollover terminates in-flight in-process tasks.

## Setup

### 1. Enable required APIs

```sh
gcloud services enable \
  logging.googleapis.com \
  monitoring.googleapis.com \
  run.googleapis.com \
  cloudtrace.googleapis.com \
  clouderrorreporting.googleapis.com \
  --project <PROJECT_ID>
```

CloudLens degrades gracefully if any are off — the affected tool returns a
structured `{"error": "api_disabled", "fix": "..."}`.

### 2. Authenticate

```sh
gcloud auth application-default login
```

The principal needs:
`roles/logging.viewer`, `roles/monitoring.viewer`, `roles/run.viewer`,
`roles/cloudtrace.user`, `roles/errorreporting.viewer`.

### 3. Install

From this directory:

```sh
pip install -e .
```

Or run via `uvx` without installing:

```sh
uvx --from . cloudlens
```

### 4. Register with your MCP client

> **Replace `<PROJECT_ID>` with your GCP project ID** (e.g. `my-project`)
> and `<PATH_TO_REPO>` with this directory's absolute path. CloudLens reads
> `GOOGLE_CLOUD_PROJECT` once at startup — if it's wrong, every tool
> silently hits the wrong project.

#### Claude Code

User scope (every project):

```sh
claude mcp add -s user cloudlens \
  --env GOOGLE_CLOUD_PROJECT=<PROJECT_ID> \
  -- uvx --from <PATH_TO_REPO> cloudlens
```

Project scope (current repo only):

```sh
claude mcp add cloudlens \
  --env GOOGLE_CLOUD_PROJECT=<PROJECT_ID> \
  -- uvx --from <PATH_TO_REPO> cloudlens
```

After registering — or after changing the project ID — **restart your Claude
Code session**. MCP env vars are read once when the stdio process spawns.

#### Generic JSON config (Claude Desktop, etc.)

```json
{
  "mcpServers": {
    "cloudlens": {
      "command": "uvx",
      "args": ["--from", "<PATH_TO_REPO>", "cloudlens"],
      "env": { "GOOGLE_CLOUD_PROJECT": "<PROJECT_ID>" }
    }
  }
}
```

#### Verify

```sh
claude mcp get cloudlens     # check env.GOOGLE_CLOUD_PROJECT
```

Or have the agent call `list_services()` — the returned `uri` fields embed
the project hash, so it's obvious if it's pointing at the wrong project.

## Local viewer

`cloudlens-watch` is a terminal UI over the same `Observability` clients the
MCP server uses.

**Quick start** — `start.sh` creates a venv, installs the package, and launches
the TUI in your terminal:

```sh
cp .env.example .env       # then set GOOGLE_CLOUD_PROJECT
./start.sh                 # tail every Cloud Run service
./start.sh -s api,worker   # extra args pass through to cloudlens-watch
```

**Manual** — if you'd rather manage the env yourself:

```sh
pip install -e .
cloudlens-watch                              # tail every Cloud Run service
cloudlens-watch -s api,worker                # tail two services
cloudlens-watch -s api --hours 4             # 4h lookback, single service
GOOGLE_CLOUD_PROJECT=my-proj cloudlens-watch # explicit project
```

Multi-service queries are a single OR'd Cloud Logging filter — entries come
back interleaved and timestamp-ordered. Each row is colored by `svc` (stable
hash), so handoffs across services stand out visually.

The viewer has three modes shown in the status bar:

- **● LIVE** — polling tail every 2s
- **⏸ PAUSED** — buffer frozen, no polling
- **🔍 SEARCH** — server-side query results, polling stopped

**Keybinds**

| key | action |
|-----|--------|
| `enter` | open detail view for the selected row |
| `c` | context — focus on this row's service, ±25 entries around it |
| `t` | trace drill-down for the selected row — cross-service stitch |
| `/` | search Cloud Logging (server-side, scoped to current service selection) |
| `s` | service picker (`space` toggle · `a` all · `n` none · `enter` apply) |
| `space` | pause / resume live polling |
| `esc` | return to live mode (closes search input or exits search results) |
| `q` | quit |

`c` (context), `t` (trace), and `enter` (details) work from inside the trace
and context views too, so you can drill recursively: search → context →
trace → context elsewhere → back back back.

The `trc` column shows `●` for rows that carry a `trace_id` (drillable with
`t`) and `·` for rows without one — Cloud Run only attaches trace IDs to
entries logged inside an HTTP request context, so background/startup logs
won't have them.

**Trace drill-down** calls `get_logs_by_trace` with no service filter, so it
stitches every entry for that trace across the project — even services not
in your current selection.

**Search scope** follows the active service selection: search "all services"
by default, or `s` first to narrow to one. Default lookback is 24h, capped
at 300 results.

## Notes

- **Project ID** comes from `$GOOGLE_CLOUD_PROJECT` (or `$GCP_PROJECT`).
- **Region** is auto-discovered per service (cached after the first
  `list_services` call). Pass `region=` to override.
- **Trace IDs in logs vs Cloud Trace** — Cloud Run propagates trace IDs
  into log entries automatically, but does *not* export spans to Cloud
  Trace unless the service is instrumented (e.g. OpenTelemetry).
  `get_trace` returns `{"found": false, "hint": "..."}` in that case;
  logs still correlate via `get_logs_by_trace`.
- **`search_logs(regex=True)` accepts a restricted RE2 subset** —
  Cloud Logging evaluates the regex server-side and supports anchors,
  quantifiers, alternation, and bracket character classes (`[a-z]`,
  `[0-9]`). It does **not** support shorthand classes like `\w` or
  `\d`. Use `[a-zA-Z0-9_]` and `[0-9]` instead. (`find_stalled_tasks`
  regex is client-side Python `re` and supports the full syntax.)

## Architecture

```
cloudlens/
├── server.py     FastMCP server: 18 tools + workflow instructions
├── api.py        Lazy facade composing the per-API clients
├── analysis.py   Cross-client compositions:
│                   health, summarize-with-deploys,
│                   diff_windows, diff_revisions, find_stalled_tasks
├── logs.py       Cloud Logging — build_filter (services, revision,
│                   exclude_noise), summary, search, stalled-task grouping
├── metrics.py    Cloud Monitoring — named-metric registry,
│                   parallel queries, per-revision filtering
├── traces.py     Cloud Trace v1
├── errors.py     Error Reporting (graceful api_disabled handling)
├── runtime.py    Cloud Run services + revisions (region cache,
│                   LATEST traffic-allocation resolution)
└── format.py     Compact shaping: entries, spans, error groups,
                    http_cluster_key for stable 5xx aggregation
```

Each client targets one GCP API surface; `analysis.py` is where multi-API
tools (`get_health`, `diff_windows`, `diff_revisions`, `find_stalled_tasks`)
compose primitives. New tools should usually live there, not in individual
clients.
