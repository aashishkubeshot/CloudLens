// Service-focused context view: 25 entries before + 25 after an anchor,
// scoped to the anchor's service, with the cursor pre-positioned on the
// anchor. Two parallel queries via errgroup so it loads in one RTT.
package main

import (
	"fmt"
	"strings"
	"sync"

	"charm.land/lipgloss/v2"
	tea "github.com/charmbracelet/bubbletea"
)

const (
	ctxBefore        = 25
	ctxAfter         = 25
	ctxWindowMinutes = 30.0
)

type contextLoadedMsg struct {
	anchorInsertID string
	entries        []logEntry
	err            error
}

type contextModal struct {
	anchor   logEntry
	entries  []logEntry
	cursor   int
	viewport int
	loading  bool
	loadErr  error
}

func newContextModal(anchor logEntry, _ *model) *contextModal {
	return &contextModal{anchor: anchor, loading: true}
}

func (c *contextModal) load(m *model) tea.Cmd {
	anchor := c.anchor
	return func() tea.Msg {
		var (
			wg              sync.WaitGroup
			before, after   []logEntry
			beforeErr, afterErr error
		)
		wg.Add(2)
		go func() {
			defer wg.Done()
			before, beforeErr = getContextBefore(m.ctx, m.project, anchor.Service, anchor.Time, ctxWindowMinutes, ctxBefore)
		}()
		go func() {
			defer wg.Done()
			after, afterErr = getContextAfter(m.ctx, m.project, anchor.Service, anchor.Time, ctxWindowMinutes, ctxAfter)
		}()
		wg.Wait()
		if beforeErr != nil {
			return contextLoadedMsg{anchorInsertID: anchor.InsertID, err: beforeErr}
		}
		if afterErr != nil {
			return contextLoadedMsg{anchorInsertID: anchor.InsertID, err: afterErr}
		}
		// `before` is descending; reverse to ascending. Merge → one stream.
		reverse(before)
		merged := append(before, after...)
		// Dedup by InsertID; the anchor instant can land in both halves.
		seen := make(map[string]struct{}, len(merged))
		out := merged[:0]
		for _, e := range merged {
			if e.InsertID != "" {
				if _, ok := seen[e.InsertID]; ok {
					continue
				}
				seen[e.InsertID] = struct{}{}
			}
			out = append(out, e)
		}
		return contextLoadedMsg{anchorInsertID: anchor.InsertID, entries: out}
	}
}

func (c *contextModal) Update(msg tea.Msg, m *model) (tea.Cmd, bool) {
	switch msg := msg.(type) {
	case contextLoadedMsg:
		if msg.anchorInsertID != c.anchor.InsertID {
			return nil, false
		}
		c.loading = false
		if msg.err != nil {
			c.loadErr = msg.err
			return nil, false
		}
		c.entries = msg.entries
		// Land cursor on the anchor row.
		c.cursor = c.anchorIndex()
		c.scrollToCursor(20)

	case tea.KeyMsg:
		switch msg.String() {
		case "esc", "q":
			return nil, true
		case "?":
			m.overlay = newHelpModal()
			return nil, false
		case "y":
			copyToClipboard(formatContextShare(c.anchor, c.entries, m.project))
			m.flash = fmt.Sprintf("copied context (%d entries) — paste into your agent", len(c.entries))
			return nil, false
		case "enter":
			if e := c.cursorEntry(); e != nil {
				m.overlay = newDetailModal(*e)
			}
			return nil, false
		case "t":
			if e := c.cursorEntry(); e != nil && e.Trace != "" {
				m.overlay = newTraceModal(e.Trace, m)
				return m.overlay.(*traceModal).load(m), false
			}
			m.flash = "no trace on this row"
			return nil, false
		case "c":
			if e := c.cursorEntry(); e != nil && e.Service != "" && !e.Time.IsZero() {
				m.overlay = newContextModal(*e, m)
				return m.overlay.(*contextModal).load(m), false
			}
			return nil, false
		case "up", "k":
			c.moveCursor(-1)
		case "down", "j":
			c.moveCursor(1)
		case "pgup":
			c.moveCursor(-10)
		case "pgdown":
			c.moveCursor(10)
		case "home":
			c.cursor, c.viewport = 0, 0
		case "end":
			c.cursor = max(0, len(c.entries)-1)
		}
	}
	return nil, false
}

