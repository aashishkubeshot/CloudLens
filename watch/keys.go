// keyMap declares every keybinding as a `key.Binding` so the bubbles/help
// widget can auto-format the footer. ShortHelp drives the always-visible
// bar; FullHelp would drive the expanded `?` overlay (we use our own help
// modal instead, so we keep FullHelp identical to ShortHelp for now).
package main

import "github.com/charmbracelet/bubbles/key"

type keyMap struct {
	Quit     key.Binding
	Search   key.Binding
	Services key.Binding
	Details  key.Binding
	Trace    key.Binding
	Context  key.Binding
	Share    key.Binding
	Pause    key.Binding
	Theme    key.Binding
	Help     key.Binding
}

func newKeyMap() keyMap {
	return keyMap{
		Quit:     key.NewBinding(key.WithKeys("q", "ctrl+c"), key.WithHelp("q", "quit")),
		Search:   key.NewBinding(key.WithKeys("/"), key.WithHelp("/", "search")),
		Services: key.NewBinding(key.WithKeys("s"), key.WithHelp("s", "services")),
		Details:  key.NewBinding(key.WithKeys("enter"), key.WithHelp("↩", "details")),
		Trace:    key.NewBinding(key.WithKeys("t"), key.WithHelp("t", "trace")),
		Context:  key.NewBinding(key.WithKeys("c"), key.WithHelp("c", "context")),
		Share:    key.NewBinding(key.WithKeys("y"), key.WithHelp("y", "share")),
		Pause:    key.NewBinding(key.WithKeys(" ", "space"), key.WithHelp("␣", "pause")),
		Theme:    key.NewBinding(key.WithKeys("T"), key.WithHelp("T", "theme")),
		Help:     key.NewBinding(key.WithKeys("?"), key.WithHelp("?", "help")),
	}
}

// ShortHelp drives the always-visible footer bar.
func (k keyMap) ShortHelp() []key.Binding {
	return []key.Binding{
		k.Quit, k.Search, k.Services, k.Details,
		k.Trace, k.Context, k.Share, k.Pause, k.Theme, k.Help,
	}
}

// FullHelp drives the expanded overlay (unused — we have our own help modal).
func (k keyMap) FullHelp() [][]key.Binding {
	return [][]key.Binding{
		{k.Quit, k.Search, k.Services, k.Details, k.Trace},
		{k.Context, k.Share, k.Pause, k.Theme, k.Help},
	}
}
