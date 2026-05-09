"""Cloud Trace queries."""

from __future__ import annotations

from google.api_core import exceptions as gax_exceptions
from google.cloud import trace_v1

from .format import format_span


_API_NAME = "cloudtrace.googleapis.com"
_NOT_FOUND_HINT = (
    "Cloud Run propagates trace IDs into log entries for correlation, but does "
    "not export spans to Cloud Trace unless the service is instrumented "
    "(OpenTelemetry / Cloud Trace exporter). The trace ID is valid in logs "
    "even though no spans exist here — use get_logs_by_trace(trace_id) instead."
)


class TracesClient:
    def __init__(self, project: str):
        self.project = project
        self._client = trace_v1.TraceServiceClient()

    def get_trace(self, trace_id: str) -> dict:
        tid = trace_id.split("/")[-1]
        try:
            trace = self._client.get_trace(project_id=self.project, trace_id=tid)
        except gax_exceptions.PermissionDenied as e:
            return {
                "trace_id": tid,
                "found": False,
                "error": "api_disabled",
                "api": _API_NAME,
                "fix": (
                    f"Enable the Cloud Trace API for project {self.project!r}: "
                    f"https://console.developers.google.com/apis/api/{_API_NAME}/overview?project={self.project}"
                ),
                "spans": [],
            }
        except gax_exceptions.NotFound:
            return {
                "trace_id": tid,
                "found": False,
                "error": "not_found",
                "hint": _NOT_FOUND_HINT,
                "spans": [],
            }
        except gax_exceptions.GoogleAPIError as e:
            return {
                "trace_id": tid,
                "found": False,
                "error": str(e).split("\n")[0],
                "spans": [],
            }

        spans = sorted(
            (format_span(s) for s in trace.spans),
            key=lambda x: x.get("start") or "",
        )

        services: set[str] = set()
        for s in trace.spans:
            for k, v in dict(getattr(s, "labels", {}) or {}).items():
                if k in ("/component", "g.co/agent", "/http/host"):
                    if v:
                        services.add(v)

        total_ms = None
        if spans:
            durations = [s["duration_ms"] for s in spans if s.get("duration_ms") is not None]
            if durations:
                total_ms = max(durations)

        return {
            "trace_id": tid,
            "found": True,
            "span_count": len(spans),
            "duration_ms": total_ms,
            "services": sorted(services),
            "spans": spans,
        }
