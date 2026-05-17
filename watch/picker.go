// Service picker modal. On open: fires a Cmd to list services. The list
// shows up with checkboxes — space toggles, a/n select all/none, enter
// applies (writes back to model.services and triggers a reload), esc cancels.
package main

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"charm.land/lipgloss/v2"
)

type servicesLoadedMsg struct {
	services []string
	err      error
}

type pickerModal struct {
	all       []string
	selected  map[string]struct{}
	cursor    int
	loading   bool
	loadError error
}

func newPickerModal(currentSelected []string, all []string) *pickerModal {
	sel := make(map[string]struct{}, len(currentSelected))
	for _, s := range currentSelected {
		sel[s] = struct{}{}
	}
	// If we don't yet have the full service list, the picker starts in loading
	// state and Update will populate `all` when servicesLoadedMsg arrives.
	loading := len(all) == 0
	if !loading && len(currentSelected) == 0 {
		// "All services" mode: pre-check everything so unticking is intuitive.
		for _, s := range all {
			sel[s] = struct{}{}
		}
	}
	return &pickerModal{
		all:      all,
		selected: sel,
		loading:  loading,
	}
}

func (p *pickerModal) loadCmd(m *model) tea.Cmd {
	return func() tea.Msg {
		services, err := listServices(m.ctx, m.project)
		return servicesLoadedMsg{services: services, err: err}
	}
}

func (p *pickerModal) Update(msg tea.Msg, m *model) (tea.Cmd, bool) {
	switch msg := msg.(type) {
	case servicesLoadedMsg:
		p.loading = false
		if msg.err != nil {
			p.loadError = msg.err
			return nil, false
		}
		p.all = msg.services
		// Default selection: if model has no services set ("all"), mark every
		// service as ticked so it's easy to deselect a couple.
		if len(m.services) == 0 {
			for _, s := range p.all {
				p.selected[s] = struct{}{}
			}
		}
		return nil, false

	case tea.KeyMsg:
		switch msg.String() {
		case "esc", "q":
			return nil, true
		case "?":
			m.overlay = newHelpModal()
			return nil, false
		case "enter":
			picked := make([]string, 0, len(p.selected))
			for _, s := range p.all {
				if _, ok := p.selected[s]; ok {
					picked = append(picked, s)
				}
			}
			// All selected ≡ no service filter; store as nil.
			if len(picked) == len(p.all) {
				m.services = nil
			} else {
				m.services = picked
			}
			cmd := m.reloadAfterScopeChange()
			return cmd, true
		case "up", "k":
			if p.cursor > 0 {
				p.cursor--
			}
		case "down", "j":
			if p.cursor < len(p.all)-1 {
				p.cursor++
			}
		case "pgup":
			p.cursor = max(0, p.cursor-10)
		case "pgdown":
			p.cursor = min(len(p.all)-1, p.cursor+10)
		case "home":
			p.cursor = 0
		case "end":
			p.cursor = max(0, len(p.all)-1)
		case " ", "space":
			if len(p.all) == 0 {
				return nil, false
			}
			svc := p.all[p.cursor]
			if _, on := p.selected[svc]; on {
				delete(p.selected, svc)
			} else {
				p.selected[svc] = struct{}{}
			}
		case "a":
			for _, s := range p.all {
				p.selected[s] = struct{}{}
			}
		case "n":
			p.selected = map[string]struct{}{}
		}
	}
	return nil, false
}

func (p *pickerModal) View(w, h int) string {
	inner := 50
	if w < inner+8 {
		inner = max(20, w-8)
	}

	title := modalTitleStyle.Render(
		fmt.Sprintf("services  ·  %d of %d selected", len(p.selected), len(p.all)),
	)

	var body string
	switch {
	case p.loadError != nil:
		body = errorBannerStyle.Render("list_services failed: "+p.loadError.Error()) +
			"\n\n" + dimStyle.Render("esc/q back")
	case p.loading:
		body = loadingStyle.Render("… loading services from Cloud Run")
	case len(p.all) == 0:
		body = dimStyle.Render("no Cloud Run services in this project")
	default:
		visibleRows := h - 10
		if visibleRows < 5 {
			visibleRows = 5
		}
		top := 0
		if p.cursor >= visibleRows {
			top = p.cursor - visibleRows + 1
		}
		end := top + visibleRows
		if end > len(p.all) {
			end = len(p.all)
		}
		var b strings.Builder
		for i := top; i < end; i++ {
			svc := p.all[i]
			mark := checkmarkOffStyle.Render("☐")
			if _, ok := p.selected[svc]; ok {
				mark = checkmarkOnStyle.Render("☑")
			}
			line := mark + "  " + lipgloss.NewStyle().Foreground(colorForService(svc)).Render(svc)
			if i == p.cursor {
				line = rowSelectedStyle.Width(inner).Render(line)
			} else {
				line = rowStyle.Width(inner).Render(line)
			}
			b.WriteString(line + "\n")
		}
		body = strings.TrimRight(b.String(), "\n")
	}

	hints := joinKeyHints(
		[2]string{"space", "toggle"},
		[2]string{"a", "all"},
		[2]string{"n", "none"},
		[2]string{"enter", "apply"},
		[2]string{"esc", "cancel"},
	)
	_ = title // banner replaces the inline title
	bannerTitle := fmt.Sprintf("SERVICES · %d of %d selected", len(p.selected), len(p.all))
	return centerOverlay(modalCard(bannerTitle, body+"\n\n"+hints, inner), w, h)
}
