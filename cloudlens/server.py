"""CloudLens — FastMCP server for Cloud Run observability.

Auth: Application Default Credentials.
Project ID: read once from $GOOGLE_CLOUD_PROJECT (or $GCP_PROJECT) on startup.
"""

from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .api import Observability
from .format import format_entries
from .metrics import NAMED_METRIC_NAMES


SERVER_INSTRUCTIONS = """\
CloudLens — Cloud Run observability for the configured GCP project:
logs, metrics, traces, deployments, and Error Reporting groups.

Default investigation workflow ("is X broken?"):
  1. get_health(service)                              — snapshot
  2. get_recent_traces(service, failing_only=True)    — which requests failed
  3. get_logs_by_trace(trace_id)                      — cross-service drill-down
  4. diff_windows(service)                            — did the latest deploy regress?

Other useful tools:
  - search_logs(text, services=None) — text search across one/many/all services
  - summarize_logs(service)          — severity/status mix + top errors + deploys
  - list_error_groups(service)       — Error Reporting clustered stack traces
  - get_trace(trace_id)              — span tree with timing breakdown
  - list_revisions(service)          — deploy history with traffic split
  - get_metric(service, metric)      — single metric time series
  - diff_revisions(service, a, b)    — compare two revisions head-to-head
  - find_stalled_tasks(service, group_by) — tasks that went silent
                                            (flags revision-shift kills)

Pitfalls and tips:
- Cloud Run request logs carry httpRequest.status but severity stays INFO.
  Use get_recent_traces(failing_only=True) and get_errors(include_5xx=True)
  — pure severity filters miss 5xx responses (common with Python apps).
- get_logs_by_trace stitches logs across services that share a trace ID —
  this is the highest-leverage drill-down tool.
- For "did the deploy break it?" use diff_windows, diff_revisions, or
  list_revisions.
- For "is my long-running task still alive?" use find_stalled_tasks.
- Trace IDs in logs may not exist in Cloud Trace — Cloud Run propagates the
  ID for log correlation but doesn't export spans without OpenTelemetry.
- All log tools default to exclude_noise=True (drops audit + sidecar logs).
  Pass exclude_noise=False when you need that signal.
- Region is auto-discovered from the service name; pass `region=` to override.
"""


mcp = FastMCP("cloudlens", instructions=SERVER_INSTRUCTIONS)
_obs: Observability | None = None


def _obs_get() -> Observability:
    global _obs
    if _obs is None:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        if not project:
            raise RuntimeError(
                "Set GOOGLE_CLOUD_PROJECT (or GCP_PROJECT) to the target GCP project ID."
            )
        _obs = Observability(project)
    return _obs


@mcp.tool()
def list_services(region: Optional[str] = None) -> list[dict]:
    """List Cloud Run services in the project."""
    return _obs_get().runtime.list_services(region=region)


