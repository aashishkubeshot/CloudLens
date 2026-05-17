"""Format log entries as briefs for pasting into an AI agent.

Output is markdown — readable to a human, structured enough for an LLM to
parse, and front-loaded with the facts an agent needs to investigate:

  * project + service + revision + trace_id + timestamps
  * a Cloud Logging Explorer deep link for human verification
  * concrete CloudLens MCP tool calls the agent can run if the MCP is
    registered ("get_logs_by_trace(...)", "get_health(...)", etc.)

Three brief shapes correspond to the three TUI surfaces you can copy from:
single entry, full cross-service trace, service-focused context window.
"""

from __future__ import annotations

from urllib.parse import quote


def cloud_logging_url(project: str, entry: dict) -> str:
    """Deep link into Cloud Logging Explorer with a query that pulls `entry`."""
    parts = ['resource.type="cloud_run_revision"']
    svc = entry.get("svc")
    if svc:
        parts.append(f'resource.labels.service_name="{svc}"')
    trace = entry.get("trace")
    if trace:
        parts.append(f'trace="projects/{project}/traces/{trace}"')
    query = " AND ".join(parts)
    return (
        f"https://console.cloud.google.com/logs/query;"
        f"query={quote(query)}?project={project}"
    )


def _meta_lines(entry: dict, project: str) -> list[str]:
    out = [f"- **Project:** `{project}`"]
    if entry.get("ts"):
        out.append(f"- **Time:** `{entry['ts']}`")
    svc, rev = entry.get("svc"), entry.get("rev")
    if svc:
        rev_part = f"  (revision `{rev}`)" if rev else ""
        out.append(f"- **Service:** `{svc}`{rev_part}")
    if entry.get("inst"):
        # Instance ID matters to agents debugging Cloud Run: it disambiguates
        # which container handled this request, and lets the agent correlate
        # the entry to OOMs / autoscaling / cold-start events for that ID.
        out.append(f"- **Instance:** `{entry['inst']}`")
    out.append(f"- **Severity:** `{entry.get('sev', 'DEFAULT')}`")
    if entry.get("http"):
        out.append(f"- **HTTP:** `{entry['http']}`")
    if entry.get("trace"):
        out.append(f"- **Trace ID:** `{entry['trace']}`")
    return out


def _mcp_hooks_for_entry(entry: dict) -> list[str]:
    svc, trace = entry.get("svc"), entry.get("trace")
    out: list[str] = []
    if trace:
        out.append(
            f'- `get_logs_by_trace("{trace}")` — every entry across services '
            "for this request, oldest → newest"
        )
    if svc:
        out += [
            f'- `get_health("{svc}")` — request count, error rate, latency '
            "p50/p95/p99 for the last hour",
            f'- `diff_windows("{svc}")` — what changed vs the previous window '
            "(deploys, error rate, latency deltas)",
            f'- `get_errors("{svc}")` — other failing entries (severity≥ERROR '
            "or 5xx) in the window",
            f'- `list_revisions("{svc}", hours=24)` — recent deploys with '
            "traffic split",
        ]
    return out


def format_entry_share(entry: dict, project: str) -> str:
    msg = entry.get("msg") or entry.get("http") or "(no message)"
    out = [
        "# CloudLens log share",
        "",
        "Help me debug this Cloud Run log. If you have the `cloudlens` MCP "
        "registered, run the tool calls under **CloudLens MCP hooks** below "
        "to investigate — they're literal tool names with arguments filled in.",
        "",
        "## The log",
        "",
        *_meta_lines(entry, project),
        "",
        "**Message:**",
        "```",
        msg,
        "```",
    ]
    hooks = _mcp_hooks_for_entry(entry)
    if hooks:
        out += ["", "## CloudLens MCP hooks", "", *hooks]
    out += ["", "## Cloud Logging Explorer", "", cloud_logging_url(project, entry)]
    return "\n".join(out)


def _entry_line(e: dict, width_svc: int = 0, anchor: bool = False) -> str:
    ts = e.get("ts", "?")
    sev = e.get("sev", "DEFAULT")
    svc = e.get("svc", "")
    msg = (e.get("msg") or e.get("http") or "").replace("\n", " ")[:280]
    marker = "▶" if anchor else " "
    if width_svc:
        return f"{marker} [{ts}] {sev:<8} {svc:<{width_svc}} {msg}"
    return f"{marker} [{ts}] {sev:<8} {msg}"


def format_trace_share(
    trace_id: str, entries: list[dict], project: str,
) -> str:
    if not entries:
        return (
            f"# CloudLens trace share — `{trace_id}`\n\n"
            f"Trace not found in the last 24h."
        )
    services = sorted({e.get("svc", "?") for e in entries if e.get("svc")})
    width = max((len(s) for s in services), default=10)
    out = [
        f"# CloudLens trace share — `{trace_id}`",
        "",
        "Help me debug this Cloud Run request. The full cross-service log "
        "stitch is below, oldest → newest.",
        "",
        f"- **Project:** `{project}`",
        f"- **Trace ID:** `{trace_id}`",
        f"- **Services touched:** {', '.join(f'`{s}`' for s in services)}",
        f"- **Entries:** {len(entries)}",
        f"- **Span:** `{entries[0].get('ts', '?')}` → "
        f"`{entries[-1].get('ts', '?')}`",
        "",
        "## Entries",
        "",
        "```",
        *(_entry_line(e, width_svc=width) for e in entries),
        "```",
        "",
        "## CloudLens MCP hooks",
        "",
        f'- `get_trace("{trace_id}")` — span tree with per-span timing '
        "(requires the service to be OTel-instrumented; falls back to "
        '`{"found": false}` otherwise)',
    ]
    for svc in services:
        out.append(
            f'- `diff_windows("{svc}")` — recent regressions in `{svc}`'
        )
    return "\n".join(out)


def format_context_share(
    anchor: dict, entries: list[dict], project: str,
) -> str:
    svc = anchor.get("svc", "?")
    anchor_ts = anchor.get("ts", "?")
    anchor_msg = anchor.get("msg") or ""
    out = [
        f"# CloudLens context share — `{svc}` around `{anchor_ts}`",
        "",
        "Help me debug this. Below are the log entries from this service "
        "before and after the row I'm focused on (marked with ▶).",
        "",
        f"- **Project:** `{project}`",
        f"- **Service:** `{svc}`",
        f"- **Anchor time:** `{anchor_ts}`",
        f"- **Anchor severity:** `{anchor.get('sev', 'DEFAULT')}`",
    ]
    if anchor.get("trace"):
        out.append(f"- **Anchor trace:** `{anchor['trace']}`")
    if anchor.get("http"):
        out.append(f"- **Anchor http:** `{anchor['http']}`")
    out += [
        f"- **Entries in window:** {len(entries)}",
        "",
        "## Entries (oldest → newest)",
        "",
        "```",
        *(
            _entry_line(
                e,
                anchor=(
                    e.get("ts") == anchor_ts
                    and (e.get("msg") or "") == anchor_msg
                ),
            )
            for e in entries
        ),
        "```",
        "",
        "## CloudLens MCP hooks",
        "",
    ]
    if anchor.get("trace"):
        out.append(
            f'- `get_logs_by_trace("{anchor["trace"]}")` — cross-service '
            "stitch for the anchor request"
        )
    if svc and svc != "?":
        out += [
            f'- `get_health("{svc}")` — service health snapshot',
            f'- `diff_windows("{svc}")` — what changed in the window',
            f'- `get_recent_traces("{svc}", failing_only=True)` — recent '
            "failing requests in `" + svc + "`",
        ]
    return "\n".join(out)
