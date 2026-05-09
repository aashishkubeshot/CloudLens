"""Cloud Error Reporting queries."""

from __future__ import annotations

from google.api_core import exceptions as gax_exceptions
from google.cloud import errorreporting_v1beta1

from .format import format_error_group, ts_iso


_API_NAME = "clouderrorreporting.googleapis.com"


class ErrorsClient:
    def __init__(self, project: str):
        self.project = project
        self._stats = errorreporting_v1beta1.ErrorStatsServiceClient()

    def list_groups(self, service: str, hours: float = 24.0, limit: int = 20) -> dict:
        req = errorreporting_v1beta1.ListGroupStatsRequest(
            project_name=f"projects/{self.project}",
            time_range=errorreporting_v1beta1.QueryTimeRange(period=_period(hours)),
            service_filter=errorreporting_v1beta1.ServiceContextFilter(service=service),
            page_size=min(limit, 100),
        )
        try:
            groups: list[dict] = []
            for stats in self._stats.list_group_stats(request=req):
                groups.append(format_error_group(stats))
                if len(groups) >= limit:
                    break
            return {"groups": groups, "count": len(groups)}
        except gax_exceptions.PermissionDenied as e:
            return {"groups": [], "count": 0, **_api_disabled_error(self.project, e)}
        except gax_exceptions.GoogleAPIError as e:
            return {"groups": [], "count": 0, **_api_error(e)}

    def get_group(
        self,
        group_id: str,
        hours: float = 24.0,
        samples: int = 3,
        service: str | None = None,
    ) -> dict:
        time_range = errorreporting_v1beta1.QueryTimeRange(period=_period(hours))

        stats_req = errorreporting_v1beta1.ListGroupStatsRequest(
            project_name=f"projects/{self.project}",
            time_range=time_range,
            group_id=[group_id],
            page_size=1,
        )
        try:
            stats = list(self._stats.list_group_stats(request=stats_req))
        except gax_exceptions.PermissionDenied as e:
            return _api_disabled_error(self.project, e)
        except gax_exceptions.GoogleAPIError as e:
            return _api_error(e)
        if not stats:
            return {"group_id": group_id, "found": False}

        out = format_error_group(stats[0])
        out["found"] = True

        events_req = errorreporting_v1beta1.ListEventsRequest(
            project_name=f"projects/{self.project}",
            group_id=group_id,
            time_range=time_range,
            page_size=samples,
        )
        if service:
            events_req.service_filter = errorreporting_v1beta1.ServiceContextFilter(service=service)

        events: list[dict] = []
        try:
            event_iter = self._stats.list_events(request=events_req)
        except gax_exceptions.GoogleAPIError as e:
            out["events_error"] = str(e)
            out["events"] = []
            return out
        for ev in event_iter:
            message = ev.message or ""
            head, _, body = message.partition("\n")
            event: dict = {
                "ts": ts_iso(ev.event_time),
                "message": head[:300],
            }
            if ev.service_context and ev.service_context.service:
                event["service"] = ev.service_context.service
            if body:
                event["stack"] = body[:1500]
            events.append(event)
            if len(events) >= samples:
                break
        out["events"] = events
        return out


def _api_disabled_error(project: str, exc: Exception) -> dict:
    return {
        "error": "api_disabled",
        "api": _API_NAME,
        "project": project,
        "message": str(exc).split("\n")[0],
        "fix": (
            f"Enable the Error Reporting API for project {project!r}: "
            f"https://console.developers.google.com/apis/api/{_API_NAME}/overview?project={project}"
        ),
    }


def _api_error(exc: Exception) -> dict:
    return {"error": "api_error", "message": str(exc).split("\n")[0]}


def _period(hours: float) -> int:
    period = errorreporting_v1beta1.QueryTimeRange.Period
    if hours <= 1:
        return period.PERIOD_1_HOUR
    if hours <= 6:
        return period.PERIOD_6_HOURS
    if hours <= 24:
        return period.PERIOD_1_DAY
    if hours <= 24 * 7:
        return period.PERIOD_1_WEEK
    return period.PERIOD_30_DAYS
