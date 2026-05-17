# cloudlens-watch (Go)

A Bubble Tea + Lip Gloss rewrite of the CloudLens TUI. Lives next to the
Python implementation (`cloudlens/tui.py`) — pick whichever feels better.

**Why a Go version:** single-binary distribution, instant cold-start, and
the live tail uses Cloud Logging's `TailLogEntries` server-streaming RPC
instead of polling — new entries arrive as the server writes them.

## Build

```sh
cd watch
go build -o cloudlens-watch
```

Or run without installing:

```sh
cd watch
go run . -p <project-id>
```

## Use

```sh
export GOOGLE_CLOUD_PROJECT=my-project
./cloudlens-watch                            # tail every Cloud Run service
./cloudlens-watch -s api,worker              # tail two services
./cloudlens-watch -s api --hours 2           # 2h initial lookback
```

Authentication uses Application Default Credentials — same as the Python
version. Run `gcloud auth application-default login` first.

## Modes

The status bar always shows one of:

- **● LIVE** — streaming tail is on. New rows append; the view follows the
  bottom unless you've scrolled up to read something, in which case an
  `↓ N new` counter ticks up and you press `g` to catch up.
- **⏸ PAUSED** — streaming stopped. Buffer is frozen.
- **🔍 SEARCH** — buffer is the last server-side search result. Streaming
  is paused. `esc` returns to LIVE.

## Keys

### Main view

| key | action |
|-----|--------|
| `enter` | open detail view for the selected row |
| `c` | context — same service, ±25 entries around this row |
| `t` | trace drill-down — cross-service stitch |
| `y` | share — copy this row as an agent-ready brief |
| `/` | search Cloud Logging (server-side; scoped to current services) |
| `s` | service picker (`space` toggle · `a` all · `n` none · `enter` apply) |
| `space` | pause / resume live polling |
| `g` / `G` / `end` | jump to latest (clear unread) |
| `home` | top of buffer |
| `↑↓` `jk` `pgup` `pgdn` | scroll |
| `T` | toggle midnight / dawn theme |
| `r` | reload from server |
| `?` | help (lists every shortcut, reachable from any modal) |
| `q` | quit |

### Inside detail / trace / context

`enter` details · `c` context · `t` trace · `y` share · `esc/q` back.
Recursive drills work: trace → context → another trace → back back back.

## The `y` share — what gets copied

A markdown brief tuned for an agent that has the `cloudlens` MCP registered.
Includes:

- the raw log facts (project, time, service+revision, severity, http, trace,
  full untruncated message)
- a Cloud Logging Explorer deep link
- a list of *literal* CloudLens MCP tool calls with arguments pre-filled:
  `get_logs_by_trace("…")`, `get_health("…")`, `diff_windows("…")`, etc.

Three shapes depending on where you press `y`:

- main view / detail view → single entry
- trace view → the full cross-service stitch
- context view → the anchor + ±25 window

Clipboard goes via OSC 52 — works in iTerm2, kitty, Alacritty, Windows
Terminal, recent gnome-terminal, and VS Code's integrated terminal.

## Themes

Two palettes, both rebuilt at runtime when you press `T`:

- **cloudlens-midnight** *(default)* — deep navy with sky-cyan brand,
  mint-teal accent, lavender secondary
- **cloudlens-dawn** — paper white with ocean-blue brand, deep cyan accent,
  rich violet secondary

The `t` column marks rows that have a trace ID (`●`) vs ones that don't
(`·`) — Cloud Run only attaches trace IDs to entries logged inside an HTTP
request context, so background / startup logs won't have them.

## Layout

```
watch/
├── go.mod
├── main.go        CLI flags + Bubble Tea program boot
├── theme.go       Midnight + Dawn palettes + applyTheme rebinder
├── overlay.go     overlay interface + centerOverlay framing
├── gcp.go         Cloud Logging filter, initial fetch, stream,
│                    trace stitch, before/after context, search
├── run.go         Cloud Run service listing for the picker
├── share.go       Markdown briefs (entry/trace/context) + OSC 52 clipboard
├── model.go       Bubble Tea root — main view + state machine
├── help.go        Help modal (?)
├── detail.go      Detail modal (enter)
├── picker.go      Service picker modal (s)
├── trace.go       Trace drill modal (t)
└── context.go     Service-focused context modal (c)
```

Streaming pattern: one goroutine runs `streamTail`, pushing batches onto a
buffered channel. A blocking `tea.Cmd` reads one batch, returns it as a
`batchMsg`, and `Update` re-schedules the read. Same shape for errors. The
context cancels on quit (or when you change service scope) so the goroutine
exits cleanly and a fresh stream is re-established.

Modals all implement a single `overlay` interface — `Update(msg, *model)`
returns a Cmd and a `close` flag. Overlays can replace themselves with
another overlay (detail → trace, trace → context, etc.) by writing to
`m.overlay` directly. One modal stack, infinite recursive drills.
