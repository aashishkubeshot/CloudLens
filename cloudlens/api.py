"""Lazy facade composing the per-API clients."""

from __future__ import annotations

from .analysis import Analysis
from .errors import ErrorsClient
from .logs import LogsClient
from .metrics import MetricsClient
from .runtime import RuntimeClient
from .traces import TracesClient


class Observability:
    def __init__(self, project: str):
        self.project = project
        self._logs: LogsClient | None = None
        self._runtime: RuntimeClient | None = None
        self._metrics: MetricsClient | None = None
        self._traces: TracesClient | None = None
        self._errors: ErrorsClient | None = None
        self._analysis: Analysis | None = None

    @property
    def logs(self) -> LogsClient:
        if self._logs is None:
            self._logs = LogsClient(self.project)
        return self._logs

    @property
    def runtime(self) -> RuntimeClient:
        if self._runtime is None:
            self._runtime = RuntimeClient(self.project)
        return self._runtime

    @property
    def metrics(self) -> MetricsClient:
        if self._metrics is None:
            self._metrics = MetricsClient(self.project)
        return self._metrics

    @property
    def traces(self) -> TracesClient:
        if self._traces is None:
            self._traces = TracesClient(self.project)
        return self._traces

    @property
    def errors(self) -> ErrorsClient:
        if self._errors is None:
            self._errors = ErrorsClient(self.project)
        return self._errors

    @property
    def analysis(self) -> Analysis:
        if self._analysis is None:
            self._analysis = Analysis(
                logs=self.logs,
                metrics=self.metrics,
                runtime=self.runtime,
            )
        return self._analysis
