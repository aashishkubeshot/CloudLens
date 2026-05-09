"""Cross-client analyses: health, summarize-with-deploys, diff_windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .logs import LogsClient
from .metrics import MetricsClient
from .runtime import RuntimeClient


# Below this many requests, latency percentiles and rate metrics are too
# noisy to compare meaningfully — diff_revisions suppresses those deltas.
_MIN_RELIABLE_SAMPLE = 30


class Analysis:
    def __init__(self, logs: LogsClient, metrics: MetricsClient, runtime: RuntimeClient):
        self._logs = logs
        self._metrics = metrics
        self._runtime = runtime

    def health(self, service: str, hours: float = 1.0, region: str | None = None) -> dict:
        region = region or self._runtime.find_region(service)
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=hours)

        snap = self._metrics.health_snapshot(
            service, since=since, until=until, region=region
        )
        revs = self._runtime.list_revisions(service, region=region, limit=200)
        active = [r for r in revs if r.get("active")]
        deploys = _deploys_in(revs, since, until)

        return {
            "service": service,
            "region": region,
            **snap,
            "active_revisions": active,
            "deploys_in_window": deploys,
        }

    def summarize_with_deploys(
        self, service: str, hours: float = 1.0, region: str | None = None,
    ) -> dict:
        region = region or self._runtime.find_region(service)
        summary = self._logs.summarize(service=service, hours=hours, region=region)
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=hours)
        revs = self._runtime.list_revisions(service, region=region, limit=200)
        summary["region"] = region
        summary["deploys_in_window"] = _deploys_in(revs, since, until)
        return summary

    def diff_windows(
        self, service: str, hours: float = 1.0, region: str | None = None,
    ) -> dict:
        region = region or self._runtime.find_region(service)
        until_now = datetime.now(timezone.utc)
        since_now = until_now - timedelta(hours=hours)
        until_base = since_now
        since_base = since_now - timedelta(hours=hours)

        now = self._metrics.health_snapshot(
            service, since=since_now, until=until_now, region=region,
        )
        base = self._metrics.health_snapshot(
            service, since=since_base, until=until_base, region=region,
        )

        now_msgs = self._logs.message_counts(
            service=service, severity="ERROR",
            since=since_now, until=until_now, region=region,
        )
        base_msgs = self._logs.message_counts(
            service=service, severity="ERROR",
            since=since_base, until=until_base, region=region,
        )
        new_msgs = [
            {"message": m, "count_now": c, "count_baseline": base_msgs.get(m, 0)}
            for m, c in now_msgs.most_common(20)
            if base_msgs.get(m, 0) == 0
        ]

        revs = self._runtime.list_revisions(service, region=region, limit=200)
        deploys = _deploys_in(revs, since_now, until_now)

        return {
            "service": service,
            "region": region,
            "window_now": _window(since_now, until_now, hours),
            "window_baseline": _window(since_base, until_base, hours),
            "deltas": {
                "request_count": _delta(now["request_count"], base["request_count"]),
                "error_rate_5xx_pct": _delta(now["error_rate_5xx_pct"], base["error_rate_5xx_pct"]),
                "error_rate_4xx_pct": _delta(now["error_rate_4xx_pct"], base["error_rate_4xx_pct"]),
                "latency_p95_ms": _delta(now["latency_ms"]["p95"], base["latency_ms"]["p95"]),
                "latency_p99_ms": _delta(now["latency_ms"]["p99"], base["latency_ms"]["p99"]),
                "cold_starts": _delta(now["cold_starts"], base["cold_starts"]),
            },
            "new_error_messages": new_msgs,
            "deploys_in_now_window": deploys,
        }


    def diff_revisions(
        self,
        service: str,
        rev_a: str,
        rev_b: str,
        hours: float = 24.0,
        region: str | None = None,
    ) -> dict:
        region = region or self._runtime.find_region(service)
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=hours)

        a_snap = self._metrics.health_snapshot(
            service, since=since, until=until, region=region, revision=rev_a,
        )
        b_snap = self._metrics.health_snapshot(
            service, since=since, until=until, region=region, revision=rev_b,
        )
        a_msgs = self._logs.message_counts(
            service=service, severity="ERROR", hours=hours,
            region=region, revision=rev_a,
        )
        b_msgs = self._logs.message_counts(
            service=service, severity="ERROR", hours=hours,
            region=region, revision=rev_b,
        )
        new_in_b = [
            {"message": m, "count_b": c, "count_a": a_msgs.get(m, 0)}
            for m, c in b_msgs.most_common(20)
            if a_msgs.get(m, 0) == 0
        ]

        a_count = a_snap["request_count"] or 0
        b_count = b_snap["request_count"] or 0
        low_sample = min(a_count, b_count) < _MIN_RELIABLE_SAMPLE

        def _guarded(d: dict) -> dict:
            if not low_sample:
                return d
            return {**d, "delta_pct": None, "low_sample": True}

        result = {
            "service": service,
            "region": region,
            "hours": hours,
            "rev_a": {"revision": rev_a, **_revision_summary(a_snap)},
            "rev_b": {"revision": rev_b, **_revision_summary(b_snap)},
            "deltas": {
                "request_count": _delta(b_count, a_count),
                "error_rate_5xx_pct": _guarded(_delta(b_snap["error_rate_5xx_pct"], a_snap["error_rate_5xx_pct"])),
                "latency_p95_ms": _guarded(_delta(b_snap["latency_ms"]["p95"], a_snap["latency_ms"]["p95"])),
                "latency_p99_ms": _guarded(_delta(b_snap["latency_ms"]["p99"], a_snap["latency_ms"]["p99"])),
                "cold_starts": _delta(b_snap["cold_starts"], a_snap["cold_starts"]),
            },
            "new_errors_in_b": new_in_b,
        }
        if low_sample:
            result["sample_warning"] = (
                f"Low sample: a={a_count}, b={b_count} requests. "
                f"Rate and percentile deltas suppressed below "
                f"{_MIN_RELIABLE_SAMPLE}-request threshold; raw values still shown."
            )
        return result

    def find_stalled_tasks(
        self,
        service: str,
        group_by: str,
        hours: float = 2.0,
        idle_minutes: float = 10.0,
        min_entries: int = 2,
        sample_cap: int = 5000,
        region: str | None = None,
    ) -> list[dict]:
        region = region or self._runtime.find_region(service)
        stalled = self._logs.find_stalled_tasks(
            service=service, group_by=group_by,
            hours=hours, idle_minutes=idle_minutes,
            min_entries=min_entries, region=region,
            sample_cap=sample_cap,
        )
        if not stalled:
            return []
        revs = self._runtime.list_revisions(service, region=region, limit=200)
        deploy_times = [
            (r["revision"], datetime.fromisoformat(r["deployed_at"].replace("Z", "+00:00")))
            for r in revs if r["deployed_at"]
        ]
        for s in stalled:
            last_seen = datetime.fromisoformat(s["last_seen"].replace("Z", "+00:00"))
            for rev_name, deployed_at in deploy_times:
                gap = (deployed_at - last_seen).total_seconds()
                if 0 <= gap <= 60:
                    s["likely_killed_by_deploy"] = {
                        "revision": rev_name,
                        "deployed_at": deployed_at.isoformat(),
                        "gap_seconds": int(gap),
                    }
                    break
        return stalled


def _revision_summary(snap: dict) -> dict:
    return {
        "request_count": snap["request_count"],
        "error_rate_5xx_pct": snap["error_rate_5xx_pct"],
        "error_rate_4xx_pct": snap["error_rate_4xx_pct"],
        "latency_ms": snap["latency_ms"],
        "cold_starts": snap["cold_starts"],
    }


def _deploys_in(
    revisions: list[dict], since: datetime, until: datetime,
) -> list[dict]:
    out: list[dict] = []
    for r in revisions:
        if not r["deployed_at"]:
            continue
        ts = datetime.fromisoformat(r["deployed_at"].replace("Z", "+00:00"))
        if since <= ts <= until:
            out.append({
                "revision": r["revision"],
                "deployed_at": r["deployed_at"],
                "active": r.get("active", False),
            })
    return out


def _window(since: datetime, until: datetime, hours: float) -> dict:
    return {
        "from": since.isoformat(),
        "to": until.isoformat(),
        "hours": hours,
    }


def _delta(now, base) -> dict:
    if now is None and base is None:
        return {"now": None, "baseline": None, "delta_pct": None}
    if base in (None, 0):
        return {"now": now, "baseline": base, "delta_pct": None}
    return {
        "now": now,
        "baseline": base,
        "delta_pct": round((now - base) / base * 100, 1),
    }
