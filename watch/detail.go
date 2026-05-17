// Detail modal — full view of one log entry. Layout per the pencil mockup:
//
//   LOG ENTRY  ·  ● SEVERITY           (tiny uppercase badge)
//
//   ●  SEVERITY                        (severity dot + word, severity color, bold)
//   service-name                       (big, service color)
//   14:33:07.224  ·  May 12 2026  ·  rev xxx   (dim subtitle)
//
//   ──────────────────────────────────
//
//   SEVERITY    ERROR                  (label uppercase muted · value)
//   HTTP        POST /api/users 500
//   TRACE       abc123...
//
//   MESSAGE
//   ┌──────────────────────────────────┐
//   │ ConnectionError: timeout ...     │  (scrollable inset card)
//   └──────────────────────────────────┘
//
//   esc/q back  ·  y share  ·  c context  ·  t trace
package main

import (
	"fmt"
	"strings"

	"charm.land/lipgloss/v2"
	tea "github.com/charmbracelet/bubbletea"
)

type detailModal struct {
	entry  logEntry
	scroll int // top line of the message body shown
}

func newDetailModal(e logEntry) *detailModal { return &detailModal{entry: e} }

func (d *detailModal) Update(msg tea.Msg, m *model) (tea.Cmd, bool) {
	k, ok := msg.(tea.KeyMsg)
	if !ok {
		return nil, false
	}
	switch k.String() {
	case "esc", "q":
		return nil, true
	case "?":
		m.overlay = newHelpModal()
		return nil, false
	case "y":
		copyToClipboard(formatEntryShare(d.entry, m.project))
		m.flash = "copied — paste into your agent"
		return nil, false
	case "t":
		if d.entry.Trace != "" {
			m.overlay = newTraceModal(d.entry.Trace, m)
			return m.overlay.(*traceModal).load(m), false
		}
		m.flash = "no trace on this entry"
		return nil, false
	case "c":
		if d.entry.Service != "" && !d.entry.Time.IsZero() {
			m.overlay = newContextModal(d.entry, m)
			return m.overlay.(*contextModal).load(m), false
		}
		return nil, false
	case "up", "k":
		if d.scroll > 0 {
			d.scroll--
		}
		return nil, false
	case "down", "j":
		d.scroll++
		return nil, false
	}
	return nil, false
}

func (d *detailModal) View(w, h int) string {
	e := d.entry
	sev := defaultStr(e.Severity, "DEFAULT")
	sevGlyph, sevSt := severityDot(sev)

	// Content width: pad in from screen edges, cap so lines don't wrap to
	// ridiculous widths on large terminals.
	innerW := w - 24
	if innerW > 96 {
		innerW = 96
	}
	if innerW < 40 {
		innerW = 40
	}

	muted := lipgloss.NewStyle().Foreground(active.FgMuted)
	base := lipgloss.NewStyle().Foreground(active.FgBase)
	sepDot := muted.Render("  ·  ")

	// --- 1. Tiny "log entry" label (lowercase, dim) — the only signal
	// telling you what this screen is. No competing severity badge here.
	label := muted.Render("log entry")

	// --- 2. Severity headline: dot + word in severity color, bold.
	// No spaceOut tricks. Standalone on its own line is the emphasis.
	headline := sevSt.Bold(true).Render(sevGlyph + "  " + sev)

	// --- 3. Service name in service-color, bold.
	svcStyle := lipgloss.NewStyle().Foreground(colorForService(e.Service)).Bold(true)
	svcLine := svcStyle.Render(defaultStr(e.Service, "—"))

	// --- 4. Two dim subtitle lines — time on one, rev on the next so
	// neither feels cramped against the headline.
	var timeLine, revLine string
	if !e.Time.IsZero() {
		timeLine = muted.Render(
			e.Time.Local().Format("15:04:05.000") + sepDot +
				e.Time.Local().Format("Jan 2, 2006 MST"))
	}
	if e.Revision != "" {
		revLine = muted.Render("rev " + e.Revision)
	}

	// --- 5. Short hairline divider (not full-width) in a visible dim color.
	divW := 24
	if divW > innerW {
		divW = innerW
	}
	divider := muted.Render(strings.Repeat("─", divW))

	// --- 6. Meta rows. Lowercase muted labels (modern), plain values.
	metaLabel := func(s string) string {
		return muted.Width(10).Render(s)
	}
	var metaRows []string
	metaRows = append(metaRows,
		metaLabel("severity")+sevSt.Render(sev))
	if e.HTTP != "" {
		metaRows = append(metaRows,
			metaLabel("http")+base.Render(e.HTTP))
	}
	if e.Trace != "" {
		metaRows = append(metaRows,
			metaLabel("trace")+lipgloss.NewStyle().Foreground(active.Secondary).Render(e.Trace))
	}

	// --- 7. Message: plain wrapped text, no inset card, no border.
	// Let it breathe on the surface itself.
	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	if msg == "" {
		msg = "(no message)"
	}
	wrapped := lipgloss.NewStyle().Width(innerW).Render(msg)
	wrappedLines := strings.Split(wrapped, "\n")

	chromeLines := 18 + len(metaRows)
	maxBody := h - chromeLines
	if maxBody < 5 {
		maxBody = 5
	}
	if d.scroll > max(0, len(wrappedLines)-maxBody) {
		d.scroll = max(0, len(wrappedLines)-maxBody)
	}
	end := d.scroll + maxBody
	if end > len(wrappedLines) {
		end = len(wrappedLines)
	}
	visible := strings.Join(wrappedLines[d.scroll:end], "\n")

	scrollHint := ""
	if len(wrappedLines) > maxBody {
		scrollHint = "  " + muted.Render(
			fmt.Sprintf("(↑↓ %d/%d)", d.scroll+maxBody, len(wrappedLines)))
	}
	msgLabel := muted.Render("message") + scrollHint

	// --- 8. Footer hints — single dim line. No pills, no boxes.
	hints := []string{footerKey("esc", "back"), footerKey("y", "share")}
	if e.Service != "" && !e.Time.IsZero() {
		hints = append(hints, footerKey("c", "context"))
	}
	if e.Trace != "" {
		hints = append(hints, footerKey("t", "trace"))
	}
	footer := strings.Join(hints, muted.Render("   "))

	// --- Compose with disciplined whitespace ---
	parts := []string{
		label,
		"",
		headline,
		svcLine,
	}
	if timeLine != "" {
		parts = append(parts, "", timeLine)
	}
	if revLine != "" {
		parts = append(parts, revLine)
	}
	parts = append(parts,
		"",
		divider,
		"",
		strings.Join(metaRows, "\n"),
		"",
		divider,
		"",
		msgLabel,
		"",
		visible,
		"",
		divider,
		"",
		footer,
	)
	body := strings.Join(parts, "\n")

	// Thin rounded border in muted-gray so the modal reads as its own
	// surface without the loud-pink frame fighting the content. Slight
	// surface bg tint differentiates it from the underlying view.
	frame := lipgloss.NewStyle().
		Background(active.BgSurface).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(active.BgBoost).
		Padding(1, 4).
		Render(body)
	return centerOverlay(frame, w, h)
}

// footerKey renders a single "k label" hint pair: pink key, muted label.
func footerKey(k, label string) string {
	return lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render(k) +
		"  " + lipgloss.NewStyle().Foreground(active.FgMuted).Render(label)
}
