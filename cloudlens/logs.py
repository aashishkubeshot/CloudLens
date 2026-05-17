"""Cloud Logging client for Cloud Run."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from google.cloud import logging_v2

from .format import extract_message, http_cluster_key, summarize_http


_SEVERITIES = {
    "DEFAULT", "DEBUG", "INFO", "NOTICE",
    "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY",
}
_ERROR_SEVS = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}

# Universal Cloud Run log noise dropped when exclude_noise=True.
# Audit logs come from the Run admin API. The sidecar label is set on logs
# emitted by Cloud Run-managed sidecars (GCSFuse, Cloud SQL Proxy, OTel).
_NOISE_EXCLUDES = (
    'NOT logName:"cloudaudit.googleapis.com"',
    'NOT labels."run.googleapis.com/sidecar":*',
)


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _http_status_int(http: Any) -> int | None:
    if not http:
        return None
    try:
        raw = http.get("status")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _payload_field(e: Any, key: str) -> str | None:
    if key in ("trace", "trace_id"):
        if e.trace:
            return e.trace.split("/")[-1]
        return None
    if key in ("span", "span_id"):
        return getattr(e, "span_id", None) or None
    payload = getattr(e, "payload", None)
    if isinstance(payload, dict):
        v = payload.get(key)
        if v not in (None, ""):
            return str(v)
    labels = getattr(e, "labels", None)
    if labels:
        try:
            v = labels.get(key)
        except AttributeError:
            v = None
        if v:
            return str(v)
    msg = extract_message(e)
    if msg:
        m = _text_field_pattern(key).search(msg)
        if m:
            return m.group(1)
    return None


_TEXT_FIELD_CACHE: dict[str, "re.Pattern"] = {}


def _text_field_pattern(key: str) -> "re.Pattern":
    if key not in _TEXT_FIELD_CACHE:
        _TEXT_FIELD_CACHE[key] = re.compile(
            rf'\b{re.escape(key)}\s*[=:]\s*["\']?([\w-]+)'
        )
    return _TEXT_FIELD_CACHE[key]


def _key_extractor(group_by: str) -> Callable[[Any], str | None]:
    """Build a key function for grouping log entries.

    `regex:<pattern>` runs the pattern against the entry's message and uses the
    last non-empty capturing group (or the full match if the pattern has no
    groups). Anything else is treated as a structured-field name.
    """
    if group_by.startswith("regex:"):
        compiled = re.compile(group_by[len("regex:"):])
        def extract(e: Any) -> str | None:
            msg = extract_message(e)
            if not msg:
                return None
            m = compiled.search(msg)
            if not m:
                return None
            if compiled.groups:
                last = next((g for g in reversed(m.groups()) if g), None)
                return str(last) if last is not None else None
            return m.group(0)
        return extract
    return lambda e: _payload_field(e, group_by)


def build_filter(
    project: str,
    *,
    service: str | None = None,
    services: list[str] | None = None,
    revision: str | None = None,
    region: str | None = None,
    hours: float | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    severity: str | None = None,
    text: str | None = None,
    trace_id: str | None = None,
    extras: list[str] | None = None,
    exclude_noise: bool = False,
) -> str:
    parts = ['resource.type="cloud_run_revision"']
    if service:
        parts.append(f'resource.labels.service_name="{_escape(service)}"')
    elif services:
        clauses = [f'resource.labels.service_name="{_escape(s)}"' for s in services if s]
        if clauses:
            parts.append(f"({' OR '.join(clauses)})")
    if revision:
        parts.append(f'resource.labels.revision_name="{_escape(revision)}"')
    if region:
        parts.append(f'resource.labels.location="{_escape(region)}"')
    if since is None and hours is not None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    if since is not None:
        parts.append(f'timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    if until is not None:
        parts.append(f'timestamp<"{until.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    if severity:
        sev = severity.upper()
        if sev not in _SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        parts.append(f"severity>={sev}")
    if text:
        parts.append(f'"{_escape(text)}"')
    if trace_id:
        tid = trace_id.split("/")[-1]
        parts.append(f'trace="projects/{project}/traces/{tid}"')
    if extras:
        # Free-form fragments contributed by the TUI's filter framework
        # (filters.py). Each one is already a complete Cloud Logging
        # predicate; we just AND it into the query.
        parts.extend(c for c in extras if c)
    if exclude_noise:
        parts.extend(_NOISE_EXCLUDES)
    return " AND ".join(parts)


class LogsClient:
    def __init__(self, project: str):
        self.project = project
        self._client = logging_v2.Client(project=project)

    def list_entries(self, filter_: str, limit: int, ascending: bool = False) -> list[Any]:
        order = logging_v2.ASCENDING if ascending else logging_v2.DESCENDING
        return list(
            self._client.list_entries(
                filter_=filter_,
                order_by=order,
                max_results=limit,
            )
        )

    def get_logs(
        self,
        *,
        service: str,
        hours: float = 1.0,
        severity: str | None = None,
        text: str | None = None,
        limit: int = 100,
        region: str | None = None,
        revision: str | None = None,
        exclude_noise: bool = True,
    ) -> list[Any]:
        f = build_filter(
            self.project, service=service, region=region, revision=revision,
            hours=hours, severity=severity, text=text,
            exclude_noise=exclude_noise,
        )
        return self.list_entries(f, limit=limit)

    def search(
        self,
        *,
        text: str,
        services: list[str] | None = None,
        hours: float = 1.0,
        severity: str | None = None,
        limit: int = 100,
        regex: bool = False,
        exclude_noise: bool = True,
    ) -> list[Any]:
        if regex:
            base = build_filter(
                self.project, services=services, hours=hours,
                severity=severity, exclude_noise=exclude_noise,
            )
            pat = _escape(text)
            f = (
                f'{base} AND '
                f'(textPayload=~"{pat}" OR jsonPayload.message=~"{pat}")'
            )
            return self.list_entries(f, limit=limit)
        f = build_filter(
            self.project, services=services, hours=hours,
            severity=severity, text=text, exclude_noise=exclude_noise,
        )
        return self.list_entries(f, limit=limit)

    def get_logs_by_trace(self, trace_id: str, hours: float = 24.0, limit: int = 500) -> list[Any]:
        f = build_filter(self.project, hours=hours, trace_id=trace_id)
        return self.list_entries(f, limit=limit, ascending=True)

    def tail_logs(
        self,
        *,
        service: str,
        since_iso: str | None = None,
        limit: int = 200,
        region: str | None = None,
        exclude_noise: bool = True,
    ) -> list[Any]:
        if since_iso:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            f = build_filter(
                self.project, service=service, region=region,
                since=since, exclude_noise=exclude_noise,
            )
        else:
            f = build_filter(
                self.project, service=service, region=region,
                hours=5 / 60, exclude_noise=exclude_noise,
            )
        return self.list_entries(f, limit=limit)

    def get_failing(
        self,
        *,
        service: str,
        hours: float = 1.0,
        limit: int = 50,
        region: str | None = None,
        revision: str | None = None,
        include_5xx: bool = True,
        exclude_noise: bool = True,
    ) -> list[Any]:
        if not include_5xx:
            return self.get_logs(
                service=service, hours=hours, severity="ERROR",
                limit=limit, region=region, revision=revision,
                exclude_noise=exclude_noise,
            )
        base = build_filter(
            self.project, service=service, region=region, revision=revision,
            hours=hours, exclude_noise=exclude_noise,
        )
        f = f"{base} AND (severity>=ERROR OR httpRequest.status>=500)"
        return self.list_entries(f, limit=limit)

    def count_logs(
        self,
        *,
        service: str,
        hours: float = 1.0,
        severity: str | None = None,
        text: str | None = None,
        region: str | None = None,
        cap: int = 1000,
        exclude_noise: bool = True,
    ) -> int:
        f = build_filter(
            self.project, service=service, region=region,
            hours=hours, severity=severity, text=text,
            exclude_noise=exclude_noise,
        )
        return len(self.list_entries(f, limit=cap))

    def get_recent_traces(
        self,
        *,
        service: str,
        hours: float = 1.0,
        min_severity: str | None = None,
        limit: int = 20,
        region: str | None = None,
        failing_only: bool = False,
        exclude_noise: bool = True,
    ) -> list[dict]:
        sample = max(limit * 20, 200)
        if failing_only:
            entries = self.get_failing(
                service=service, hours=hours, region=region,
                limit=sample, include_5xx=True, exclude_noise=exclude_noise,
            )
        else:
            f = build_filter(
                self.project, service=service, region=region,
                hours=hours, severity=min_severity,
                exclude_noise=exclude_noise,
            )
            entries = self.list_entries(f, limit=sample)
        traces: dict[str, dict] = {}
        for e in entries:
            tid = (e.trace or "").split("/")[-1]
            if not tid:
                continue
            t = traces.get(tid)
            if t is None:
                t = traces[tid] = {
                    "trace_id": tid,
                    "ts": e.timestamp.isoformat() if e.timestamp else None,
                    "http": summarize_http(getattr(e, "http_request", None)),
                    "entries": 0,
                    "errors": 0,
                    "status_5xx": 0,
                }
            t["entries"] += 1
            if (e.severity or "").upper() in _ERROR_SEVS:
                t["errors"] += 1
            status = _http_status_int(getattr(e, "http_request", None))
            if status is not None and status >= 500:
                t["status_5xx"] += 1
            if not t["http"]:
                http = summarize_http(getattr(e, "http_request", None))
                if http:
                    t["http"] = http
        values = list(traces.values())
        return sorted(values, key=lambda x: x["ts"] or "", reverse=True)[:limit]

    def summarize(
        self,
        *,
        service: str,
        hours: float = 1.0,
        region: str | None = None,
        revision: str | None = None,
        sample_cap: int = 1000,
        error_cap: int = 200,
        exclude_noise: bool = True,
    ) -> dict:
        f = build_filter(
            self.project, service=service, region=region, revision=revision,
            hours=hours, exclude_noise=exclude_noise,
        )
        entries = self.list_entries(f, limit=sample_cap)
        sev_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        traces: set[str] = set()
        for e in entries:
            sev_counts[(e.severity or "DEFAULT").upper()] += 1
            status_int = _http_status_int(getattr(e, "http_request", None))
            if status_int is not None:
                status_counts[str(status_int)] += 1
            if e.trace:
                traces.add(e.trace.split("/")[-1])

        failing = self.get_failing(
            service=service, hours=hours, region=region, revision=revision,
            limit=error_cap, include_5xx=True, exclude_noise=exclude_noise,
        )
        error_msgs: Counter[str] = Counter()
        requests_5xx = 0
        for e in failing:
            http = getattr(e, "http_request", None)
            status_int = _http_status_int(http)
            is_5xx = status_int is not None and status_int >= 500
            if is_5xx:
                requests_5xx += 1
                key = http_cluster_key(http, status=status_int) or f"HTTP {status_int}"
                error_msgs[f"5xx: {key}"[:200]] += 1
            else:
                msg = (extract_message(e) or "")[:200]
                if msg:
                    error_msgs[msg] += 1

        return {
            "service": service,
            "hours": hours,
            "sampled": len(entries),
            "sample_cap": sample_cap,
            "by_severity": dict(sev_counts),
            "by_http_status": dict(status_counts),
            "requests_5xx": requests_5xx,
            "failing_sampled": len(failing),
            "failing_cap": error_cap,
            "distinct_traces": len(traces),
            "top_errors": [
                {"count": c, "message": m} for m, c in error_msgs.most_common(10)
            ],
        }

    def message_counts(
        self,
        *,
        service: str,
        severity: str = "ERROR",
        hours: float | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        region: str | None = None,
        revision: str | None = None,
        sample_cap: int = 500,
        exclude_noise: bool = True,
    ) -> Counter[str]:
        f = build_filter(
            self.project, service=service, region=region, revision=revision,
            hours=hours, since=since, until=until, severity=severity,
            exclude_noise=exclude_noise,
        )
        entries = self.list_entries(f, limit=sample_cap)
        c: Counter[str] = Counter()
        for e in entries:
            msg = (extract_message(e) or "")[:200]
            if msg:
                c[msg] += 1
        return c

    def find_stalled_tasks(
        self,
        *,
        service: str,
        group_by: str,
        hours: float = 2.0,
        idle_minutes: float = 10.0,
        min_entries: int = 2,
        region: str | None = None,
        sample_cap: int = 5000,
        exclude_noise: bool = True,
    ) -> list[dict]:
        f = build_filter(
            self.project, service=service, region=region,
            hours=hours, exclude_noise=exclude_noise,
        )
        entries = self.list_entries(f, limit=sample_cap)
        extractor = _key_extractor(group_by)
        groups: dict[str, dict] = {}
        for e in entries:
            key = extractor(e)
            ts = e.timestamp
            if not key or ts is None:
                continue
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "id": key,
                    "first_seen": ts,
                    "last_seen": ts,
                    "entry_count": 0,
                    "last_message": None,
                }
            if ts < g["first_seen"]:
                g["first_seen"] = ts
            if ts > g["last_seen"]:
                g["last_seen"] = ts
                g["last_message"] = (extract_message(e) or "")[:200] or None
            g["entry_count"] += 1

        now = datetime.now(timezone.utc)
        threshold = timedelta(minutes=idle_minutes)
        out_key = "match" if group_by.startswith("regex:") else group_by
        stalled: list[dict] = []
        for g in groups.values():
            if g["entry_count"] < min_entries:
                continue
            idle = now - g["last_seen"]
            if idle < threshold:
                continue
            duration = g["last_seen"] - g["first_seen"]
            if duration < threshold:
                continue
            stalled.append({
                out_key: g["id"],
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
                "idle_seconds": int(idle.total_seconds()),
                "duration_seconds": int(duration.total_seconds()),
                "entry_count": g["entry_count"],
                "last_message": g["last_message"],
            })
        stalled.sort(key=lambda x: x["last_seen"], reverse=True)
        return stalled