@mcp.tool()
def list_revisions(
    service: str,
    hours: Optional[float] = None,
    region: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Revisions for a service, newest first.

    Each: revision, deployed_at, traffic_percent, active, image, min/max_instances,
    concurrency. Pass `hours` to filter to recent deploys only.
    """
    return _obs_get().runtime.list_revisions(
        service, region=region, hours=hours, limit=min(max(limit, 1), 100),
    )


@mcp.tool()
def get_health(
    service: str,
    hours: float = 1.0,
    region: Optional[str] = None,
) -> dict:
    """One-call health snapshot.

    Returns: request_count, error_rate_5xx_pct, error_rate_4xx_pct,
    latency_ms p50/p95/p99, instance_count avg/max, cold_starts,
    active_revisions, deploys_in_window. First call when investigating a service.
    """
    return _obs_get().analysis.health(service, hours=hours, region=region)


@mcp.tool()
def get_metric(
    service: str,
    metric: str,
    hours: float = 1.0,
    region: Optional[str] = None,
    points: int = 30,
) -> dict:
    """Time series for one named metric.

    metric is one of: request_count, request_count_5xx, request_count_4xx,
    latency_p50, latency_p95, latency_p99, instance_count, cold_starts,
    cpu_utilization, memory_utilization.
    """
    series = _obs_get().metrics.query(
        metric, service, hours=hours, region=region,
        points=min(max(points, 5), 200),
    )
    return {"metric": metric, "service": service, "hours": hours, "points": series}


@mcp.tool()
def diff_windows(
    service: str,
    hours: float = 1.0,
    region: Optional[str] = None,
) -> dict:
    """Compare the most recent `hours` window against the previous `hours` window.

    Returns deltas (request count, error rate, latency p95/p99, cold starts),
    error messages new in the now-window, and deploys that landed in it.
    Best tool for "what changed?".
    """
    return _obs_get().analysis.diff_windows(service, hours=hours, region=region)


@mcp.tool()
def list_error_groups(
    service: str,
    hours: float = 24.0,
    limit: int = 20,
) -> dict:
    """Recurring error clusters from Cloud Error Reporting.

    Returns {"groups": [...], "count": N}. Each group: group_id, count,
    affected_users, first_seen, last_seen, sample. If the API isn't enabled,
    returns {"groups": [], "error": "api_disabled", "fix": "<URL>"}.
    """
    return _obs_get().errors.list_groups(
        service, hours=hours, limit=min(max(limit, 1), 100),
    )


@mcp.tool()
def get_error_group(
    group_id: str,
    hours: float = 24.0,
    samples: int = 3,
    service: Optional[str] = None,
) -> dict:
    """Drill into one Error Reporting group: stats + sample event stack traces."""
    return _obs_get().errors.get_group(
        group_id, hours=hours, samples=min(max(samples, 1), 10), service=service,
    )


@mcp.tool()
def get_trace(trace_id: str) -> dict:
    """Span tree for a trace ID: each span's name, parent, start, duration_ms.

    Use after get_logs_by_trace to see where time was actually spent
    (downstream calls and their timing).
    """
    return _obs_get().traces.get_trace(trace_id)


@mcp.tool()
def get_logs(
    service: str,
    hours: float = 1.0,
    severity: Optional[str] = None,
    text: Optional[str] = None,
    limit: int = 100,
    region: Optional[str] = None,
    exclude_noise: bool = True,
) -> list[dict]:
    """Recent log entries for one service, newest first.

    Compact shape: ts, sev, msg, trace, http, svc, rev.
    `exclude_noise=True` (default) drops audit logs and Cloud Run sidecar
    output (GCSFuse / Cloud SQL Proxy / OTel collector).
    """
    entries = _obs_get().logs.get_logs(
        service=service, hours=hours, severity=severity, text=text,
        limit=min(max(limit, 1), 500), region=region,
        exclude_noise=exclude_noise,
    )
    return format_entries(entries)


@mcp.tool()
def search_logs(
    text: str,
    services: Optional[list[str]] = None,
    hours: float = 1.0,
    severity: Optional[str] = None,
    limit: int = 100,
    regex: bool = False,
    exclude_noise: bool = True,
) -> list[dict]:
    """Free-text search across logs in the project.

    `services=None` searches every Cloud Run service in the project; pass a
    list to scope to specific ones. Each entry includes `svc` so you can tell
    which service matched.

    `regex=True` switches `text` to RE2 regex matching against
    `textPayload` and `jsonPayload.message` (Cloud Logging server-side).
    Cloud Logging's regex is a restricted RE2 subset — anchors (`^`, `$`),
    quantifiers (`*`, `+`, `?`), alternation (`|`), and bracket character
    classes (`[a-z]`, `[0-9]`) work; **shorthand classes like `\\w` and
    `\\d` do not**. Default `regex=False` does a substring match (cheaper).
    """
    entries = _obs_get().logs.search(
        text=text, services=services, hours=hours, severity=severity,
        limit=min(max(limit, 1), 500), regex=regex, exclude_noise=exclude_noise,
    )
    return format_entries(entries)


@mcp.tool()
def get_logs_by_trace(trace_id: str, hours: float = 24.0, limit: int = 500) -> list[dict]:
    """Every log entry for a trace ID, oldest → newest. Stitches services together."""
    entries = _obs_get().logs.get_logs_by_trace(
        trace_id=trace_id, hours=hours, limit=min(max(limit, 1), 1000),
    )
    return format_entries(entries)


@mcp.tool()
def get_recent_traces(
    service: str,
    hours: float = 1.0,
    min_severity: Optional[str] = None,
    limit: int = 20,
    region: Optional[str] = None,
    failing_only: bool = False,
    exclude_noise: bool = True,
) -> list[dict]:
    """Recent distinct traces with http summary, error count, and 5xx count.

    Set `failing_only=True` to keep only traces that had at least one ERROR
    severity entry OR a 5xx response — useful when the app logs at INFO/DEFAULT
    even on failure (common with Python apps).
    """
    return _obs_get().logs.get_recent_traces(
        service=service, hours=hours, min_severity=min_severity,
        limit=min(max(limit, 1), 100), region=region,
        failing_only=failing_only, exclude_noise=exclude_noise,
    )


@mcp.tool()
def get_errors(
    service: str,
    hours: float = 1.0,
    limit: int = 50,
    region: Optional[str] = None,
    include_5xx: bool = True,
    exclude_noise: bool = True,
) -> list[dict]:
    """Failing log entries.

    By default returns severity>=ERROR plus any entry with httpRequest.status>=500.
    The 5xx fallback matters because Cloud Run's request logger writes 5xx
    responses with INFO severity — pure severity filters miss them. Pass
    `include_5xx=False` to revert to severity-only.
    """
    entries = _obs_get().logs.get_failing(
        service=service, hours=hours, region=region,
        limit=min(max(limit, 1), 500),
        include_5xx=include_5xx, exclude_noise=exclude_noise,
    )
    return format_entries(entries)


@mcp.tool()
def tail_logs(
    service: str,
    since_iso: Optional[str] = None,
    limit: int = 200,
    region: Optional[str] = None,
    exclude_noise: bool = True,
) -> list[dict]:
    """Logs newer than the given ISO timestamp (default: last 5 min)."""
    entries = _obs_get().logs.tail_logs(
        service=service, since_iso=since_iso,
        limit=min(max(limit, 1), 500), region=region,
        exclude_noise=exclude_noise,
    )
    return format_entries(entries)


@mcp.tool()
def find_stalled_tasks(
    service: str,
    group_by: str,
    hours: float = 2.0,
    idle_minutes: float = 10.0,
    min_entries: int = 2,
    sample_cap: int = 5000,
    region: Optional[str] = None,
) -> list[dict]:
    """Find long-running tasks that went silent.

    Groups log entries by a structured field (e.g. `sync_id`, `task_id`,
    `job_id`) and returns groups whose newest entry is older than
    `idle_minutes` ago. `min_entries` (default 2) filters out one-shot
    requests so only multi-step tasks qualify.

    If a deploy landed within ~60s after the last entry, the result includes
    a `likely_killed_by_deploy` annotation — the classic Cloud Run failure
    mode where a revision rollover terminates in-flight in-process tasks.

    `group_by` matches (in order) against jsonPayload, entry labels, and
    finally a regex against the textPayload of the form
    `<group_by>[=:]<value>` — so plain `group_by="job_id"` works on Python
    apps that log via stdlib `logging` (where the field lands in the
    message rather than as a structured payload field). For arbitrary
    extraction (e.g. an ID embedded in a URL path), pass
    `group_by="regex:..."` with a Python `re` pattern using a capturing
    group, e.g. `regex:/api/sync/status/([a-f0-9-]{20,})`.

    `sample_cap` bounds how many entries are scanned (default 5000, max
    10000). For very busy services, the newest 5000 entries may not span
    the requested `hours` window — crank `sample_cap` or narrow `hours` if
    you're hunting historical stalls.
    """
    return _obs_get().analysis.find_stalled_tasks(
        service=service, group_by=group_by,
        hours=hours, idle_minutes=idle_minutes,
        min_entries=min_entries, region=region,
        sample_cap=min(max(sample_cap, 100), 10000),
    )


@mcp.tool()
def diff_revisions(
    service: str,
    rev_a: str,
    rev_b: str,
    hours: float = 24.0,
    region: Optional[str] = None,
) -> dict:
    """Compare two revisions of the same service.

    Returns per-revision request_count, error rates, latency p50/p95/p99,
    cold starts; deltas (b vs a); and error messages new in `rev_b` that
    didn't appear under `rev_a`. The revision filter is applied to both
    metrics and logs.
    """
    return _obs_get().analysis.diff_revisions(
        service=service, rev_a=rev_a, rev_b=rev_b,
        hours=hours, region=region,
    )


@mcp.tool()
def summarize_logs(
    service: str,
    hours: float = 1.0,
    region: Optional[str] = None,
) -> dict:
    """Severity / status mix, top error messages, deploys in the window."""
    return _obs_get().analysis.summarize_with_deploys(
        service, hours=hours, region=region,
    )


@mcp.tool()
def count_logs(
    service: str,
    hours: float = 1.0,
    severity: Optional[str] = None,
    text: Optional[str] = None,
    region: Optional[str] = None,
    exclude_noise: bool = True,
) -> dict:
    """Count matching entries (capped at 1000). For a/b checks across windows."""
    cap = 1000
    n = _obs_get().logs.count_logs(
        service=service, hours=hours, severity=severity, text=text,
        region=region, cap=cap, exclude_noise=exclude_noise,
    )
    return {"count": n, "capped": n >= cap}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
