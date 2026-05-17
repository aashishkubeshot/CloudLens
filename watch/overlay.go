// Modal/overlay infrastructure. The root model has an `overlay` field. When
// it's non-nil:
//   - tea.KeyMsg is routed to overlay.Update(msg, m) first
//   - View renders overlay.View(w, h) on top of (replacing) the main view
//
// Overlay.Update returns (cmd, close). If close is true, the root sets
// overlay = nil. The overlay can mutate the root model directly via the
// *model parameter — that's how the picker writes back selected services
// and how trace/context drills push new overlays on top.
package main

import (
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"charm.land/lipgloss/v2"
)

type overlay interface {
	Update(msg tea.Msg, m *model) (tea.Cmd, bool)
	View(w, h int) string
}

// centerOverlay frames a child string inside a panel, centered on a w×h area
// filled with the base background. Used by every full-screen modal.
func centerOverlay(child string, w, h int) string {
	bg := lipgloss.NewStyle().Background(active.BgBase).Width(w).Height(h)
	return bg.Render(
		lipgloss.Place(w, h, lipgloss.Center, lipgloss.Center, child),
	)
}

// modalBanner renders a strong title strip — primary bg, base fg, bold,
// padded — for the top of every modal. Same width as the modal body so it
// reads as one continuous header band.
func modalBanner(title string, w int) string {
	return lipgloss.NewStyle().
		Background(active.Primary).
		Foreground(active.BgBase).
		Bold(true).
		Padding(0, 2).
		Width(w).
		Render("▎ " + title)
}

// modalCard wraps a banner + body in the rounded panel. Pass the panel
// inner width so the banner fills edge-to-edge.
func modalCard(title, body string, innerW int) string {
	banner := modalBanner(title, innerW)
	gap := lipgloss.NewStyle().Width(innerW).Render("")
	return panelStyle.Render(banner + "\n" + gap + "\n" + body)
}

// joinKeyHints renders "[k] label · [k2] label2 …" for footer / title hints.
func joinKeyHints(pairs ...[2]string) string {
	parts := make([]string, 0, len(pairs)*2)
	for i, p := range pairs {
		if i > 0 {
			parts = append(parts, dimStyle.Render("·"))
		}
		parts = append(parts, modalKeyStyle.Render(p[0])+" "+p[1])
	}
	return strings.Join(parts, " ")
}
