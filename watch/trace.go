// Trace drill-down modal: every entry for a trace_id, oldest → newest,
// across services. The cross-service stitch is the high-leverage debug move.
package main

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"charm.land/lipgloss/v2"
)

type traceLoadedMsg struct {
	traceID string
	entries []logEntry
	err     error
}

type traceModal struct {
	traceID  string
	entries  []logEntry
	cursor   int
	viewport int
	loading  bool
	loadErr  error
}

func newTraceModal(traceID string, _ *model) *traceModal {
	return &traceModal{traceID: traceID, loading: true}
}

func (t *traceModal) load(m *model) tea.Cmd {
	return func() tea.Msg {
		entries, err := getLogsByTrace(m.ctx, m.project, t.traceID, 24, 500)
		return traceLoadedMsg{traceID: t.traceID, entries: entries, err: err}
	}
}

func (t *traceModal) Update(msg tea.Msg, m *model) (tea.Cmd, bool) {
	switch msg := msg.(type) {
	case traceLoadedMsg:
		if msg.traceID != t.traceID {
			return nil, false
		}
		t.loading = false
		if msg.err != nil {
			t.loadErr = msg.err
			return nil, false
		}
		t.entries = msg.entries

	case tea.KeyMsg:
		switch msg.String() {
		case "esc", "q":
			return nil, true
		case "?":
			m.overlay = newHelpModal()
			return nil, false
		case "y":
			copyToClipboard(formatTraceShare(t.traceID, t.entries, m.project))
			m.flash = fmt.Sprintf("copied trace (%d entries) — paste into your agent", len(t.entries))
			return nil, false
		case "enter":
			if e := t.cursorEntry(); e != nil {
				m.overlay = newDetailModal(*e)
			}
			return nil, false
		case "c":
			if e := t.cursorEntry(); e != nil && e.Service != "" && !e.Time.IsZero() {
				m.overlay = newContextModal(*e, m)
				return m.overlay.(*contextModal).load(m), false
			}
			return nil, false
		case "up", "k":
			t.moveCursor(-1)
		case "down", "j":
			t.moveCursor(1)
		case "pgup":
			t.moveCursor(-10)
		case "pgdown":
			t.moveCursor(10)
		case "home":
			t.cursor, t.viewport = 0, 0
		case "end":
			t.cursor = max(0, len(t.entries)-1)
		}
	}
	return nil, false
}

func (t *traceModal) cursorEntry() *logEntry {
	if t.cursor < 0 || t.cursor >= len(t.entries) {
		return nil
	}
	return &t.entries[t.cursor]
}

func (t *traceModal) moveCursor(delta int) {
	if len(t.entries) == 0 {
		return
	}
	t.cursor = clamp(t.cursor+delta, 0, len(t.entries)-1)
	// Keep cursor in viewport (handled in View; viewport derived from cursor).
}

func (t *traceModal) View(w, h int) string {
	inner := w - 6
	if inner > 140 {
		inner = 140
	}
	bannerTitle := fmt.Sprintf("TRACE · %s · %d entries", t.traceID, len(t.entries))

	visibleRows := h - 8
	if visibleRows < 5 {
		visibleRows = 5
	}

	var body string
	switch {
	case t.loadErr != nil:
		body = errorBannerStyle.Render("trace fetch failed: " + t.loadErr.Error())
	case t.loading:
		body = loadingStyle.Render("… loading trace from Cloud Logging")
	case len(t.entries) == 0:
		body = dimStyle.Render(fmt.Sprintf("no entries for trace %s in last 24h", t.traceID))
	default:
		// Keep cursor in viewport.
		if t.cursor < t.viewport {
			t.viewport = t.cursor
		} else if t.cursor >= t.viewport+visibleRows {
			t.viewport = t.cursor - visibleRows + 1
		}
		end := t.viewport + visibleRows
		if end > len(t.entries) {
			end = len(t.entries)
		}
		var lines []string
		for i := t.viewport; i < end; i++ {
			lines = append(lines, renderTraceRow(t.entries[i], inner, i == t.cursor))
		}
		body = strings.Join(lines, "\n")
	}

	hints := joinKeyHints(
		[2]string{"esc/q", "back"},
		[2]string{"enter", "details"},
		[2]string{"c", "context"},
		[2]string{"y", "share trace"},
	)
	return centerOverlay(modalCard(bannerTitle, body+"\n\n"+hints, inner), w, h)
}

func renderTraceRow(e logEntry, w int, selected bool) string {
	timeStr := "            "
	if !e.Time.IsZero() {
		timeStr = e.Time.Local().Format("15:04:05.000")
	}
	sevGlyph, sevSt := severityDot(defaultStr(e.Severity, "DEFAULT"))

	traceGlyph := "·"
	traceStyle := dimStyle
	if e.Trace != "" {
		traceGlyph = "●"
		traceStyle = lipgloss.NewStyle().Foreground(active.Secondary)
	}
	svc := truncate(e.Service, 24)
	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	msg = strings.ReplaceAll(msg, "\n", " ")

	marker := "  "
	if selected {
		marker = lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render("▌ ")
	}

	mw := w - 2 - 12 - 2 - 2 - 2 - 2 - 24 - 2 - 4
	if mw < 10 {
		mw = 10
	}
	msg = truncate(msg, mw)
	svcStyle := lipgloss.NewStyle().Foreground(colorForService(e.Service))

	cells := lipgloss.JoinHorizontal(lipgloss.Left,
		dimStyle.Render(fixed(timeStr, 12)), "  ",
		sevSt.Render(fixed(sevGlyph, 2)), " ",
		traceStyle.Render(fixed(traceGlyph, 2)), " ",
		svcStyle.Render(fixed(svc, 24)), " ",
		fixed(msg, mw),
	)
	if selected {
		return marker + rowSelectedStyle.Width(w-2).Render(cells)
	}
	return marker + rowStyle.Width(w-2).Render(cells)
}
