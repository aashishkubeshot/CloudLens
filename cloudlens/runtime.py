"""Cloud Run service and revision metadata."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.cloud import run_v2


_DEFAULT_REGION = "us-central1"


class RuntimeClient:
    def __init__(self, project: str):
        self.project = project
        self._services_client: run_v2.ServicesClient | None = None
        self._revisions_client: run_v2.RevisionsClient | None = None
        self._region_cache: dict[str, str] = {}
        self._listed_all = False

    def list_services(self, region: str | None = None) -> list[dict]:
        client = self._get_services()
        loc = region or "-"
        parent = f"projects/{self.project}/locations/{loc}"
        out: list[dict] = []
        for s in client.list_services(parent=parent):
            parts = s.name.split("/")
            name = parts[-1]
            svc_region = parts[3] if len(parts) >= 4 else None
            self._region_cache[name] = svc_region or _DEFAULT_REGION
            out.append({
                "name": name,
                "region": svc_region,
                "uri": getattr(s, "uri", None),
                "latest_ready_revision": (
                    s.latest_ready_revision.split("/")[-1]
                    if getattr(s, "latest_ready_revision", None) else None
                ),
            })
        if region is None:
            self._listed_all = True
        return out

    def find_region(self, service: str) -> str:
        if service in self._region_cache:
            return self._region_cache[service]
        if not self._listed_all:
            self.list_services()
        return self._region_cache.get(service, _DEFAULT_REGION)

    def get_service(self, service: str, region: str):
        client = self._get_services()
        name = f"projects/{self.project}/locations/{region}/services/{service}"
        return client.get_service(name=name)

    def list_revisions(
        self,
        service: str,
        region: str | None = None,
        hours: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        region = region or self.find_region(service)
        client = self._get_revisions()
        parent = f"projects/{self.project}/locations/{region}/services/{service}"
        cutoff = None
        if hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        traffic = self._traffic_map(service, region)
        out: list[dict] = []
        for r in client.list_revisions(parent=parent):
            if cutoff and r.create_time and r.create_time < cutoff:
                continue
            rev_name = r.name.split("/")[-1]
            entry: dict = {
                "revision": rev_name,
                "deployed_at": r.create_time.isoformat() if r.create_time else None,
                "traffic_percent": traffic.get(rev_name, 0),
                "active": traffic.get(rev_name, 0) > 0,
            }
            if r.containers:
                entry["image"] = r.containers[0].image
            if getattr(r, "scaling", None):
                if r.scaling.min_instance_count:
                    entry["min_instances"] = r.scaling.min_instance_count
                if r.scaling.max_instance_count:
                    entry["max_instances"] = r.scaling.max_instance_count
            if getattr(r, "max_instance_request_concurrency", None):
                entry["concurrency"] = r.max_instance_request_concurrency
            out.append(entry)
            if len(out) >= limit:
                break
        return sorted(out, key=lambda x: x["deployed_at"] or "", reverse=True)

    def deploys_between(
        self,
        service: str,
        since: datetime,
        until: datetime,
        region: str | None = None,
    ) -> list[dict]:
        revs = self.list_revisions(service, region=region, limit=200)
        out: list[dict] = []
        for r in revs:
            if not r["deployed_at"]:
                continue
            ts = datetime.fromisoformat(r["deployed_at"].replace("Z", "+00:00"))
            if since <= ts <= until:
                out.append(r)
        return out

    def _traffic_map(self, service: str, region: str) -> dict[str, int]:
        try:
            svc = self.get_service(service, region=region)
        except Exception:
            return {}
        latest = None
        if getattr(svc, "latest_ready_revision", None):
            latest = svc.latest_ready_revision.split("/")[-1]
        out: dict[str, int] = {}
        statuses = getattr(svc, "traffic_statuses", None) or getattr(svc, "traffic", []) or []
        for t in statuses:
            rev = getattr(t, "revision", "") or ""
            pct = getattr(t, "percent", 0) or 0
            if not rev and latest:
                rev = latest
            if rev:
                out[rev] = out.get(rev, 0) + pct
        return out

    def _get_services(self) -> run_v2.ServicesClient:
        if self._services_client is None:
            self._services_client = run_v2.ServicesClient()
        return self._services_client

    def _get_revisions(self) -> run_v2.RevisionsClient:
        if self._revisions_client is None:
            self._revisions_client = run_v2.RevisionsClient()
        return self._revisions_client
