"""Composable Cloud Logging filters.

A `Filter` is one immutable, AND-able predicate. The TUI holds a small set
of active filters and ANDs every active filter's `clause()` into the Cloud
Logging query — for the initial window, the live tail, and the lazy-loaded
history pages. Adding a filter doesn't switch modes: the tail keeps
flowing, only with the predicate applied.

**Adding a new filter type — three steps:**

1. Subclass `Filter` here as a frozen dataclass.
2. Set the `key` class attribute (used as the dict identifier so there's
   at most one filter of each kind active).
3. Implement `clause(project)` and `chip(dawn)`.

That's it. Register a key binding in `tui.py` that constructs your new
filter from user input, and the rest (chip rendering in the status bar,
persistence across reloads, lazy-load applying it, dawn/moon color
adaptation) is handled by the framework.

Filters are `@dataclass(frozen=True)` so they're hashable, comparable by
value, and trivially safe to share between threads / pass through workers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


def _escape(s: str) -> str:
    """Escape a string for embedding in a Cloud Logging filter literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


class Filter(ABC):
    """One AND-able predicate against Cloud Logging.

    Subclasses are frozen dataclasses so the App can compare filter
    instances for equality (skip refetch when the user re-submits the
    same value) and use them as dict values without worrying about
    mutation.
    """

    # Subclass-level identifier. The App keys its `_filters` dict by this
    # value so re-applying the same filter type replaces the previous
    # instance rather than stacking. Pick something short and stable —
    # also used in the help screen and on log statements.
    key: str = ""

    @abstractmethod
    def clause(self, project: str) -> Optional[str]:
        """Return the Cloud Logging filter fragment for this predicate,
        or `None` if the filter is in an inactive state (e.g., empty
        text). Inactive filters are dropped from the final query."""

    @abstractmethod
    def chip(self, dawn: bool = False) -> str:
        """Rich-markup chip shown in the status bar. The leading glyph
        should hint at the keybind that toggles this filter so users
        can clear it without reading the help."""


@dataclass(frozen=True)
class TextFilter(Filter):
    """Free-text substring match across all log fields.

    Uses Cloud Logging's bare-quoted-string operator — matches
    `textPayload`, `jsonPayload.*`, and `protoPayload.*` content. This is
    what powers the `/` search.
    """

    key = "text"
    text: str

    def clause(self, project: str) -> Optional[str]:
        if not self.text:
            return None
        return f'"{_escape(self.text)}"'

    def chip(self, dawn: bool = False) -> str:
        return f"[b #9ccfd8]/[/] [#e0def4]{self.text}[/]"


@dataclass(frozen=True)
class EmailFilter(Filter):
    """Restrict to logs authenticated as a specific principal email.

    `protoPayload.authenticationInfo.principalEmail` is set on every
    audit log, and on Cloud Run access logs when the service is
    IAM-protected (Cloud Run "Require authentication" with Google ID
    tokens, or IAP). For app-level auth (Firebase / Auth0 / custom),
    the email lives in app stdout instead — use `TextFilter` for that
    case.
    """

    key = "email"
    email: str

    def clause(self, project: str) -> Optional[str]:
        if not self.email:
            return None
        return (
            f'protoPayload.authenticationInfo.principalEmail='
            f'"{_escape(self.email)}"'
        )

    def chip(self, dawn: bool = False) -> str:
        return f"[b #c4a7e7]e[/] [#e0def4]{self.email}[/]"


@dataclass(frozen=True)
class SeverityFilter(Filter):
    """Minimum severity threshold.

    Cloud Logging severities, ordered: `DEFAULT < DEBUG < INFO < NOTICE
    < WARNING < ERROR < CRITICAL < ALERT < EMERGENCY`. The `>=` operator
    matches the threshold and anything more severe.
    """

    key = "severity"
    min_severity: str

    _VALID = frozenset({
        "DEFAULT", "DEBUG", "INFO", "NOTICE",
        "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY",
    })

    def clause(self, project: str) -> Optional[str]:
        sev = self.min_severity.upper().strip()
        if sev not in self._VALID:
            return None
        return f"severity>={sev}"

    def chip(self, dawn: bool = False) -> str:
        return f"[b #f6c177]≥[/] [#e0def4]{self.min_severity.upper()}[/]"


@dataclass(frozen=True)
class UrlFilter(Filter):
    """Substring match on `httpRequest.requestUrl`.

    Useful for isolating one endpoint, or one query-tagged session if
    you append a marker to your URLs.
    """

    key = "url"
    substring: str

    def clause(self, project: str) -> Optional[str]:
        if not self.substring:
            return None
        return f'httpRequest.requestUrl:"{_escape(self.substring)}"'

    def chip(self, dawn: bool = False) -> str:
        return f"[b #ea9a97]u[/] [#e0def4]{self.substring}[/]"


@dataclass(frozen=True)
class IpFilter(Filter):
    """Filter to requests from a specific client IP.

    The TUI auto-detects your public IP at startup (via ipify) and
    pre-fills the prompt — pressing `i` then `enter` filters to your
    own browser session.

    Caveat: lands on Cloud Run **request logs** only (the 10% of rows
    where `httpRequest` is set). The corresponding stdout/stderr
    lines from your app code don't carry a remote IP — pivot via `t`
    (trace) to bridge them in.
    """

    key = "ip"
    ip: str

    def clause(self, project: str) -> Optional[str]:
        if not self.ip:
            return None
        return f'httpRequest.remoteIp="{_escape(self.ip)}"'

    def chip(self, dawn: bool = False) -> str:
        return f"[b #3e8fb0]i[/] [#e0def4]{self.ip}[/]"


# Registry of filter classes by key — used by the TUI to render help
# text and (later) by an `--filter key=value` CLI flag. New filter
# subclasses register themselves here automatically via __init_subclass__
# below.
_REGISTRY: dict[str, type[Filter]] = {}


def __init_subclass_hook(cls: type[Filter], **kwargs) -> None:
    if cls.key:
        _REGISTRY[cls.key] = cls


# Apply the registration hook to existing subclasses + future ones.
for _cls in (TextFilter, EmailFilter, SeverityFilter, UrlFilter, IpFilter):
    __init_subclass_hook(_cls)


def filter_classes() -> dict[str, type[Filter]]:
    """Read-only view of registered filter classes, keyed by `Filter.key`."""
    return dict(_REGISTRY)