func (c *contextModal) anchorIndex() int {
	for i, e := range c.entries {
		if c.anchor.InsertID != "" && e.InsertID == c.anchor.InsertID {
			return i
		}
	}
	for i, e := range c.entries {
		if e.Time.Equal(c.anchor.Time) && e.Message == c.anchor.Message {
			return i
		}
	}
	return 0
}

func (c *contextModal) cursorEntry() *logEntry {
	if c.cursor < 0 || c.cursor >= len(c.entries) {
		return nil
	}
	return &c.entries[c.cursor]
}

func (c *contextModal) moveCursor(delta int) {
	if len(c.entries) == 0 {
		return
	}
	c.cursor = clamp(c.cursor+delta, 0, len(c.entries)-1)
}

func (c *contextModal) scrollToCursor(visibleRows int) {
	if c.cursor < c.viewport {
		c.viewport = c.cursor
	} else if c.cursor >= c.viewport+visibleRows {
		c.viewport = c.cursor - visibleRows + 1
	}
}

func (c *contextModal) View(w, h int) string {
	inner := w - 6
	if inner > 140 {
		inner = 140
	}

	bannerTitle := fmt.Sprintf("CONTEXT · %s · around %s · ±%d entries",
		c.anchor.Service,
		c.anchor.Time.Local().Format("15:04:05.000"),
		ctxBefore,
	)

	visibleRows := h - 8
	if visibleRows < 5 {
		visibleRows = 5
	}

	var body string
	switch {
	case c.loadErr != nil:
		body = errorBannerStyle.Render("context fetch failed: " + c.loadErr.Error())
	case c.loading:
		body = loadingStyle.Render("… loading entries before & after the anchor")
	case len(c.entries) == 0:
		body = dimStyle.Render("no entries in the surrounding window")
	default:
		c.scrollToCursor(visibleRows)
		end := c.viewport + visibleRows
		if end > len(c.entries) {
			end = len(c.entries)
		}
		anchorIdx := c.anchorIndex()
		var lines []string
		for i := c.viewport; i < end; i++ {
			lines = append(lines, renderContextRow(c.entries[i], inner, i == c.cursor, i == anchorIdx))
		}
		body = strings.Join(lines, "\n")
	}

	hints := joinKeyHints(
		[2]string{"esc/q", "back"},
		[2]string{"enter", "details"},
		[2]string{"t", "trace"},
		[2]string{"c", "context"},
		[2]string{"y", "share context"},
	)
	return centerOverlay(modalCard(bannerTitle, body+"\n\n"+hints, inner), w, h)
}

// Constant width for the "← OPENED FROM" anchor label on the right.
const anchorLabelW = 16

func renderContextRow(e logEntry, w int, selected, anchor bool) string {
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

	// Two-cell left marker: pink ▌ on the ANCHOR row (the row the user
	// pressed `c` on to open this context view). This is the explicit
	// "you opened context from this row" signal. Cursor selection is still
	// signaled via the row bg lift.
	marker := "  "
	if anchor {
		marker = lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render("▌ ")
	}

	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	msg = strings.ReplaceAll(msg, "\n", " ")

	// Reserve right-edge space for the "← OPENED FROM" label even on
	// non-anchor rows so column widths line up consistently across rows.
	mw := w - 2 - 12 - 2 - 2 - 2 - 2 - 2 - anchorLabelW - 2
	if mw < 10 {
		mw = 10
	}
	msg = truncate(msg, mw)

	var rightCell string
	if anchor {
		rightCell = lipgloss.NewStyle().
			Foreground(active.Primary).
			Bold(true).
			Width(anchorLabelW).
			Align(lipgloss.Right).
			Render("← OPENED FROM")
	} else {
		rightCell = strings.Repeat(" ", anchorLabelW)
	}

	cells := lipgloss.JoinHorizontal(lipgloss.Left,
		dimStyle.Render(fixed(timeStr, 12)), "  ",
		sevSt.Render(fixed(sevGlyph, 2)), " ",
		traceStyle.Render(fixed(traceGlyph, 2)), " ",
		fixed(msg, mw), "  ",
		rightCell,
	)
	if selected {
		return marker + rowSelectedStyle.Width(w-2).Render(cells)
	}
	return marker + rowStyle.Width(w-2).Render(cells)
}
