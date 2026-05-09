"""Compact shaping for log entries, spans, error groups, and timestamps."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


_NOISE_PAYLOAD_KEYS = {
    "logging.googleapis.com/trace",
    "logging.googleapis.com/spanId",
    "logging.googleapis.com/trace_sampled",
    "logging.googleapis.com/sourceLocation",
    "logging.googleapis.com/labels",
}


def format_entries(entries: list[Any]) -> list[dict]:
    return [format_entry(e) for e in entries]


def format_entry(e: Any) -> dict:
    out: dict = {
        "ts": ts_iso(e.timestamp),
        "sev": (e.severity or "DEFAULT").upper(),
    }
    msg = extract_message(e)
    if msg:
        out["msg"] = msg
    if e.trace:
        out["trace"] = e.trace.split("/")[-1]
    if getattr(e, "span_id", None):
        out["span"] = e.span_id
    http = summarize_http(getattr(e, "http_request", None))
    if http:
        out["http"] = http
    if getattr(e, "resource", None) and getattr(e.resource, "labels", None):
        svc = e.resource.labels.get("service_name")
        if svc:
            out["svc"] = svc
        rev = e.resource.labels.get("revision_name")
        if rev:
            out["rev"] = rev
    return out


def extract_message(e: Any) -> str | None:
    payload = getattr(e, "payload", None)
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        for key in ("message", "msg", "event", "log", "error"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        clean = {k: v for k, v in payload.items() if k not in _NOISE_PAYLOAD_KEYS}
        if not clean:
            return None
        try:
            return json.dumps(clean, default=str, separators=(",", ":"))[:600]
        except Exception:
            return str(clean)[:600]
    return str(payload)[:600]


def http_cluster_key(http: Any, status: int | None = None) -> str | None:
    if not http:
        return None
    try:
        get = http.get
    except AttributeError:
        return None
    method = get("requestMethod") or get("request_method") or ""
    url = get("requestUrl") or get("request_url") or ""
    path = ""
    if url:
        try:
            path = urlparse(str(url)).path or "/"
        except Exception:
            path = str(url)
    if status is None:
        status = get("status")
    parts = [str(status)] if status else []
    if method:
        parts.append(str(method))
    if path:
        parts.append(path)
    return " ".join(parts) if parts else None


def summarize_http(http: Any) -> str | None:
    if not http:
        return None
    try:
        get = http.get
    except AttributeError:
        return None
    parts: list[str] = []
    method = get("requestMethod") or get("request_method")
    if method:
        parts.append(str(method))
    url = get("requestUrl") or get("request_url")
    if url:
        try:
            p = urlparse(str(url))
            short = p.path or "/"
            if p.query:
                short += "?" + p.query
            parts.append(short)
        except Exception:
            parts.append(str(url))
    status = get("status")
    if status:
        parts.append(str(status))
    latency = get("latency")
    if latency:
        parts.append(str(latency))
    return " ".join(parts) if parts else None


def format_span(span: Any) -> dict:
    start = span.start_time
    end = span.end_time
    dur_ms = None
    if start and end:
        dur_ms = round((end - start).total_seconds() * 1000, 2)
    out: dict = {
        "span_id": span.span_id,
        "name": span.name,
        "start": ts_iso(start),
        "duration_ms": dur_ms,
    }
    parent = getattr(span, "parent_span_id", None)
    if parent:
        out["parent"] = parent
    labels = getattr(span, "labels", None)
    if labels:
        clean = {
            k: v for k, v in dict(labels).items()
            if not k.startswith("/g.co/r/") and not k.startswith("g.co/r/")
        }
        if clean:
            out["labels"] = clean
    return out


def format_error_group(stats: Any) -> dict:
    out: dict = {
        "group_id": stats.group.group_id,
        "count": stats.count,
        "affected_users": stats.affected_users_count,
    }
    first = ts_iso(stats.first_seen_time)
    if first:
        out["first_seen"] = first
    last = ts_iso(stats.last_seen_time)
    if last:
        out["last_seen"] = last
    services = [s.service for s in (stats.affected_services or []) if s.service]
    if services:
        out["services"] = services
    if stats.representative and stats.representative.message:
        out["sample"] = stats.representative.message.split("\n")[0][:300]
    return out


def ts_iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        try:
            return ts.isoformat()
        except Exception:
            pass
    if hasattr(ts, "ToDatetime"):
        try:
            return ts.ToDatetime().isoformat() + "Z"
        except Exception:
            return None
    if hasattr(ts, "seconds"):
        if not ts.seconds and not getattr(ts, "nanos", 0):
            return None
    return None
