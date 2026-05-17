"""CloudLens TUI — interactive terminal viewer for Cloud Run logs.

Modes:
  LIVE     — polling tail across the active service selection + filters
  PAUSED   — polling stopped; buffer frozen

Filters (composable, all applied to the live tail — see filters.py):
  /  text substring        e  principalEmail (audit/IAM)
  S  minimum severity      u  URL substring
  x  clear all             s  pick services (separate from filters)

There is no "search mode" — text search is a TextFilter, and like every
other filter it applies to the live tail. Adding/removing a filter
refetches the initial window and keeps polling.

Visual language: Rosé Pine Moon (dark) / Rosé Pine Dawn (light), with
severity encoded as a colored letter (E rose, W gold, I foam, D muted)
and a stable per-service hue rotated from love/gold/foam/iris/pine/rose.
The chrome (header, status bar, footer) is custom-rendered to match the
Pencil mockups — Textual's default Header/Footer wouldn't carry the
brand.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Input, Label, Rule, SelectionList, Static,
)
from textual.widgets.selection_list import Selection

from .api import Observability
from .filters import (
    EmailFilter, Filter, IpFilter, SeverityFilter, TextFilter, UrlFilter,
)
from .format import format_entries
from .logs import build_filter
from .share import format_context_share, format_entry_share, format_trace_share
from .themes import CLOUDLENS_DAWN, CLOUDLENS_MOON, DEFAULT_THEME, THEMES


# Rosé Pine palettes — duplicated here (rather than read from Theme) so row
# rendering doesn't pay a per-cell lookup. The Moon set runs on dark
# terminals; Dawn deepens each hue so it stays legible on parchment.
_PALETTE_MOON = (
    "#eb6f92",  # love
    "#f6c177",  # gold
    "#9ccfd8",  # foam
    "#c4a7e7",  # iris
    "#3e8fb0",  # pine
    "#ea9a97",  # rose
)
_PALETTE_DAWN = (
    "#b4637a",
    "#ea9d34",
    "#56949f",
    "#907aa9",
    "#286983",
    "#d7827e",
)

_SEV_COLOR_MOON = {
    "ERROR": "#eb6f92", "CRITICAL": "#eb6f92",
    "ALERT": "#eb6f92", "EMERGENCY": "#eb6f92",
    "WARNING": "#f6c177",
    "NOTICE": "#9ccfd8", "INFO": "#9ccfd8",
    "DEBUG": "#6e6a86", "DEFAULT": "#6e6a86",
}
_SEV_COLOR_DAWN = {
    "ERROR": "#b4637a", "CRITICAL": "#b4637a",
    "ALERT": "#b4637a", "EMERGENCY": "#b4637a",
    "WARNING": "#ea9d34",
    "NOTICE": "#56949f", "INFO": "#56949f",
    "DEBUG": "#9893a5", "DEFAULT": "#9893a5",
}

# Glyph hierarchy: triangle for WARN reads as "attention" without competing
# with the rose error dot; everything else collapses to a filled dot whose
# color carries the meaning. Used in DetailScreen's big headline.
_SEV_GLYPH = {"WARNING": "▲"}

# In the table SEV column we show a single bold letter instead of a dot — the
# color still carries urgency, and the letter remains legible if a colorblind
# user (or screenshot) needs it. EMERGENCY collides with ERROR on the first
# letter, but both render in rose so the read is consistent.
_SEV_LETTER = {
    "ERROR": "E", "CRITICAL": "C",
    "ALERT": "A", "EMERGENCY": "E",
    "WARNING": "W",
    "NOTICE": "N", "INFO": "I",
    "DEBUG": "D", "DEFAULT": "·",
}

_TRACE_DOT_MOON, _TRACE_DOT_DAWN = "#c4a7e7", "#907aa9"
_DIM_MOON, _DIM_DAWN = "#56526e", "#9893a5"
_MUTED_MOON, _MUTED_DAWN = "#908caa", "#797593"
_TEXT_MOON, _TEXT_DAWN = "#e0def4", "#575279"

# Selection marker: rose vertical bar on the focused row. Pre-built as Text
# objects so we don't reallocate per cursor move.
_BLANK_CELL = Text("")
_MARKER_MOON = Text("▌", style="bold #eb6f92")
_MARKER_DAWN = Text("▌", style="bold #b4637a")


def _marker(dawn: bool) -> Text:
    return _MARKER_DAWN if dawn else _MARKER_MOON


# "← OPENED FROM" tag on the anchor row of context/trace modals — the row the
# user opened the modal from. Pre-built for both themes.
_TAG_OPENED_MOON = Text("← OPENED FROM", style="bold #eb6f92")
_TAG_OPENED_DAWN = Text("← OPENED FROM", style="bold #b4637a")


def _opened_tag(dawn: bool) -> Text:
    return _TAG_OPENED_DAWN if dawn else _TAG_OPENED_MOON

_INITIAL_LIMIT = 100        # small first pull → instant
_TAIL_LIMIT = 200
_TAIL_INTERVAL = 2.0
_BUFFER_CAP = 4000
_BUFFER_TRIM = 3000
_HISTORY_PAGE = 200         # rows pulled per lazy-load when scrolling up
# When the cursor sits within this many rows of the top, kick off another
# history fetch so the user rarely sees the wall.
_HISTORY_PREFETCH_ZONE = 5


class Mode(Enum):
    """Polling state. Filters are orthogonal to mode — adding/removing
    filters never changes polling behavior; only `space` (pause/resume)
    does."""
    LIVE = "live"
    PAUSED = "paused"


def _is_dawn(theme_name: str) -> bool:
    return theme_name == CLOUDLENS_DAWN.name


def _color_for_service(svc: str, dawn: bool = False) -> str:
    if not svc:
        return _TEXT_DAWN if dawn else _TEXT_MOON
    palette = _PALETTE_DAWN if dawn else _PALETTE_MOON
    h = int(hashlib.md5(svc.encode()).hexdigest()[:8], 16)
    return palette[h % len(palette)]


def _color_for_instance(inst: str, dawn: bool = False) -> str:
    """Hash-based hue per Cloud Run instance ID. Context views show only one
    service, so reusing the service palette is unambiguous and makes
    instance churn pop visually — when the ID changes, the color flicks."""
    if not inst:
        return _DIM_DAWN if dawn else _DIM_MOON
    palette = _PALETTE_DAWN if dawn else _PALETTE_MOON
    h = int(hashlib.md5(inst.encode()).hexdigest()[:8], 16)
    return palette[h % len(palette)]


def _inst_cell(inst: Optional[str], dawn: bool = False) -> Text:
    """Last 8 chars of the instance ID, colored by hash. Cloud Run instance
    IDs are 32+ hex chars; the suffix is enough to spot churn at a glance."""
    if not inst:
        return Text("·", style=_DIM_DAWN if dawn else _DIM_MOON)
    short = inst[-8:] if len(inst) > 8 else inst
    return Text(short, style=_color_for_instance(inst, dawn))


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _fmt_time(ts: Optional[str]) -> str:
    """Format an ISO timestamp as 'MMM DD HH:MM:SS' (15 chars), e.g.
    'May 05 13:09:12'. Year is dropped — Cloud Run logs are always recent and
    a four-digit year in every row burns column width."""
    if not ts or len(ts) < 19:
        return ts or ""
    try:
        month = _MONTHS[int(ts[5:7]) - 1]
    except (ValueError, IndexError):
        return ts
    return f"{month} {ts[8:10]} {ts[11:19]}"


def _trunc(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _row(e: dict, dawn: bool = False) -> tuple[Text, ...]:
    sev = e.get("sev", "DEFAULT")
    svc = e.get("svc", "")
    msg = e.get("msg") or e.get("http") or ""
    sev_table = _SEV_COLOR_DAWN if dawn else _SEV_COLOR_MOON
    trace_color = _TRACE_DOT_DAWN if dawn else _TRACE_DOT_MOON
    dim = _DIM_DAWN if dawn else _DIM_MOON
    text_color = _TEXT_DAWN if dawn else _TEXT_MOON
    sev_letter = _SEV_LETTER.get(sev, "·")
    sev_style = f"bold {sev_table.get(sev, dim)}"
    trace = (
        Text("●", style=trace_color)
        if e.get("trace")
        else Text("·", style=dim)
    )
    return (
        _BLANK_CELL,  # marker column — filled in by RowHighlighted handler
        Text(_fmt_time(e.get("ts")), style=dim),
        Text(sev_letter, style=sev_style),
        trace,
        Text(svc, style=_color_for_service(svc, dawn)),
        Text(_trunc(msg, 220), style=text_color),
    )


def _row_focused(e: dict, dawn: bool = False) -> tuple[Text, ...]:
    """Row for service-focused views — drops the svc column, adds INST so
    instance churn is visible row-to-row."""
    sev = e.get("sev", "DEFAULT")
    msg = e.get("msg") or e.get("http") or ""
    sev_table = _SEV_COLOR_DAWN if dawn else _SEV_COLOR_MOON
    trace_color = _TRACE_DOT_DAWN if dawn else _TRACE_DOT_MOON
    dim = _DIM_DAWN if dawn else _DIM_MOON
    text_color = _TEXT_DAWN if dawn else _TEXT_MOON
    sev_letter = _SEV_LETTER.get(sev, "·")
    sev_style = f"bold {sev_table.get(sev, dim)}"
    trace = (
        Text("●", style=trace_color)
        if e.get("trace")
        else Text("·", style=dim)
    )
    return (
        _BLANK_CELL,
        Text(_fmt_time(e.get("ts")), style=dim),
        Text(sev_letter, style=sev_style),
        trace,
        _inst_cell(e.get("inst"), dawn),
        Text(_trunc(msg, 260), style=text_color),
    )


def _apply_marker(
    table: "DataTable",
    prev_row: Optional[int],
    new_row: Optional[int],
    dawn: bool,
) -> Optional[int]:
    """Move the rose ▌ marker from `prev_row` to `new_row`. Returns the row
    that ended up holding the marker (or None). Tolerates stale indices
    that survived a clear/reflow.
    """
    if prev_row is not None and prev_row < table.row_count:
        try:
            table.update_cell_at(Coordinate(prev_row, 0), _BLANK_CELL)
        except Exception:  # noqa: BLE001 — best-effort marker reset
            pass
    if new_row is not None and 0 <= new_row < table.row_count:
        try:
            table.update_cell_at(Coordinate(new_row, 0), _marker(dawn))
            return new_row
        except Exception:  # noqa: BLE001
            return None
    return None


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


_HELP_TEXT = """\
[b #eb6f92]MAIN VIEW[/]
[#393552]────────────────────────────────────────────[/]
  [b #e0def4]↩[/]  [#908caa]details[/]       [b #e0def4]c[/]  [#908caa]context[/]       [b #e0def4]t[/]  [#908caa]trace[/]
  [b #e0def4]y[/]  [#908caa]share[/]         [b #e0def4]s[/]  [#908caa]services[/]      [b #e0def4]␣[/]  [#908caa]pause[/]
  [b #e0def4]g[/]  [#908caa]jump latest[/]   [b #e0def4]r[/]  [#908caa]reload[/]        [b #e0def4]T[/]  [#908caa]theme[/]
  [b #e0def4]?[/]  [#908caa]help[/]          [b #e0def4]q[/]  [#908caa]quit[/]          [b #e0def4]esc[/]  [#908caa]cancel input[/]

[b #eb6f92]FILTERS  ·  composable, all live[/]
[#393552]────────────────────────────────────────────[/]
  [b #9ccfd8]/[/]  [#908caa]text substring across all fields[/]
  [b #3e8fb0]i[/]  [#908caa]your public IP (auto-detected) — only request logs[/]
  [b #c4a7e7]e[/]  [#908caa]principalEmail — audit logs and IAM-protected services[/]
  [b #f6c177]S[/]  [#908caa]minimum severity — INFO / WARNING / ERROR / …[/]
  [b #ea9a97]u[/]  [#908caa]URL substring on httpRequest.requestUrl[/]
  [b #e0def4]x[/]  [#908caa]clear all filters[/]
  [#56526e]Filters compose with AND. Re-press a key with empty input to[/]
  [#56526e]clear that one filter. The live tail keeps flowing — only[/]
  [#56526e]matching new rows are appended.[/]

[b #eb6f92]INSIDE DETAILS / TRACE / CONTEXT[/]
[#393552]────────────────────────────────────────────[/]
  [b #e0def4]↩[/]  [#908caa]details on selected row[/]
  [b #e0def4]c[/]  [#908caa]context for this row[/]
  [b #e0def4]t[/]  [#908caa]trace drill for this row[/]
  [b #e0def4]y[/]  [#908caa]share — details copies the entry, trace copies the[/]
       [#908caa]full cross-service stitch, context copies the window[/]
  [b #e0def4]esc / q[/]  [#908caa]back[/]

[b #eb6f92]SERVICE PICKER[/]
[#393552]────────────────────────────────────────────[/]
  [b #e0def4]␣[/]  [#908caa]toggle service[/]      [b #e0def4]a[/]  [#908caa]all[/]
  [b #e0def4]n[/]  [#908caa]none[/]                [b #e0def4]↩[/]  [#908caa]apply[/]
  [b #e0def4]esc[/]  [#908caa]cancel[/]

[b #eb6f92]ROW MARKERS  ·  severity legend[/]
[#393552]────────────────────────────────────────────[/]
  [b #eb6f92]E  C  A[/]   [#e0def4]ERROR  ·  CRITICAL  ·  ALERT  ·  EMERGENCY[/]
  [b #f6c177]W[/]         [#e0def4]WARNING[/]
  [b #9ccfd8]I  N[/]      [#e0def4]INFO  ·  NOTICE[/]
  [b #56526e]D  ·[/]      [#908caa]DEBUG  ·  DEFAULT[/]

  [#c4a7e7]●[/]  [#e0def4]row has a trace ID — press[/] [b #e0def4]t[/] [#e0def4]to drill[/]
  [#56526e]·[/]  [#908caa]no trace ID  (startup / background logs)[/]

[b #eb6f92]STATUS-BAR MODES[/]
[#393552]────────────────────────────────────────────[/]
  [b #9ccfd8]● LIVE[/]      [#908caa]polling tail every 2s, filtered by the active set[/]
  [b #f6c177]⏸ PAUSED[/]    [#908caa]buffer frozen, no polling[/]
  [#56526e]Active filters appear as chips after the row count.[/]

[b #eb6f92]THEMES[/]
[#393552]────────────────────────────────────────────[/]
  [b #e0def4]T[/]            [#908caa]toggle rose-pine-moon  ↔  rose-pine-dawn[/]
  [b #e0def4]ctrl+p[/]       [#908caa]command palette — type "theme" to pick any[/]

[b #eb6f92]SHARE TO AGENT  ·  y[/]
[#393552]────────────────────────────────────────────[/]
  [#908caa]Copies a markdown brief to the clipboard: the log facts, a Cloud[/]
  [#908caa]Logging Explorer link, and a list of literal CloudLens MCP tool[/]
  [#908caa]calls with arguments pre-filled. Paste into an agent that has the[/]
  [#908caa]`cloudlens` MCP and it can investigate without further prompting.[/]
"""


class HelpScreen(ModalScreen[None]):
    """All keybinds, reachable from every screen via `?`."""

    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 80; height: 90%;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }
    #help-head {
        height: 1;
        color: $text-muted;
    }
    #help-body {
        height: 1fr;
        overflow-y: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "back"),
        Binding("q", "dismiss", "back"),
        Binding("question_mark", "dismiss", "back"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label(
                "[b #eb6f92]HELP[/]   [#56526e]keyboard shortcuts  ·  esc/q/? to close[/]",
                id="help-head",
            )
            yield Static(_HELP_TEXT, id="help-body")


class ContextScreen(ModalScreen[None]):
    """Service-focused view: N entries before + M after an anchor row.

    Two parallel Cloud Logging queries (descending<=anchor, ascending>anchor)
    merged into one ascending stream with the cursor positioned on the anchor.
    """

    BEFORE = 25
    AFTER = 25
    WINDOW_MINUTES = 30  # bound on each side; tightens the query

    CSS = """
    ContextScreen { align: center middle; }
    #ctx-box {
        width: 95%; height: 90%;
        background: $panel;
        border: round $accent;
        padding: 1 2;
    }
    #ctx-head { height: auto; }
    #ctx-label { color: $primary; }
    #ctx-svc { color: $text; }
    #ctx-sub { color: $text-muted; height: 1; margin-top: 1; }
    #ctx-table {
        height: 1fr; margin-top: 1;
        background: $panel;
        scrollbar-background: $panel;
        scrollbar-color: $boost;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: $panel;
        color: $text-muted;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $boost;
        text-style: bold;
    }
    DataTable > .datatable--hover { background: $boost; }
    DataTable > .datatable--odd-row  { background: $panel; }
    DataTable > .datatable--even-row { background: $panel; }
    #ctx-foot { color: $text-muted; height: 1; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "back"),
        Binding("q", "dismiss", "back"),
        Binding("t", "open_trace", "trace"),
        Binding("c", "open_context", "context"),
        Binding("y", "share", "share context"),
        Binding("question_mark", "open_help", "help"),
    ]

    def __init__(self, anchor: dict, obs: Observability) -> None:
        super().__init__()
        self._anchor = anchor
        self._obs = obs
        self._entries: list[dict] = []
        self._marked_row: Optional[int] = None

    def compose(self) -> ComposeResult:
        svc = self._anchor.get("svc", "—")
        ts = _fmt_time(self._anchor.get("ts"))
        svc_color = _color_for_service(svc, _is_dawn(self.app.theme))
        with Vertical(id="ctx-box"):
            yield Label("[b]CONTEXT[/]", id="ctx-label")
            yield Label(
                f"[b {svc_color}]{svc}[/]",
                id="ctx-svc",
            )
            yield Label(
                f"around [b]{ts}[/]  ·  ±{self.BEFORE} entries",
                id="ctx-sub",
            )
            yield DataTable(
                id="ctx-table", cursor_type="row", zebra_stripes=True,
            )
            yield Label(
                "esc/q back  ·  ↩ details  ·  c context  ·  t trace  ·  y share",
                id="ctx-foot",
            )

    async def on_mount(self) -> None:
        table = self.query_one("#ctx-table", DataTable)
        table.cursor_foreground_priority = "renderable"
        table.add_column("", width=1)
        table.add_column("TIME", width=15)
        table.add_column("SEV", width=3)
        table.add_column("T", width=2)
        # INST: last 8 chars of the Cloud Run instance ID, color-hashed. Lets
        # the user see when consecutive entries hit different instances —
        # often a sign of autoscaling, cold starts, or container churn.
        table.add_column("INST", width=8)
        table.add_column("MESSAGE")
        # Right-side "← OPENED FROM" tag, blank on every row except the anchor.
        table.add_column("", width=16, key="opened")
        table.show_horizontal_scrollbar = False
        anchor_ts = _parse_ts(self._anchor.get("ts"))
        svc = self._anchor.get("svc")
        if anchor_ts is None or not svc:
            self.app.notify("anchor missing service/timestamp", severity="error")
            self.dismiss()
            return
        try:
            before, after = await asyncio.gather(
                asyncio.to_thread(self._fetch_before, svc, anchor_ts),
                asyncio.to_thread(self._fetch_after, svc, anchor_ts),
            )
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"context fetch failed: {exc}", severity="error")
            self.dismiss()
            return
        # `before` came back descending; reverse to ascending. `after` already
        # ascending. Merge → one oldest-to-newest stream around the anchor.
        merged_raw = list(reversed(before)) + list(after)
        formatted = format_entries(merged_raw)
        # Dedup in case the anchor instant is captured by both halves.
        seen: set[object] = set()
        deduped: list[dict] = []
        for raw, e in zip(merged_raw, formatted):
            key = getattr(raw, "insert_id", None) or (
                e.get("ts"), e.get("msg")
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
        self._entries = deduped
        anchor_ts_iso = self._anchor.get("ts")
        anchor_msg = self._anchor.get("msg")
        anchor_row: Optional[int] = None
        dawn = _is_dawn(self.app.theme)
        for i, e in enumerate(self._entries):
            # Last cell is the opened-from tag slot — blank for every row,
            # we'll fill in the anchor's after the loop.
            table.add_row(*_row_focused(e, dawn), _BLANK_CELL)
            if (
                anchor_row is None
                and e.get("ts") == anchor_ts_iso
                and (e.get("msg") or "") == (anchor_msg or "")
            ):
                anchor_row = i
        if anchor_row is not None:
            table.move_cursor(row=anchor_row, animate=False)
            # Opened tag goes in the last column (index 6: marker, TIME, SEV,
            # T, INST, MESSAGE, OPENED).
            try:
                table.update_cell_at(
                    Coordinate(anchor_row, 6), _opened_tag(dawn),
                )
            except Exception:  # noqa: BLE001 — best-effort decoration
                pass
        else:
            self.app.notify(
                "anchor not in window — showing nearest",
                severity="warning",
            )

    def _fetch_before(self, svc: str, anchor_ts: datetime) -> list:
        f = build_filter(
            self._obs.project, service=svc,
            since=anchor_ts - timedelta(minutes=self.WINDOW_MINUTES),
            until=anchor_ts + timedelta(microseconds=1),
            exclude_noise=True,
        )
        return self._obs.logs.list_entries(f, limit=self.BEFORE, ascending=False)

    def _fetch_after(self, svc: str, anchor_ts: datetime) -> list:
        f = build_filter(
            self._obs.project, service=svc,
            since=anchor_ts + timedelta(microseconds=1),
            until=anchor_ts + timedelta(minutes=self.WINDOW_MINUTES),
            exclude_noise=True,
        )
        return self._obs.logs.list_entries(f, limit=self.AFTER, ascending=True)

    def _cursor_entry(self) -> Optional[dict]:
        table = self.query_one("#ctx-table", DataTable)
        if table.row_count == 0:
            return None
        idx = table.cursor_row
        if idx is None or not 0 <= idx < len(self._entries):
            return None
        return self._entries[idx]

    def action_open_trace(self) -> None:
        e = self._cursor_entry()
        if e is None:
            return
        trace = e.get("trace")
        if not trace:
            self.app.notify("no trace on this row", severity="warning")
            return
        self.app.push_screen(TraceScreen(trace, self._obs, anchor=e))

    def action_open_context(self) -> None:
        e = self._cursor_entry()
        if e is not None and e.get("svc") and e.get("ts"):
            self.app.push_screen(ContextScreen(e, self._obs))

    def action_share(self) -> None:
        brief = format_context_share(
            self._anchor, self._entries, self._obs.project,
        )
        self.app.copy_to_clipboard(brief)
        self.app.notify(
            f"copied context ({len(self._entries)} entries) — paste into your agent"
        )

    def action_open_help(self) -> None:
        self.app.push_screen(HelpScreen())

    @on(DataTable.RowSelected, "#ctx-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or not 0 <= idx < len(self._entries):
            return
        self.app.push_screen(DetailScreen(self._entries[idx], self._obs))

    @on(DataTable.RowHighlighted, "#ctx-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#ctx-table", DataTable)
        self._marked_row = _apply_marker(
            table, self._marked_row, event.cursor_row, _is_dawn(self.app.theme),
        )


class DetailScreen(ModalScreen[None]):
    """Full view of one log entry — opened with ↩ on a row."""

    CSS = """
    DetailScreen { align: center middle; }
    #detail-box {
        width: 90%; height: 85%;
        background: $panel;
        border: round $accent;
        padding: 1 2;
    }
    #detail-badge { height: 1; }
    #detail-sev-big { height: 1; margin-top: 1; }
    #detail-svc-big { height: 1; }
    #detail-when { color: $text-muted; height: 1; }
    Rule { color: $panel-darken-1; margin: 1 0; }
    #detail-meta { height: auto; }
    #detail-msg-label { color: $text-muted; height: 1; margin-top: 1; }
    #detail-msg {
        height: 1fr;
        background: $surface;
        border: round $panel-darken-1;
        padding: 0 1;
        overflow-y: auto;
        margin-top: 1;
    }
    #detail-footer { height: 1; color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "back"),
        Binding("q", "dismiss", "back"),
        Binding("t", "open_trace", "trace"),
        Binding("c", "open_context", "context"),
        Binding("y", "share", "share"),
        Binding("question_mark", "open_help", "help"),
    ]

    def __init__(self, entry: dict, obs: Observability) -> None:
        super().__init__()
        self._entry = entry
        self._obs = obs

    def compose(self) -> ComposeResult:
        e = self._entry
        dawn = _is_dawn(self.app.theme)
        sev = e.get("sev", "DEFAULT")
        sev_color = (_SEV_COLOR_DAWN if dawn else _SEV_COLOR_MOON).get(
            sev, _DIM_DAWN if dawn else _DIM_MOON,
        )
        sev_glyph = _SEV_GLYPH.get(sev, "●")
        svc = e.get("svc", "—")
        svc_color = _color_for_service(svc, dawn)
        ts = e.get("ts", "—")
        rev = e.get("rev", "")
        with Vertical(id="detail-box"):
            yield Label(
                f"[#908caa]LOG ENTRY[/]   [#56526e]·[/]   "
                f"[{sev_color}]{sev_glyph}[/] [b {sev_color}]{sev}[/]",
                id="detail-badge",
            )
            # Big two-line headline: sev label and service name each get their
            # own row so the eye lands on them like a page title.
            yield Label(
                f"[{sev_color}]{sev_glyph}[/]   [b {sev_color}]{sev}[/]",
                id="detail-sev-big",
            )
            yield Label(f"[b {svc_color}]{svc}[/]", id="detail-svc-big")
            when = ts
            if rev:
                when = f"{when}  ·  rev {rev}"
            yield Label(f"[#56526e]{when}[/]", id="detail-when")
            yield Rule(line_style="solid")
            yield Static(self._meta_text(sev_color, svc_color), id="detail-meta")
            yield Label("[#56526e]MESSAGE[/]", id="detail-msg-label")
            yield Static(
                e.get("msg") or e.get("http") or "(no message)",
                id="detail-msg",
            )
            hints = ["esc/q back", "y share"]
            if e.get("svc") and e.get("ts"):
                hints.append("c context")
            if e.get("trace"):
                hints.append("t trace")
            yield Static("  ·  ".join(hints), id="detail-footer")

    def _meta_text(self, sev_color: str, svc_color: str) -> str:
        e = self._entry
        sev = e.get("sev", "DEFAULT")
        rows = [
            f"[#56526e]SEVERITY[/]    [b {sev_color}]{sev}[/]",
        ]
        if e.get("http"):
            rows.append(f"[#56526e]HTTP    [/]    {e['http']}")
        if e.get("trace"):
            rows.append(f"[#56526e]TRACE   [/]    [#c4a7e7]{e['trace']}[/]")
        if e.get("rev"):
            rows.append(f"[#56526e]REV     [/]    {e['rev']}")
        if e.get("inst"):
            # Full instance ID — the column form is truncated in context;
            # here we show all of it so a copy-paste lands the real value.
            inst = e["inst"]
            inst_color = _color_for_instance(
                inst, _is_dawn(self.app.theme),
            )
            rows.append(f"[#56526e]INSTANCE[/]    [{inst_color}]{inst}[/]")
        return "\n".join(rows)

    def action_open_trace(self) -> None:
        trace = self._entry.get("trace")
        if trace:
            self.app.push_screen(
                TraceScreen(trace, self._obs, anchor=self._entry),
            )

    def action_open_context(self) -> None:
        if self._entry.get("svc") and self._entry.get("ts"):
            self.app.push_screen(ContextScreen(self._entry, self._obs))

    def action_share(self) -> None:
        brief = format_entry_share(self._entry, self._obs.project)
        self.app.copy_to_clipboard(brief)
        self.app.notify("copied — paste into your agent")

    def action_open_help(self) -> None:
        self.app.push_screen(HelpScreen())


class TraceScreen(ModalScreen[None]):
    """Cross-service drill-down for one trace_id."""

    CSS = """
    TraceScreen { align: center middle; }
    #trace-box {
        width: 95%; height: 90%;
        background: $panel;
        border: round $accent;
        padding: 1 2;
    }
    #trace-label { color: $primary; height: 1; }
    #trace-id { color: $secondary; height: 1; }
    #trace-sub { color: $text-muted; height: 1; margin-top: 1; }
    #trace-table {
        height: 1fr; margin-top: 1;
        background: $panel;
        scrollbar-background: $panel;
        scrollbar-color: $boost;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: $panel;
        color: $text-muted;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $boost;
        text-style: bold;
    }
    DataTable > .datatable--hover { background: $boost; }
    DataTable > .datatable--odd-row  { background: $panel; }
    DataTable > .datatable--even-row { background: $panel; }
    #trace-foot { color: $text-muted; height: 1; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "back"),
        Binding("q", "dismiss", "back"),
        Binding("c", "open_context", "context"),
        Binding("y", "share", "share trace"),
        Binding("question_mark", "open_help", "help"),
    ]

    def __init__(
        self,
        trace_id: str,
        obs: Observability,
        anchor: Optional[dict] = None,
    ) -> None:
        super().__init__()
        self._trace_id = trace_id
        self._obs = obs
        # The entry the user was on when they pressed `t` — used to tag the
        # matching row in the trace stitch with "← OPENED FROM" so they can
        # find their starting point amid the cross-service entries.
        self._anchor = anchor
        self._entries: list[dict] = []
        self._marked_row: Optional[int] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-box"):
            yield Label("[b]TRACE[/]", id="trace-label")
            yield Label(f"[b]{self._trace_id}[/]", id="trace-id")
            yield Label("loading…", id="trace-sub")
            yield DataTable(
                id="trace-table", cursor_type="row", zebra_stripes=True,
            )
            yield Label(
                "esc/q back  ·  ↩ details  ·  c context  ·  y share trace",
                id="trace-foot",
            )

    async def on_mount(self) -> None:
        table = self.query_one("#trace-table", DataTable)
        table.cursor_foreground_priority = "renderable"
        table.add_column("", width=1)
        table.add_column("TIME", width=15)
        table.add_column("SEV", width=3)
        table.add_column("T", width=2)
        table.add_column("SERVICE", width=26)
        table.add_column("MESSAGE")
        # Right-side "← OPENED FROM" tag column — fills only on the anchor row.
        table.add_column("", width=16, key="opened")
        table.show_horizontal_scrollbar = False
        entries = await asyncio.to_thread(
            self._obs.logs.get_logs_by_trace, self._trace_id, 24.0, 500
        )
        self._entries = format_entries(entries)
        dawn = _is_dawn(self.app.theme)
        services_seen: list[str] = []
        times: list[datetime] = []
        anchor_ts = self._anchor.get("ts") if self._anchor else None
        anchor_msg = self._anchor.get("msg") if self._anchor else None
        anchor_row: Optional[int] = None
        for i, e in enumerate(self._entries):
            table.add_row(*_row(e, dawn), _BLANK_CELL)
            svc = e.get("svc")
            if svc and svc not in services_seen:
                services_seen.append(svc)
            ts = _parse_ts(e.get("ts"))
            if ts is not None:
                times.append(ts)
            if (
                anchor_row is None
                and anchor_ts is not None
                and e.get("ts") == anchor_ts
                and (e.get("msg") or "") == (anchor_msg or "")
            ):
                anchor_row = i
        if anchor_row is not None:
            # Opened tag is the last column we added (index 6: marker, TIME,
            # SEV, T, SERVICE, MESSAGE, OPENED).
            try:
                table.update_cell_at(
                    Coordinate(anchor_row, 6), _opened_tag(dawn),
                )
            except Exception:  # noqa: BLE001
                pass
        # Sub-line: services involved · entry count · cross-service span
        svc_chips = "  ·  ".join(
            f"[{_color_for_service(s, dawn)}]{s}[/]" for s in services_seen
        ) or "[#56526e]no services[/]"
        n = len(self._entries)
        parts = [svc_chips, f"[#908caa]{n} entr{'y' if n == 1 else 'ies'}[/]"]
        if len(times) >= 2:
            span_ms = int((max(times) - min(times)).total_seconds() * 1000)
            parts.append(f"[#908caa]{span_ms}ms span[/]")
        self.query_one("#trace-sub", Label).update(
            "    [#56526e]·[/]    ".join(parts)
        )

    def _cursor_entry(self) -> Optional[dict]:
        table = self.query_one("#trace-table", DataTable)
        if table.row_count == 0:
            return None
        idx = table.cursor_row
        if idx is None or not 0 <= idx < len(self._entries):
            return None
        return self._entries[idx]

    def action_open_context(self) -> None:
        e = self._cursor_entry()
        if e is not None and e.get("svc") and e.get("ts"):
            self.app.push_screen(ContextScreen(e, self._obs))

    def action_share(self) -> None:
        brief = format_trace_share(
            self._trace_id, self._entries, self._obs.project,
        )
        self.app.copy_to_clipboard(brief)
        self.app.notify(
            f"copied trace ({len(self._entries)} entries) — paste into your agent"
        )

    def action_open_help(self) -> None:
        self.app.push_screen(HelpScreen())

    @on(DataTable.RowSelected, "#trace-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or not 0 <= idx < len(self._entries):
            return
        self.app.push_screen(DetailScreen(self._entries[idx], self._obs))

    @on(DataTable.RowHighlighted, "#trace-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#trace-table", DataTable)
        self._marked_row = _apply_marker(
            table, self._marked_row, event.cursor_row, _is_dawn(self.app.theme),
        )


class ServicePicker(ModalScreen[Optional[list[str]]]):
    CSS = """
    ServicePicker { align: center middle; }
    #picker-box {
        width: 64; height: 80%;
        background: $panel;
        border: round $primary;
        padding: 1 2;
    }
    #picker-label { color: $primary; height: 1; }
    #picker-count { color: $text-muted; height: 1; }
    #picker-list {
        height: 1fr; margin-top: 1;
        background: $panel;
        border: none;
    }
    SelectionList > .selection-list--button-selected {
        color: $accent;
    }
    SelectionList:focus > .option-list--option-highlighted {
        background: $boost;
        text-style: bold;
    }
    SelectionList > .option-list--option-highlighted {
        background: $boost;
    }
    #picker-foot { color: $text-muted; height: 1; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        # priority=True so screen-level enter beats SelectionList's default
        # enter-to-toggle. Space stays as the toggle (SelectionList default).
        Binding("enter", "confirm", "apply", priority=True),
        Binding("a", "select_all", "all"),
        Binding("n", "select_none", "none"),
        Binding("question_mark", "open_help", "help"),
    ]

    def __init__(self, services: list[str], selected: list[str]) -> None:
        super().__init__()
        self._services = services
        self._initial = set(selected)

    def compose(self) -> ComposeResult:
        n_sel = len(self._initial)
        n_all = len(self._services)
        dawn = _is_dawn(self.app.theme)
        with Vertical(id="picker-box"):
            yield Label("[b]SERVICES[/]", id="picker-label")
            yield Label(
                f"{n_sel} of {n_all} selected",
                id="picker-count",
            )
            yield SelectionList[str](
                *(
                    Selection(
                        Text(s, style=_color_for_service(s, dawn)),
                        s,
                        s in self._initial,
                    )
                    for s in self._services
                ),
                id="picker-list",
            )
            yield Label(
                "␣ toggle  ·  a all  ·  n none  ·  ↩ apply  ·  esc cancel",
                id="picker-foot",
            )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        self.dismiss(list(self.query_one(SelectionList).selected))

    def action_select_all(self) -> None:
        self.query_one(SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one(SelectionList).deselect_all()

    def action_open_help(self) -> None:
        self.app.push_screen(HelpScreen())

    @on(SelectionList.SelectedChanged, "#picker-list")
    def _on_selection_changed(
        self, event: SelectionList.SelectedChanged,
    ) -> None:
        n_sel = len(event.selection_list.selected)
        self.query_one("#picker-count", Label).update(
            f"{n_sel} of {len(self._services)} selected"
        )


class CloudLensApp(App):
    TITLE = "CloudLens"

    CSS = """
    Screen { layout: vertical; background: $background; }

    #cl-header {
        height: 2; padding: 0 1; background: $panel;
        align-vertical: middle;
    }
    #cl-header-left  { width: 1fr; height: 1; }
    #cl-header-right { width: auto; height: 1; color: $text-muted; content-align: right middle; }

    #status-bar {
        height: 2; padding: 0 1; background: $surface;
        align-vertical: middle;
    }
    #status-left  { width: 1fr; height: 1; background: $surface; }
    #status-right { width: auto; height: 1; background: $surface; content-align: right middle; }

    /* Shared input row for every filter type — the prompt char and
       placeholder mutate based on which filter the user opened. */
    #filter-row {
        height: 3;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }
    #filter-prompt {
        width: 3; height: 1; padding: 0;
        color: $primary; content-align: center middle;
    }
    #filter-input {
        width: 1fr; height: 1;
        background: $surface; color: $text;
        border: none;
    }
    #filter-input:focus { border: none; }
    #filter-hint {
        width: auto; height: 1; padding: 0 1 0 0;
        color: $text-muted; content-align: right middle;
    }

    #log-table {
        height: 1fr;
        background: $background;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $boost;
        scrollbar-color-hover: $panel-lighten-1;
        scrollbar-color-active: $panel-lighten-1;
        scrollbar-corner-color: $background;
        scrollbar-size: 1 1;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $boost;
        text-style: bold;
    }
    DataTable > .datatable--hover {
        background: $boost;
    }
    DataTable > .datatable--odd-row  { background: $background; }
    DataTable > .datatable--even-row { background: $background; }

    #cl-footer {
        height: 2; padding: 0 1; background: $panel; color: $text-muted;
        content-align: left middle;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        # Filter palette — each key opens the shared input row primed for
        # that filter type. Re-pressing with empty input clears that
        # specific filter; `x` clears them all.
        Binding("slash", "open_text_filter", "text"),
        Binding("e", "open_email_filter", "email"),
        Binding("i", "open_ip_filter", "my IP", show=False),
        Binding("S", "open_severity_filter", "severity", show=False),
        Binding("u", "open_url_filter", "url", show=False),
        Binding("x", "clear_filters", "clear", show=False),
        Binding("enter", "open_detail", "details"),
        Binding("c", "open_context", "context"),
        Binding("s", "pick_services", "services"),
        Binding("space", "toggle_polling", "pause/resume"),
        Binding("t", "open_trace", "trace"),
        Binding("y", "share", "share"),
        Binding("g", "scroll_to_latest", "latest", show=False),
        Binding("T", "toggle_theme", "theme", show=False),
        Binding("question_mark", "open_help", "help"),
        Binding("escape", "back_to_live", "back", show=False),
        Binding("r", "reload", "reload", show=False),
    ]

    # Per-filter UI metadata — keys map to (prompt char, placeholder,
    # constructor). The constructor takes the raw input string and
    # returns the Filter instance (or None to clear).
    _FILTER_UI: dict[str, tuple[str, str, "object"]] = {
        "text": (
            "/", "filter rows by free-text substring",
            lambda s: TextFilter(s) if s else None,
        ),
        "email": (
            "@", "principalEmail — IAM-authenticated requests only",
            lambda s: EmailFilter(s) if s else None,
        ),
        "severity": (
            "≥", "minimum severity: INFO / WARNING / ERROR / CRITICAL",
            lambda s: SeverityFilter(s) if s else None,
        ),
        "url": (
            "~", "URL substring (httpRequest.requestUrl)",
            lambda s: UrlFilter(s) if s else None,
        ),
        "ip": (
            "i", "remote IP (request logs only — pivot via t for stdout)",
            lambda s: IpFilter(s) if s else None,
        ),
    }

    def __init__(
        self,
        obs: Observability,
        services: Optional[list[str]],
        hours: float,
    ) -> None:
        super().__init__()
        self._obs = obs
        self._all_services: list[str] = []
        self._selected: Optional[list[str]] = services  # None ⇒ all
        self._initial_minutes = max(hours * 60.0, 1.0)
        self._buffer: list[dict] = []
        self._seen: set[object] = set()
        self._mode: Mode = Mode.LIVE
        # Active filters keyed by Filter.key. Every fetch path
        # (initial window, live tail, lazy-load history) ANDs each
        # active filter's clause into the Cloud Logging query. New
        # filter types defined in filters.py drop in here without any
        # changes to the fetch logic — see _active_clauses().
        self._active_filters: dict[str, Filter] = {}
        # Which filter type the input row is currently editing, if any.
        # Lets one shared Input handle every filter kind — submit
        # dispatches by this value.
        self._editing_filter: Optional[str] = None
        self._last_ts: Optional[datetime] = None
        self._busy = False
        # Count of new rows appended while the user was scrolled up. Reset
        # when the user returns to the bottom on their own (detected on tick).
        self._unseen = 0
        # Row currently holding the rose ▌ marker. Tracked so we can clear
        # the previous cell when the cursor moves.
        self._marked_row: Optional[int] = None
        # In-flight guard for the load-older paginator. Without it, fast
        # cursor movement could fire several overlapping fetches.
        self._loading_older = False
        # Your machine's public IP, detected once at startup. Used to
        # pre-fill the IP filter prompt so a single keystroke (`i`)
        # followed by enter applies the "only my browser session"
        # filter. None until the background fetch completes (or fails).
        self._public_ip: Optional[str] = None
        # Set to True once the API returns zero older entries — stops us
        # from hammering the API on every up-arrow once we've exhausted
        # the retention window.
        self._history_exhausted = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="cl-header"):
            yield Static("", id="cl-header-left")
            yield Static("", id="cl-header-right")
        with Horizontal(id="status-bar"):
            yield Static("", id="status-left")
            yield Static("", id="status-right")
        with Horizontal(id="filter-row"):
            yield Static("/", id="filter-prompt")
            yield Input(placeholder="filter", id="filter-input")
            yield Static(
                "[italic]enter apply · empty clears · esc cancel[/]",
                id="filter-hint",
            )
        yield DataTable(id="log-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="cl-footer")

    async def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME
        table = self.query_one("#log-table", DataTable)
        # Let renderable colors (severity dot, trace dot, service hue) win on
        # the cursor row — otherwise CSS `color` clobbers them with one uniform
        # foreground and the row goes monochrome on hover.
        table.cursor_foreground_priority = "renderable"
        table.add_column("", width=1)
        table.add_column("TIME", width=15)
        table.add_column("SEV", width=3)
        table.add_column("T", width=2)
        table.add_column("SERVICE", width=26)
        table.add_column("MESSAGE")
        table.show_horizontal_scrollbar = False
        self.query_one("#filter-row", Horizontal).display = False
        self._refresh_header()
        self._refresh_footer()
        self._refresh_status(loading=True)
        # Show the spinner before we even start listing services — covers the
        # full startup gap (services + first log fetch) so the user isn't
        # staring at an empty table wondering if the app froze.
        table.set_loading(True)
        try:
            self._all_services = await asyncio.to_thread(self._list_services)
        except Exception as exc:  # noqa: BLE001 — surfaced in UI
            self.notify(f"list_services failed: {exc}", severity="error")
        self._refresh_header()  # picks up service count for "all services"
        # _reload_live owns the table.loading lifecycle from here — it sets
        # True at the top and False in its finally.
        await self._reload_live()
        self.set_interval(_TAIL_INTERVAL, self._tick, name="tail")
        table.focus()
        # Fire-and-forget public IP detection so the `i` keybind is
        # already prefilled by the time the user presses it. Failures
        # are silent — user can still type an IP manually.
        self.run_worker(self._detect_public_ip(), exclusive=False)

    async def _detect_public_ip(self) -> None:
        def _fetch() -> Optional[str]:
            import urllib.request
            try:
                with urllib.request.urlopen(
                    "https://api.ipify.org", timeout=2.0,
                ) as resp:
                    return resp.read().decode("ascii").strip()
            except Exception:  # noqa: BLE001 — best-effort, no UI surface
                return None

        ip = await asyncio.to_thread(_fetch)
        if ip:
            self._public_ip = ip

    # --- data fetching -----------------------------------------------------

    def _list_services(self) -> list[str]:
        return sorted(s["name"] for s in self._obs.runtime.list_services())

    def _fetch(self, *, since: Optional[datetime], limit: int) -> list:
        f = build_filter(
            self._obs.project,
            services=self._selected,  # None ⇒ all
            hours=None if since else (self._initial_minutes / 60.0),
            since=since,
            extras=self._active_clauses(),
            exclude_noise=True,
        )
        return self._obs.logs.list_entries(f, limit=limit)

    def _active_clauses(self) -> list[str]:
        """Collect every active filter's Cloud Logging fragment. Used by
        every fetch path so all filters apply uniformly."""
        out: list[str] = []
        for f in self._active_filters.values():
            c = f.clause(self._obs.project)
            if c:
                out.append(c)
        return out

    async def _reload_live(self) -> None:
        table = self.query_one("#log-table", DataTable)
        self._mode = Mode.LIVE
        self._buffer.clear()
        self._seen.clear()
        self._last_ts = None
        self._unseen = 0
        self._history_exhausted = False
        table.clear()
        self._marked_row = None
        # Centered braille spinner overlay while the fetch runs — covers the
        # otherwise-blank table area so the user knows we're working.
        table.set_loading(True)
        self._refresh_status(loading=True)
        if self._selected == []:
            table.set_loading(False)
            self._refresh_status()
            return
        since = datetime.now(timezone.utc) - timedelta(minutes=self._initial_minutes)
        entries: list = []
        try:
            entries = await asyncio.to_thread(
                self._fetch, since=since, limit=_INITIAL_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(f"fetch failed: {exc}", severity="error")
            self._refresh_status()
            return
        finally:
            table.set_loading(False)
        for raw in reversed(entries):  # API ↓ → buffer oldest→newest
            self._ingest(raw)
        self._render_all()
        self._refresh_status()

    async def _tick(self) -> None:
        # Filters are applied via _fetch — the tail keeps polling with
        # whatever predicates are active. Only PAUSED stops polling.
        if self._mode != Mode.LIVE or self._busy or self._selected == []:
            return
        since = (
            self._last_ts + timedelta(microseconds=1)
            if self._last_ts is not None
            else datetime.now(timezone.utc) - timedelta(minutes=2)
        )
        self._busy = True
        try:
            new = await asyncio.to_thread(
                self._fetch, since=since, limit=_TAIL_LIMIT,
            )
        finally:
            self._busy = False
        appended = self._append(new)
        if appended:
            self._refresh_status()
        if len(self._buffer) > _BUFFER_CAP:
            self._buffer = self._buffer[-_BUFFER_TRIM:]
            self._render_all()

    def _append(self, raw_entries: list) -> int:
        table = self.query_one("#log-table", DataTable)
        appended = 0
        dawn = _is_dawn(self.theme)
        for raw in reversed(raw_entries):
            entry = self._ingest(raw)
            if entry is not None:
                table.add_row(*_row(entry, dawn))
                appended += 1
        # Cursor stays where it was — the user controls when to advance.
        # `_unseen` carries the news; RowHighlighted resets it when the cursor
        # reaches the new bottom (see _on_row_highlighted).
        if appended and self._mode == Mode.LIVE:
            self._unseen += appended
        return appended

    def _ingest(self, raw) -> Optional[dict]:
        key = getattr(raw, "insert_id", None) or id(raw)
        if key in self._seen:
            return None
        self._seen.add(key)
        entry = format_entries([raw])[0]
        self._buffer.append(entry)
        ts = getattr(raw, "timestamp", None)
        if ts is not None and (self._last_ts is None or ts > self._last_ts):
            self._last_ts = ts
        return entry

    # --- rendering ---------------------------------------------------------

    def _render_all(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.clear()
        # table.clear() wipes any marker we had drawn; track that and re-mark
        # on the cursor row after rows are re-added below.
        self._marked_row = None
        dawn = _is_dawn(self.theme)
        for e in self._buffer:
            table.add_row(*_row(e, dawn))
        if self._mode == Mode.LIVE and table.row_count > 0:
            # Park cursor at the head of the tail in LIVE mode so the marker
            # lands somewhere visible — otherwise it sits invisibly at row 0
            # while the viewport is scrolled to the bottom.
            table.move_cursor(row=table.row_count - 1, animate=False)
            table.scroll_end(animate=False)
        if table.row_count > 0:
            self._marked_row = _apply_marker(
                table, None, table.cursor_row, dawn,
            )

    def _scope_label(self) -> str:
        if self._selected is None:
            n = len(self._all_services) or "?"
            return f"all services ({n})"
        if not self._selected:
            return "[#eb6f92]no services[/]"
        if len(self._selected) <= 3:
            return ", ".join(self._selected)
        return f"{len(self._selected)} services"

    def _refresh_header(self) -> None:
        left = self.query_one("#cl-header-left", Static)
        right = self.query_one("#cl-header-right", Static)
        left.update(
            f"[b #eb6f92]CloudLens[/]   [#56526e]·[/]   "
            f"[#908caa]project[/]   [b]{self._obs.project}[/]"
        )
        right.update(f"[italic]{self.theme}[/]")

    def _refresh_footer(self) -> None:
        self.query_one("#cl-footer", Static).update(
            "q quit   ·   / text   ·   i my IP   ·   e email   ·   "
            "x clear   ·   s services   ·   ↩ details   ·   t trace   ·   "
            "c context   ·   y share   ·   ␣ pause   ·   T theme   ·   ? help"
        )

    def _refresh_status(self, *, loading: bool = False) -> None:
        left = self.query_one("#status-left", Static)
        right = self.query_one("#status-right", Static)
        scope = self._scope_label()
        rows = len(self._buffer)
        chips = self._filter_chips()
        if loading:
            left.update(
                f"[b #9ccfd8]⏳[/] [b #9ccfd8]LOADING[/]   "
                f"[#56526e]·[/]   {scope}{chips}"
            )
            right.update("[#56526e](please wait)[/]")
            return
        if self._mode == Mode.LIVE:
            left.update(
                f"[b #9ccfd8]●[/] [b #9ccfd8]LIVE[/]   [#56526e]·[/]   "
                f"{scope}   [#56526e]·[/]   [#908caa]{rows} logs[/]"
                f"{chips}"
            )
            right.update(
                f"[b #f6c177]↓ {self._unseen} new[/]" if self._unseen else ""
            )
        else:  # PAUSED
            left.update(
                f"[b #f6c177]⏸[/] [b #f6c177]PAUSED[/]   [#56526e]·[/]   "
                f"{scope}   [#56526e]·[/]   [#908caa]{rows} logs[/]"
                f"{chips}"
            )
            right.update("[#56526e](space to resume)[/]")

    def _filter_chips(self) -> str:
        """Render the active filter chips for the status bar. Order is
        stable across renders (preserve insertion order of _filters)."""
        if not self._active_filters:
            return ""
        dawn = _is_dawn(self.theme)
        parts = [f.chip(dawn) for f in self._active_filters.values()]
        joined = "   [#56526e]·[/]   ".join(parts)
        return f"   [#56526e]·[/]   {joined}"

    # --- actions -----------------------------------------------------------

    # One thin action per filter binding — bindings can't reliably pass
    # arguments across Textual versions, so each key has its own method
    # that dispatches to the shared `_open_filter` helper.
    def action_open_text_filter(self) -> None: self._open_filter("text")
    def action_open_email_filter(self) -> None: self._open_filter("email")
    def action_open_severity_filter(self) -> None: self._open_filter("severity")
    def action_open_url_filter(self) -> None: self._open_filter("url")
    def action_open_ip_filter(self) -> None: self._open_filter("ip")

    def _open_filter(self, kind: str) -> None:
        """Open the shared input row primed for filter `kind`.

        Pre-fills with the current value (if any) so the user can edit
        or clear with a single key. For `ip`, pre-fills with the
        detected public IP on first open — pressing enter then applies
        "only my browser session" without typing anything.
        Submitting empty removes the filter.
        """
        if kind not in self._FILTER_UI:
            return
        prompt, placeholder, _builder = self._FILTER_UI[kind]
        self._editing_filter = kind
        self.query_one("#filter-row", Horizontal).display = True
        self.query_one("#filter-prompt", Static).update(prompt)
        inp = self.query_one("#filter-input", Input)
        inp.placeholder = placeholder
        current = self._active_filters.get(kind)
        if current:
            inp.value = self._filter_text(current)
        elif kind == "ip" and self._public_ip:
            # Convenience: first `i` press → IP prefilled → enter applies.
            inp.value = self._public_ip
        else:
            inp.value = ""
        inp.focus()

    @staticmethod
    def _filter_text(f: Filter) -> str:
        """Best-effort reverse: pull the user-facing string out of a
        Filter so we can pre-fill the input on re-edit."""
        for attr in ("text", "email", "min_severity", "substring", "ip"):
            v = getattr(f, attr, None)
            if v:
                return v
        return ""

    @on(Input.Submitted, "#filter-input")
    def _on_filter_submit(self, event: Input.Submitted) -> None:
        kind = self._editing_filter
        if kind is None:
            self._hide_filter_input()
            return
        _prompt, _placeholder, builder = self._FILTER_UI[kind]
        new_filter = builder(event.value.strip())
        self._hide_filter_input()
        self._apply_filter(kind, new_filter)

    def _hide_filter_input(self) -> None:
        self._editing_filter = None
        self.query_one("#filter-row", Horizontal).display = False
        self.query_one("#log-table", DataTable).focus()

    def _apply_filter(self, kind: str, new_filter: Optional[Filter]) -> None:
        """Add, replace, or remove a filter by key. Triggers a refetch
        of the initial window so historical matches appear; live
        polling continues with the new filter set."""
        prev = self._active_filters.get(kind)
        if new_filter is None:
            self._active_filters.pop(kind, None)
        else:
            self._active_filters[kind] = new_filter
        if prev == new_filter:
            return  # no change, skip the refetch
        self._refresh_status()
        self.run_worker(self._reload_live(), exclusive=True)

    def action_clear_filters(self) -> None:
        if not self._active_filters:
            return
        self._active_filters.clear()
        self.notify("filters cleared")
        self._refresh_status()
        self.run_worker(self._reload_live(), exclusive=True)

    def action_back_to_live(self) -> None:
        # ESC just dismisses the filter input row if it's open. To remove
        # active filters, use the per-filter keys (re-press with empty
        # input) or `x` to clear them all.
        fr = self.query_one("#filter-row", Horizontal)
        if fr.display:
            self.query_one("#filter-input", Input).value = ""
            self._hide_filter_input()

    def action_open_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_share(self) -> None:
        idx = self._cursor_index()
        if idx is None:
            self.notify("no row selected to share", severity="warning")
            return
        brief = format_entry_share(self._buffer[idx], self._obs.project)
        self.copy_to_clipboard(brief)
        self.notify("copied — paste into your agent")

    def action_toggle_theme(self) -> None:
        self.theme = (
            CLOUDLENS_DAWN.name
            if self.theme == CLOUDLENS_MOON.name
            else CLOUDLENS_MOON.name
        )
        # Re-render so per-cell hex colors in Text cells follow the new theme.
        self._render_all()
        self._refresh_header()
        self._refresh_status()
        self.notify(f"theme: {self.theme}")

    def action_scroll_to_latest(self) -> None:
        table = self.query_one("#log-table", DataTable)
        if table.row_count == 0:
            return
        # Also move the cursor — RowHighlighted then carries the ▌ marker to
        # the latest entry, so `g` is a true "jump to head of tail" not just
        # a scroll.
        table.move_cursor(row=table.row_count - 1, animate=False)
        table.scroll_end(animate=False)
        if self._unseen:
            self._unseen = 0
            self._refresh_status()

    def action_toggle_polling(self) -> None:
        self._mode = Mode.PAUSED if self._mode == Mode.LIVE else Mode.LIVE
        if self._mode == Mode.LIVE:
            table = self.query_one("#log-table", DataTable)
            if table.row_count > 0:
                table.move_cursor(row=table.row_count - 1, animate=False)
            table.scroll_end(animate=False)
        self._refresh_status()

    def action_reload(self) -> None:
        # Refetches the initial window with whatever filters are active.
        self.run_worker(self._reload_live(), exclusive=True)

    def action_open_detail(self) -> None:
        # DataTable owns the `enter` key and emits RowSelected (handled below);
        # the binding here just keeps "details" visible in the footer hint.
        idx = self._cursor_index()
        if idx is None:
            return
        self.push_screen(DetailScreen(self._buffer[idx], self._obs))

    @on(DataTable.RowSelected, "#log-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or not 0 <= idx < len(self._buffer):
            return
        self.push_screen(DetailScreen(self._buffer[idx], self._obs))

    @on(DataTable.RowHighlighted, "#log-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#log-table", DataTable)
        self._marked_row = _apply_marker(
            table, self._marked_row, event.cursor_row, _is_dawn(self.theme),
        )
        # Reaching the absolute last row means the user caught up to the
        # tail — drop the "↓ N new" counter.
        if (
            self._unseen
            and event.cursor_row is not None
            and table.row_count > 0
            and event.cursor_row == table.row_count - 1
        ):
            self._unseen = 0
            self._refresh_status()
        # Scrolling up to the head of the buffer kicks off another history
        # page so the user can keep going back without thinking about it.
        if (
            self._mode == Mode.LIVE
            and not self._loading_older
            and not self._history_exhausted
            and event.cursor_row is not None
            and event.cursor_row <= _HISTORY_PREFETCH_ZONE
            and len(self._buffer) > 0
        ):
            self.run_worker(self._load_older(), exclusive=False)

    async def _load_older(self) -> None:
        """Fetch the next page of older entries and prepend them to the
        buffer. Cursor is shifted by the prepended count so the row the
        user was reading stays under their selection."""
        if self._loading_older or not self._buffer:
            return
        self._loading_older = True
        table = self.query_one("#log-table", DataTable)
        try:
            oldest_ts = _parse_ts(self._buffer[0].get("ts"))
            if oldest_ts is None:
                return
            f = build_filter(
                self._obs.project,
                services=self._selected,  # None ⇒ all
                until=oldest_ts,
                extras=self._active_clauses(),
                exclude_noise=True,
            )
            # Indicate the fetch in the status bar without obscuring the
            # table — set_loading() would cover the whole list and feel
            # disruptive for a background page fetch.
            self.query_one("#status-right", Static).update(
                "[#56526e italic]loading older…[/]"
            )
            try:
                raw = await asyncio.to_thread(
                    self._obs.logs.list_entries,
                    f, limit=_HISTORY_PAGE, ascending=False,
                )
            except Exception as exc:  # noqa: BLE001
                self.notify(f"older fetch failed: {exc}", severity="error")
                return
            # API returns descending; reverse to oldest→newest for buffer.
            fresh: list[dict] = []
            for entry in reversed(raw):
                key = getattr(entry, "insert_id", None) or id(entry)
                if key in self._seen:
                    continue
                self._seen.add(key)
                fresh.append(format_entries([entry])[0])
            if not fresh:
                # Cloud Logging returned nothing older — likely past the
                # retention window or no matching entries. Stop hammering.
                self._history_exhausted = True
                self.notify("no older logs", severity="information")
                return
            # Capture cursor row BEFORE we mutate the buffer so we can
            # re-pin the user to the same data row after re-render.
            prev_cursor = table.cursor_row
            self._buffer = fresh + self._buffer
            dawn = _is_dawn(self.theme)
            table.clear()
            self._marked_row = None
            for e in self._buffer:
                table.add_row(*_row(e, dawn))
            # Default to "below the freshly prepended rows" so the user's
            # cursor doesn't get teleported off-screen. `or 0` would land at
            # row `len(fresh)` which is back in the prefetch zone and could
            # re-trigger — using `len(fresh)` floor explicitly is fine but
            # we also defer the flag clear below as belt-and-suspenders.
            new_cursor = (
                prev_cursor + len(fresh)
                if prev_cursor is not None
                else len(fresh)
            )
            if 0 <= new_cursor < table.row_count:
                table.move_cursor(row=new_cursor, animate=False)
            if table.row_count > 0:
                self._marked_row = _apply_marker(
                    table, None, table.cursor_row, dawn,
                )
        finally:
            self._refresh_status()
            # Defer clearing the guard so the RowHighlighted event from
            # move_cursor above has time to dispatch (and find the guard
            # still True). Without this delay, that event can re-fire
            # _load_older if the new cursor lands back in the zone.
            self.set_timer(0.1, self._end_loading_older)

    def _end_loading_older(self) -> None:
        self._loading_older = False

    def action_open_trace(self) -> None:
        idx = self._cursor_index()
        if idx is None:
            return
        entry = self._buffer[idx]
        trace = entry.get("trace")
        if not trace:
            self.notify("no trace on this row", severity="warning")
            return
        self.push_screen(TraceScreen(trace, self._obs, anchor=entry))

    def action_open_context(self) -> None:
        idx = self._cursor_index()
        if idx is None:
            return
        entry = self._buffer[idx]
        if not entry.get("svc") or not entry.get("ts"):
            self.notify("no service/timestamp on this row", severity="warning")
            return
        self.push_screen(ContextScreen(entry, self._obs))

    def _cursor_index(self) -> Optional[int]:
        table = self.query_one("#log-table", DataTable)
        if table.row_count == 0:
            return None
        idx = table.cursor_row
        if idx is None or not 0 <= idx < len(self._buffer):
            return None
        return idx

    @work(exclusive=True)
    async def action_pick_services(self) -> None:
        # `@work` runs this in a Textual worker context, which is required for
        # `push_screen_wait` in Textual 8+. Without it: NoActiveWorker.
        if not self._all_services:
            try:
                self._all_services = await asyncio.to_thread(self._list_services)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"list_services failed: {exc}", severity="error")
                return
        current = (
            list(self._selected)
            if self._selected is not None
            else list(self._all_services)
        )
        picked = await self.push_screen_wait(
            ServicePicker(self._all_services, current)
        )
        if picked is None:
            return
        # Selecting everything ≡ "no service filter" — store as None so
        # build_filter omits the clause (cheaper, and survives services added
        # to the project after this session started).
        self._selected = (
            None if set(picked) == set(self._all_services) else picked
        )
        await self._reload_live()


def _parse_services(arg: Optional[str]) -> Optional[list[str]]:
    if not arg:
        return None
    return [s.strip() for s in arg.split(",") if s.strip()] or None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cloudlens-watch",
        description="Interactive Cloud Run log viewer (TUI).",
    )
    parser.add_argument(
        "-s", "--services",
        help="Comma-separated service names. Omit to tail every service.",
    )
    parser.add_argument(
        "--hours", type=float, default=0.5,
        help="Initial lookback window in hours (default 0.5).",
    )
    parser.add_argument(
        "-p", "--project",
        help="GCP project ID. Defaults to $GOOGLE_CLOUD_PROJECT / $GCP_PROJECT.",
    )
    args = parser.parse_args()

    project = (
        args.project
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
    )
    if not project:
        sys.exit("Set --project or GOOGLE_CLOUD_PROJECT to your GCP project ID.")

    app = CloudLensApp(
        obs=Observability(project),
        services=_parse_services(args.services),
        hours=max(args.hours, 0.05),
    )
    app.run()


if __name__ == "__main__":
    main()
