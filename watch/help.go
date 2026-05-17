// Help modal. Per the design mockup: HELP label + dim close hint, hairline
// divider, then sections (MAIN VIEW keys, INSIDE MODALS, SERVICE PICKER,
// ROW MARKERS — with a real colored severity legend, THEMES).
package main

import (
	"image/color"
	"strings"

	"charm.land/lipgloss/v2"
	tea "github.com/charmbracelet/bubbletea"
)

type helpModal struct{}

func newHelpModal() *helpModal { return &helpModal{} }

func (h *helpModal) Update(msg tea.Msg, _ *model) (tea.Cmd, bool) {
	if k, ok := msg.(tea.KeyMsg); ok {
		switch k.String() {
		case "esc", "q", "?":
			return nil, true
		}
	}
	return nil, false
}

// section title — uppercase, dim, letter-spaced.
func helpSectionTitle(s string) string {
	return lipgloss.NewStyle().
		Foreground(active.FgMuted).
		Bold(true).
		Render(s)
}

// keyHint — `k <space> <label>` with the key in primary bold, label dim.
func helpKeyHint(k, label string) string {
	return lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render(k) +
		"  " + lipgloss.NewStyle().Foreground(active.FgBase).Render(label)
}

// severityLegendRow — colored dot + severity label list.
func sevLegendRow(glyph string, c color.Color, label, hint string) string {
	dot := lipgloss.NewStyle().Foreground(c).Bold(true).Render(glyph)
	lab := lipgloss.NewStyle().Foreground(active.FgBase).Render(label)
	if hint != "" {
		lab += lipgloss.NewStyle().Foreground(active.FgMuted).Render("  " + hint)
	}
	return dot + "   " + lab
}

func (h *helpModal) View(w, hh int) string {
	innerW := 70
	if w > 0 && w-12 < innerW {
		innerW = max(40, w-12)
	}

	// --- Title row ---
	title := lipgloss.NewStyle().
		Foreground(active.Primary).
		Bold(true).
		Render("HELP")
	subtitle := lipgloss.NewStyle().
		Foreground(active.FgMuted).
		Render("  keyboard shortcuts  ·  esc/q/? to close")

	// --- Divider ---
	divider := lipgloss.NewStyle().
		Foreground(active.BgPanel).
		Render(strings.Repeat("─", innerW))

	// --- Sections ---
	mainSection := strings.Join([]string{
		helpSectionTitle("MAIN VIEW"),
		"",
		helpKeyHint("↩", "open log details") + "       " +
			helpKeyHint("c", "context — same service, ±25 entries"),
		helpKeyHint("t", "trace drill-down — cross-service stitch"),
		helpKeyHint("y", "share — copy this row as an agent-ready brief"),
		helpKeyHint("/", "search Cloud Logging (server-side, scoped)"),
		helpKeyHint("s", "service picker") + "       " +
			helpKeyHint("␣", "pause / resume live tail"),
		helpKeyHint("g", "jump to latest, clear unread"),
		helpKeyHint("T", "toggle rose-pine-moon  ↔  rose-pine-dawn"),
		helpKeyHint("?", "this help") + "       " +
			helpKeyHint("q", "quit"),
	}, "\n")

	modalsSection := strings.Join([]string{
		helpSectionTitle("INSIDE DETAILS / TRACE / CONTEXT"),
		"",
		helpKeyHint("↩", "open details on the selected row"),
		helpKeyHint("c", "open context for this row") + "       " +
			helpKeyHint("t", "open trace drill for this row"),
		helpKeyHint("y", "share — context-aware (entry / trace stitch / window)"),
		helpKeyHint("esc/q", "back"),
	}, "\n")

	pickerSection := strings.Join([]string{
		helpSectionTitle("SERVICE PICKER"),
		"",
		helpKeyHint("␣", "toggle service") + "       " +
			helpKeyHint("a", "select all") + "       " +
			helpKeyHint("n", "select none"),
		helpKeyHint("↩", "apply") + "       " +
			helpKeyHint("esc", "cancel"),
	}, "\n")

	// --- Severity legend with colored dots ---
	legendSection := strings.Join([]string{
		helpSectionTitle("ROW MARKERS  ·  severity legend"),
		"",
		sevLegendRow("●", active.Error,
			"ERROR  ·  CRITICAL  ·  ALERT  ·  EMERGENCY", ""),
		sevLegendRow("▲", active.Warning,
			"WARNING", ""),
		sevLegendRow("●", active.Accent,
			"INFO  ·  NOTICE", ""),
		sevLegendRow("●", active.FgMuted,
			"DEFAULT  ·  DEBUG", ""),
		"",
		sevLegendRow("●", active.Secondary,
			"row has a trace ID",
			"— press t to drill into the cross-service stitch"),
		sevLegendRow("·", active.FgMuted,
			"no trace ID",
			"(startup / background logs)"),
	}, "\n")

	// --- Compose ---
	body := strings.Join([]string{
		title + subtitle,
		"",
		divider,
		"",
		mainSection,
		"",
		divider,
		"",
		modalsSection,
		"",
		pickerSection,
		"",
		divider,
		"",
		legendSection,
	}, "\n")

	card := lipgloss.NewStyle().
		Background(active.BgPanel).
		Foreground(active.FgBase).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(active.Primary).
		Padding(1, 3).
		Render(body)
	return centerOverlay(card, w, hh)
}
