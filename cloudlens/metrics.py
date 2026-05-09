"""Cloud Monitoring queries for Cloud Run."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import monitoring_v3
from google.protobuf.duration_pb2 import Duration


_NAMED_METRICS: dict[str, dict] = {
    "request_count": {
        "type": "run.googleapis.com/request_count",
        "aligner": "ALIGN_DELTA",
        "reducer": "REDUCE_SUM",
    },
    "request_count_5xx": {
        "type": "run.googleapis.com/request_count",
        "aligner": "ALIGN_DELTA",
        "reducer": "REDUCE_SUM",
        "extra": 'metric.label.response_code_class="5xx"',
    },
    "request_count_4xx": {
        "type": "run.googleapis.com/request_count",
        "aligner": "ALIGN_DELTA",
        "reducer": "REDUCE_SUM",
        "extra": 'metric.label.response_code_class="4xx"',
    },
    "latency_p50": {
        "type": "run.googleapis.com/request_latencies",
        "aligner": "ALIGN_PERCENTILE_50",
        "reducer": "REDUCE_MEAN",
    },
    "latency_p95": {
        "type": "run.googleapis.com/request_latencies",
        "aligner": "ALIGN_PERCENTILE_95",
        "reducer": "REDUCE_MEAN",
    },
    "latency_p99": {
        "type": "run.googleapis.com/request_latencies",
        "aligner": "ALIGN_PERCENTILE_99",
        "reducer": "REDUCE_MEAN",
    },
    "instance_count": {
        "type": "run.googleapis.com/container/instance_count",
        "aligner": "ALIGN_MEAN",
        "reducer": "REDUCE_SUM",
    },
    "cold_starts": {
        "type": "run.googleapis.com/container/startup_latencies",
        "aligner": "ALIGN_DELTA",
        "reducer": "REDUCE_SUM",
    },
    "cpu_utilization": {
        "type": "run.googleapis.com/container/cpu/utilizations",
        "aligner": "ALIGN_PERCENTILE_99",
        "reducer": "REDUCE_MEAN",
    },
    "memory_utilization": {
        "type": "run.googleapis.com/container/memory/utilizations",
        "aligner": "ALIGN_PERCENTILE_99",
        "reducer": "REDUCE_MEAN",
    },
}

NAMED_METRIC_NAMES = list(_NAMED_METRICS.keys())

_HEALTH_METRICS = (
    "request_count",
    "request_count_5xx",
    "request_count_4xx",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "instance_count",
    "cold_starts",
)


class MetricsClient:
    def __init__(self, project: str):
        self.project = project
        self._client = monitoring_v3.MetricServiceClient()

    def query(
        self,
        name: str,
        service: str,
        *,
        hours: float = 1.0,
        since: datetime | None = None,
        until: datetime | None = None,
        region: str | None = None,
        revision: str | None = None,
        points: int = 30,
    ) -> list[dict]:
        if name not in _NAMED_METRICS:
            raise ValueError(
                f"unknown metric {name!r}; choose from {NAMED_METRIC_NAMES}"
            )
        spec = _NAMED_METRICS[name]
        start, end = _resolve_window(hours, since, until)
        return self._query(
            metric_type=spec["type"],
            aligner=spec["aligner"],
            reducer=spec["reducer"],
            extra=spec.get("extra"),
            service=service,
            region=region,
            revision=revision,
            start=start,
            end=end,
            points=points,
        )

    def health_snapshot(
        self,
        service: str,
        *,
        hours: float = 1.0,
        since: datetime | None = None,
        until: datetime | None = None,
        region: str | None = None,
        revision: str | None = None,
    ) -> dict:
        start, end = _resolve_window(hours, since, until)
        actual_hours = (end - start).total_seconds() / 3600
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                name: ex.submit(
                    self.query, name, service,
                    since=start, until=end, region=region,
                    revision=revision, points=30,
                )
                for name in _HEALTH_METRICS
            }
            results: dict[str, list[dict]] = {}
            for name, fut in futures.items():
                try:
                    results[name] = fut.result()
                except Exception:
                    results[name] = []
        return _summarize(results, actual_hours)

    def _query(
        self,
        *,
        metric_type: str,
        aligner: str,
        reducer: str,
        service: str,
        region: str | None,
        revision: str | None,
        start: datetime,
        end: datetime,
        points: int,
        extra: str | None = None,
    ) -> list[dict]:
        interval = monitoring_v3.TimeInterval(end_time=end, start_time=start)
        window_seconds = (end - start).total_seconds()
        period_seconds = max(int(window_seconds / max(points, 1)), 60)

        f_parts = [
            f'metric.type="{metric_type}"',
            'resource.type="cloud_run_revision"',
            f'resource.label.service_name="{service}"',
        ]
        if region:
            f_parts.append(f'resource.label.location="{region}"')
        if revision:
            f_parts.append(f'resource.label.revision_name="{revision}"')
        if extra:
            f_parts.append(extra)

        aggregation = monitoring_v3.Aggregation(
            alignment_period=Duration(seconds=period_seconds),
            per_series_aligner=getattr(monitoring_v3.Aggregation.Aligner, aligner),
            cross_series_reducer=getattr(monitoring_v3.Aggregation.Reducer, reducer),
        )

        result = self._client.list_time_series(
            request={
                "name": f"projects/{self.project}",
                "filter": " AND ".join(f_parts),
                "interval": interval,
                "aggregation": aggregation,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )

        out: list[dict] = []
        for ts in result:
            for p in ts.points:
                v = _scalar(p.value)
                if v is None:
                    continue
                out.append({
                    "ts": p.interval.end_time.isoformat() if p.interval.end_time else None,
                    "value": v,
                })
        out.sort(key=lambda x: x["ts"] or "")
        return out


def _resolve_window(
    hours: float | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime, datetime]:
    if until is None:
        until = datetime.now(timezone.utc)
    if since is None:
        if hours is None:
            hours = 1.0
        since = until - timedelta(hours=hours)
    return since, until


def _scalar(value: Any) -> float | int | None:
    pb = value._pb if hasattr(value, "_pb") else value
    which = pb.WhichOneof("value") if hasattr(pb, "WhichOneof") else None
    if which == "double_value":
        return value.double_value
    if which == "int64_value":
        return value.int64_value
    if which == "distribution_value":
        return value.distribution_value.count
    if which == "bool_value":
        return 1 if value.bool_value else 0
    return None


def _summarize(results: dict[str, list[dict]], hours: float) -> dict:
    rc = sum(p["value"] for p in results.get("request_count", []))
    rc_5xx = sum(p["value"] for p in results.get("request_count_5xx", []))
    rc_4xx = sum(p["value"] for p in results.get("request_count_4xx", []))
    cold = sum(p["value"] for p in results.get("cold_starts", []))

    def _mean(name: str) -> float | None:
        pts = results.get(name, [])
        if not pts:
            return None
        return round(sum(p["value"] for p in pts) / len(pts), 2)

    def _max(name: str) -> float | None:
        pts = results.get(name, [])
        if not pts:
            return None
        return round(max(p["value"] for p in pts), 2)

    def _pct(num: float, denom: float) -> float:
        if not denom:
            return 0.0
        return round(num / denom * 100, 2)

    return {
        "hours": round(hours, 3),
        "request_count": int(rc),
        "error_rate_5xx_pct": _pct(rc_5xx, rc),
        "error_rate_4xx_pct": _pct(rc_4xx, rc),
        "latency_ms": {
            "p50": _mean("latency_p50"),
            "p95": _mean("latency_p95"),
            "p99": _mean("latency_p99"),
        },
        "instance_count": {
            "avg": _mean("instance_count"),
            "max": _max("instance_count"),
        },
        "cold_starts": int(cold),
    }
