// Bubble Tea root model. Owns the main streaming view; renders modals when
// `overlay` is non-nil. State machine for the main view:
//
//   modeLive    — streaming on, new entries auto-append (and auto-scroll
//                 only if the user is at the bottom)
//   modePaused  — streaming on but no auto-scroll, buffer frozen visually
//   modeSearch  — buffer = last server-side search result, streaming ignored
//
// Overlays (help, detail, picker, trace, context) live on a single
// `overlay` field — only one is open at a time. Overlays can replace
// themselves with another overlay (e.g. detail → trace) by writing to
// m.overlay directly.
package main

import (
	"context"
	"fmt"
	"image/color"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/bubbles/help"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	lg1 "github.com/charmbracelet/lipgloss" // v1 — bubbles widgets need this
)

type mode int

const (
	modeLive mode = iota
	modePaused
	modeSearch
)

const (
	bufferCap     = 4000
	bufferTrim    = 3000
	initialLimit  = 100
	batchChanSize = 64
	searchLimit   = 300
	searchHours   = 24.0
)

// --- Bubble Tea messages ---

type initialDoneMsg struct {
	entries []logEntry
	err     error
}

type batchMsg struct{ entries []logEntry }
type errMsg struct{ err error }
type searchDoneMsg struct {
	text    string
	entries []logEntry
	err     error
}
type clearFlashMsg struct{}

// --- Model ---

type model struct {
	// config
	project  string
	services []string
	hours    float64

	// streaming state
	entries  []logEntry
	seen     map[string]struct{}
	cursor   int
	viewport int
	width    int
	height   int
	mode     mode
	unseen   int
	loading  bool
	lastErr  error

	// streaming plumbing
	ctx     context.Context
	cancel  context.CancelFunc
	batches chan batchMsg
	errs    chan error

	// v2: overlays + search input + flash toast
	overlay       overlay
	flash         string
	searchActive  bool
	searchInput   textinput.Model
	searchText    string
	knownServices []string // populated lazily for the picker

	// bubbles widgets
	keys keyMap
	help help.Model
}

func newModel(project string, services []string, hours float64) *model {
	ctx, cancel := context.WithCancel(context.Background())

	ti := textinput.New()
	ti.Placeholder = "search Cloud Logging — text appears across textPayload + jsonPayload.message"
	ti.Prompt = "/ "
	ti.CharLimit = 200

	hp := help.New()
	applyBubblesStyles(&ti, &hp)

	return &model{
		project:     project,
		services:    services,
		hours:       hours,
		seen:        make(map[string]struct{}),
		mode:        modeLive,
		loading:     true,
		ctx:         ctx,
		cancel:      cancel,
		batches:     make(chan batchMsg, batchChanSize),
		errs:        make(chan error, 4),
		searchInput: ti,
		keys:        newKeyMap(),
		help:        hp,
	}
}

func (m *model) Init() tea.Cmd {
	go streamTail(m.ctx, m.project, m.services,
		func(b []logEntry) {
			select {
			case m.batches <- batchMsg{entries: b}:
			case <-m.ctx.Done():
			}
		},
		func(err error) {
			select {
			case m.errs <- err:
			case <-m.ctx.Done():
			}
		},
	)
	return tea.Batch(m.fetchInitialCmd(), m.waitBatchCmd(), m.waitErrCmd())
}

// --- Commands ---

func (m *model) fetchInitialCmd() tea.Cmd {
	return func() tea.Msg {
		entries, err := initialFetch(m.ctx, m.project, m.services, m.hours, initialLimit)
		return initialDoneMsg{entries: entries, err: err}
	}
}

func (m *model) waitBatchCmd() tea.Cmd {
	return func() tea.Msg {
		select {
		case b := <-m.batches:
			return b
		case <-m.ctx.Done():
			return nil
		}
	}
}

func (m *model) waitErrCmd() tea.Cmd {
	return func() tea.Msg {
		select {
		case e := <-m.errs:
			return errMsg{err: e}
		case <-m.ctx.Done():
			return nil
		}
	}
}

func (m *model) runSearchCmd(text string) tea.Cmd {
	return func() tea.Msg {
		entries, err := searchLogs(m.ctx, m.project, text, m.services, searchHours, searchLimit)
		return searchDoneMsg{text: text, entries: entries, err: err}
	}
}

func (m *model) reloadAfterScopeChange() tea.Cmd {
	// Clear buffer + restart streaming with the new service scope.
	m.entries = m.entries[:0]
	m.seen = make(map[string]struct{})
	m.cursor, m.viewport, m.unseen = 0, 0, 0
	m.mode = modeLive
	m.loading = true
	// Cancel current stream goroutine.
	m.cancel()
	m.ctx, m.cancel = context.WithCancel(context.Background())
	m.batches = make(chan batchMsg, batchChanSize)
	m.errs = make(chan error, 4)
	go streamTail(m.ctx, m.project, m.services,
		func(b []logEntry) {
			select {
			case m.batches <- batchMsg{entries: b}:
			case <-m.ctx.Done():
			}
		},
		func(err error) {
			select {
			case m.errs <- err:
			case <-m.ctx.Done():
			}
		},
	)
	return tea.Batch(m.fetchInitialCmd(), m.waitBatchCmd(), m.waitErrCmd())
}

// --- Update ---

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.help.Width = msg.Width
		m.searchInput.Width = msg.Width - 12
		m.clampViewport()
		return m, nil

	case initialDoneMsg:
		m.loading = false
		if msg.err != nil {
			m.lastErr = msg.err
			return m, nil
		}
		for _, e := range msg.entries {
			m.ingest(e)
		}
		m.scrollToEnd()
		return m, nil

	case batchMsg:
		// Always ingest into `seen` so we don't double-render on later
		// reload, but only update the visible buffer in modeLive.
		if m.mode == modeLive {
			wasAtBottom := m.isAtBottom()
			added := 0
			for _, e := range msg.entries {
				if m.ingest(e) {
					added++
				}
			}
			if added > 0 {
				if wasAtBottom {
					m.scrollToEnd()
				} else {
					m.unseen += added
				}
			}
		}
		return m, m.waitBatchCmd()

	case errMsg:
		if msg.err != nil {
			m.lastErr = msg.err
		}
		return m, m.waitErrCmd()

	case searchDoneMsg:
		// Drop a stale result if the user already moved on.
		if msg.text != m.searchText {
			return m, nil
		}
		m.loading = false
		if msg.err != nil {
			m.lastErr = msg.err
			return m, nil
		}
		m.entries = msg.entries
		m.seen = make(map[string]struct{}, len(msg.entries))
		for _, e := range msg.entries {
			if e.InsertID != "" {
				m.seen[e.InsertID] = struct{}{}
			}
		}
		m.cursor, m.viewport = 0, 0
		m.flash = fmt.Sprintf("%d results for %q", len(msg.entries), msg.text)
		return m, nil

	case clearFlashMsg:
		m.flash = ""
		return m, nil

	case tea.KeyMsg:
		// Route order: overlay > search input > main keys.
		if m.overlay != nil {
			cmd, close := m.overlay.Update(msg, m)
			if close {
				m.overlay = nil
			}
			return m, cmd
		}
		if m.searchActive {
			return m, m.handleSearchInput(msg)
		}
		return m, m.handleMainKey(msg)

	default:
		// Async messages owned by an overlay (servicesLoaded, traceLoaded,
		// contextLoaded). Forward to the overlay if one is active.
		if m.overlay != nil {
			cmd, close := m.overlay.Update(msg, m)
			if close {
				m.overlay = nil
			}
			return m, cmd
		}
		// Forward non-key messages (e.g. textinput.BlinkMsg) to the search
		// input so the cursor keeps blinking while the bar is open.
		if m.searchActive {
			var cmd tea.Cmd
			m.searchInput, cmd = m.searchInput.Update(msg)
			return m, cmd
		}
	}
	return m, nil
}

func (m *model) handleMainKey(k tea.KeyMsg) tea.Cmd {
	m.flash = ""
	switch k.String() {
	case "q", "ctrl+c":
		m.cancel()
		return tea.Quit

	case "?":
		m.overlay = newHelpModal()
		return nil

	case "T":
		if active == moon {
			applyTheme(dawn)
		} else {
			applyTheme(moon)
		}
		applyBubblesStyles(&m.searchInput, &m.help)
		m.flash = "theme: " + active.Name
		return nil

	case "/":
		// Pause streaming visually while typing; results will replace buffer.
		m.searchActive = true
		m.searchInput.SetValue(m.searchText)
		m.searchInput.CursorEnd()
		return m.searchInput.Focus()

	case "s":
		m.overlay = newPickerModal(m.services, m.knownServices)
		if len(m.knownServices) == 0 {
			return m.overlay.(*pickerModal).loadCmd(m)
		}
		return nil

	case "enter":
		if e := m.cursorEntry(); e != nil {
			m.overlay = newDetailModal(*e)
		}
		return nil

	case "t":
		e := m.cursorEntry()
		if e == nil {
			return nil
		}
		if e.Trace == "" {
			m.flash = "no trace on this row"
			return nil
		}
		m.overlay = newTraceModal(e.Trace, m)
		return m.overlay.(*traceModal).load(m)

	case "c":
		e := m.cursorEntry()
		if e == nil || e.Service == "" || e.Time.IsZero() {
			return nil
		}
		m.overlay = newContextModal(*e, m)
		return m.overlay.(*contextModal).load(m)

	case "y":
		e := m.cursorEntry()
		if e == nil {
			m.flash = "no row selected to share"
			return nil
		}
		copyToClipboard(formatEntryShare(*e, m.project))
		m.flash = "copied — paste into your agent"
		return nil

	case " ", "space":
		if m.mode == modeSearch {
			m.flash = "esc first to leave search"
			return nil
		}
		if m.mode == modeLive {
			m.mode = modePaused
		} else {
			m.mode = modeLive
			if m.isAtBottom() {
				m.unseen = 0
			}
		}
		return nil

	case "esc":
		if m.mode == modeSearch {
			return m.exitSearchResults()
		}
		return nil

	case "r":
		m.loading = true
		if m.mode == modeSearch && m.searchText != "" {
			return m.runSearchCmd(m.searchText)
		}
		return m.reloadAfterScopeChange()

	case "g":
		m.scrollToEnd()
		m.unseen = 0
		return nil
	case "G", "end":
		m.scrollToEnd()
		m.unseen = 0
		return nil
	case "home":
		m.cursor, m.viewport = 0, 0
		return nil

	case "up", "k":
		m.moveCursor(-1)
		return nil
	case "down", "j":
		m.moveCursor(1)
		return nil
	case "pgup":
		m.moveCursor(-m.visibleRows())
		return nil
	case "pgdown":
		m.moveCursor(m.visibleRows())
		return nil
	}
	return nil
}

func (m *model) handleSearchInput(k tea.KeyMsg) tea.Cmd {
	// Enter/Esc are handled here; everything else (typing, backspace, cursor
	// movement, ctrl-u clear, …) is delegated to bubbles/textinput.
	switch k.Type {
	case tea.KeyEnter:
		text := strings.TrimSpace(m.searchInput.Value())
		m.searchActive = false
		m.searchInput.Blur()
		if text == "" {
			return nil
		}
		m.searchText = text
		m.mode = modeSearch
		m.entries = m.entries[:0]
		m.seen = make(map[string]struct{})
		m.cursor, m.viewport, m.unseen = 0, 0, 0
		m.loading = true
		return m.runSearchCmd(text)
	case tea.KeyEsc, tea.KeyCtrlC:
		m.searchActive = false
		m.searchInput.Blur()
		m.searchInput.SetValue("")
		return nil
	}
	var cmd tea.Cmd
	m.searchInput, cmd = m.searchInput.Update(k)
	return cmd
}

func (m *model) exitSearchResults() tea.Cmd {
	m.searchText = ""
	return m.reloadAfterScopeChange()
}

// --- Buffer / viewport ---

func (m *model) ingest(e logEntry) bool {
	if e.InsertID != "" {
		if _, ok := m.seen[e.InsertID]; ok {
			return false
		}
		m.seen[e.InsertID] = struct{}{}
	}
	m.entries = append(m.entries, e)
	if len(m.entries) > bufferCap {
		drop := len(m.entries) - bufferTrim
		m.entries = m.entries[drop:]
		m.cursor = clamp(m.cursor-drop, 0, len(m.entries)-1)
		m.viewport = clamp(m.viewport-drop, 0, m.maxViewport())
	}
	return true
}

func (m *model) cursorEntry() *logEntry {
	if m.cursor < 0 || m.cursor >= len(m.entries) {
		return nil
	}
	return &m.entries[m.cursor]
}

func (m *model) visibleRows() int {
	// chrome: header(1) + status(1) + table-header(1) + footer/search(1) = 4
	return max(0, m.height-4)
}

func (m *model) maxViewport() int {
	if len(m.entries) <= m.visibleRows() {
		return 0
	}
	return len(m.entries) - m.visibleRows()
}

func (m *model) isAtBottom() bool {
	if len(m.entries) == 0 {
		return true
	}
	return m.viewport >= m.maxViewport()
}

func (m *model) scrollToEnd() {
	if len(m.entries) == 0 {
		m.cursor, m.viewport = 0, 0
		return
	}
	m.cursor = len(m.entries) - 1
	m.viewport = m.maxViewport()
}

func (m *model) clampViewport() {
	m.viewport = clamp(m.viewport, 0, m.maxViewport())
	m.cursor = clamp(m.cursor, 0, max(0, len(m.entries)-1))
}

func (m *model) moveCursor(delta int) {
	if len(m.entries) == 0 {
		return
	}
	m.cursor = clamp(m.cursor+delta, 0, len(m.entries)-1)
	if m.cursor < m.viewport {
		m.viewport = m.cursor
	} else if m.cursor >= m.viewport+m.visibleRows() {
		m.viewport = m.cursor - m.visibleRows() + 1
	}
	m.viewport = clamp(m.viewport, 0, m.maxViewport())
	if m.isAtBottom() {
		m.unseen = 0
	}
}

// --- View ---

func (m *model) View() string {
	if m.width == 0 || m.height == 0 {
		return ""
	}
	if m.overlay != nil {
		return m.overlay.View(m.width, m.height)
	}
	w := m.width
	var b strings.Builder
	b.WriteString(m.renderHeader(w))
	b.WriteString("\n")
	b.WriteString(m.renderStatus(w))
	b.WriteString("\n")
	b.WriteString(m.renderTableHeader(w))
	b.WriteString("\n")
	b.WriteString(m.renderBody(w, m.visibleRows()))
	b.WriteString("\n")
	b.WriteString(m.renderFooterOrSearch(w))
	return b.String()
}

// applyBubblesStyles rebinds the bubbles/v1 widget styles to the active
// palette. Bubbles v1 wants `lipgloss/v1.Style` values, so we bridge through
// hex strings (palette stores image/color.Color from lipgloss v2).
func applyBubblesStyles(ti *textinput.Model, hp *help.Model) {
	pri := lg1.Color(hexOf(active.Primary))
	mut := lg1.Color(hexOf(active.FgMuted))
	base := lg1.Color(hexOf(active.FgBase))
	acc := lg1.Color(hexOf(active.Accent))

	ti.PromptStyle = lg1.NewStyle().Foreground(pri).Bold(true)
	ti.TextStyle = lg1.NewStyle().Foreground(base)
	ti.PlaceholderStyle = lg1.NewStyle().Foreground(mut).Italic(true)
	ti.Cursor.Style = lg1.NewStyle().Foreground(acc)

	hp.Styles = help.Styles{
		ShortKey:       lg1.NewStyle().Foreground(pri).Bold(true),
		ShortDesc:      lg1.NewStyle().Foreground(mut),
		ShortSeparator: lg1.NewStyle().Foreground(mut),
		FullKey:        lg1.NewStyle().Foreground(pri).Bold(true),
		FullDesc:       lg1.NewStyle().Foreground(mut),
		FullSeparator:  lg1.NewStyle().Foreground(mut),
		Ellipsis:       lg1.NewStyle().Foreground(mut),
	}
}

// chip renders a powerline-style segment with its own bg/fg, bold, padded.
func chip(text string, bg, fg color.Color) string {
	return lipgloss.NewStyle().
		Background(bg).
		Foreground(fg).
		Bold(true).
		Padding(0, 1).
		Render(text)
}

// fill renders a flex segment that pads to `w` cells with the given bg.
func fill(text string, bg, fg color.Color, w int) string {
	if w < 0 {
		w = 0
	}
	return lipgloss.NewStyle().
		Background(bg).
		Foreground(fg).
		Width(w).
		Padding(0, 1).
		Render(text)
}

// renderAvatar draws a compact 2-char "pixel cluster" before the brand text.
// Each block uses a different brand hue so it reads as a tiny pixel-art mark
// rather than a single character. Pairs naturally with the gradient on the
// "CloudLens" wordmark that follows it.
func renderAvatar() string {
	left := lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render("▟")
	right := lipgloss.NewStyle().Foreground(active.Secondary).Bold(true).Render("▙")
	return left + right
}

func (m *model) renderHeader(w int) string {
	// Pixel avatar (pink ▟ + lavender ▙) + per-character pink→purple
	// gradient on "CloudLens". Project name plain bright; theme tag dim
	// italic on the right. No chip backgrounds — just clean colored text on
	// a flat surface tint. Restraint pass per the design mockup.
	avatar := renderAvatar()
	brand := gradientText("CloudLens", active.BrandGradStart, active.BrandGradEnd)
	dot := lipgloss.NewStyle().Foreground(active.FgMuted).Render("  ·  ")
	projLabel := lipgloss.NewStyle().Foreground(active.FgMuted).Render("project")
	projName := lipgloss.NewStyle().Foreground(active.FgBase).Bold(true).Render(m.project)
	theme := lipgloss.NewStyle().Foreground(active.FgMuted).Italic(true).Render(active.Name)

	left := "  " + avatar + "  " + brand + dot + projLabel + " " + projName
	right := theme + "  "
	pad := w - lipgloss.Width(left) - lipgloss.Width(right)
	if pad < 0 {
		pad = 0
	}
	return lipgloss.NewStyle().
		Background(active.BgSurface).
		Width(w).
		Render(left + strings.Repeat(" ", pad) + right)
}

func (m *model) renderStatus(w int) string {
	// Flat status row — colored MODE on the left (dot + word in mode color),
	// scope and count as dim text in the middle, gold unseen counter / flash
	// /error chips on the right when present. No chip backgrounds — single
	// surface tint across the whole row. Per design mockup: restraint.

	bg := active.BgPanel // slightly darker than header to band the zones

	var modeText string
	switch m.mode {
	case modeLive:
		modeText = lipgloss.NewStyle().Foreground(active.Success).Bold(true).Render("●  LIVE")
	case modePaused:
		modeText = lipgloss.NewStyle().Foreground(active.Warning).Bold(true).Render("⏸  PAUSED")
	case modeSearch:
		modeText = lipgloss.NewStyle().Foreground(active.Secondary).Bold(true).Render("🔍  SEARCH")
	}

	scope := "all services"
	switch len(m.services) {
	case 0:
	case 1, 2, 3:
		scope = strings.Join(m.services, ", ")
	default:
		scope = fmt.Sprintf("%d services", len(m.services))
	}
	sep := lipgloss.NewStyle().Foreground(active.FgMuted).Render("  ·  ")
	scopeText := lipgloss.NewStyle().Foreground(active.FgBase).Render(scope)

	var countText string
	if m.mode == modeSearch && m.searchText != "" {
		q := lipgloss.NewStyle().Foreground(active.Secondary).Italic(true).Render(fmt.Sprintf("%q", m.searchText))
		c := lipgloss.NewStyle().Foreground(active.FgMuted).Render(fmt.Sprintf("%d results", len(m.entries)))
		countText = q + sep + c
	} else {
		countText = lipgloss.NewStyle().Foreground(active.FgMuted).Render(fmt.Sprintf("%d logs", len(m.entries)))
	}

	leftParts := []string{modeText, sep, scopeText, sep, countText}
	if m.loading {
		leftParts = append(leftParts, sep,
			lipgloss.NewStyle().Foreground(active.FgMuted).Italic(true).Render("loading…"))
	}
	leftText := "  " + strings.Join(leftParts, "")

	rightParts := []string{}
	if m.unseen > 0 && m.mode == modeLive {
		rightParts = append(rightParts,
			lipgloss.NewStyle().Foreground(active.Warning).Bold(true).Render(fmt.Sprintf("↓ %d new", m.unseen)))
	}
	if m.flash != "" {
		rightParts = append(rightParts,
			lipgloss.NewStyle().Foreground(active.Accent).Bold(true).Render("✓ "+m.flash))
	}
	if m.lastErr != nil {
		rightParts = append(rightParts,
			lipgloss.NewStyle().Foreground(active.Error).Bold(true).Render("⚠ "+truncate(m.lastErr.Error(), 28)))
	}
	var rightText string
	if len(rightParts) > 0 {
		rightText = strings.Join(rightParts, sep) + "  "
	}

	pad := w - lipgloss.Width(leftText) - lipgloss.Width(rightText)
	if pad < 0 {
		pad = 0
	}
	return lipgloss.NewStyle().
		Background(bg).
		Width(w).
		Render(leftText + strings.Repeat(" ", pad) + rightText)
}

const (
	colTimeW = 8
	colSevW  = 2 // single dot + breathing space (no more INFO/ERROR text)
	colTrcW  = 2 // single dot + breathing space
	colSvcW  = 28
)

func msgWidth(total int) int {
	used := 2 + 4 + colTimeW + colSevW + colTrcW + colSvcW
	w := total - used
	if w < 10 {
		return 10
	}
	return w
}

func (m *model) renderTableHeader(w int) string {
	// Tiny uppercase column labels in dim color, letter-spaced for elegance.
	// The strip uses a slightly darker bg so the column header band reads
	// distinctly above the table body.
	label := lipgloss.NewStyle().
		Foreground(active.FgMuted).
		Bold(true)

	row := lipgloss.JoinHorizontal(lipgloss.Left,
		"  ", // gutter to align with row markers
		label.Render(fixed("TIME", colTimeW)), "  ",
		label.Render(fixed("SEV", colSevW)), " ",
		label.Render(fixed("T", colTrcW)), " ",
		label.Render(fixed("SERVICE", colSvcW)), " ",
		label.Render("MESSAGE"),
	)
	return lipgloss.NewStyle().
		Background(active.BgSurface).
		Foreground(active.FgBase).
		Width(w).
		Render(row)
}

func (m *model) renderBody(w, rows int) string {
	if rows <= 0 {
		return ""
	}
	if len(m.entries) == 0 {
		var msg string
		switch {
		case m.loading:
			msg = "waiting for logs…"
		case m.mode == modeSearch:
			msg = fmt.Sprintf("no results for %q in last %.0fh", m.searchText, searchHours)
		case len(m.services) > 0:
			msg = fmt.Sprintf(
				"no logs in last %.0fm matching services: %s\n"+
					"  · do those service names exist? `gcloud run services list --project %s`\n"+
					"  · press s to pick from a list, / to search, or T to toggle theme",
				m.hours*60, strings.Join(m.services, ", "), m.project,
			)
		default:
			msg = fmt.Sprintf("no Cloud Run logs in last %.0fm for %s · still listening", m.hours*60, m.project)
		}
		return loadingStyle.Width(w).Padding(0, 1).Render(msg) +
			strings.Repeat("\n", max(0, rows-strings.Count(msg, "\n")-1))
	}
	end := m.viewport + rows
	if end > len(m.entries) {
		end = len(m.entries)
	}
	var lines []string
	for i := m.viewport; i < end; i++ {
		lines = append(lines, m.renderRow(m.entries[i], w, i == m.cursor))
	}
	for len(lines) < rows {
		lines = append(lines, lipgloss.NewStyle().Width(w).Render(""))
	}
	return strings.Join(lines, "\n")
}

func (m *model) renderRow(e logEntry, w int, selected bool) string {
	timeStr := "        "
	if !e.Time.IsZero() {
		timeStr = e.Time.Local().Format("15:04:05")
	}

	// Severity → colored dot (no more INFO/WARN/ERROR text column).
	// WARNING gets ▲ for shape distinction; everything else is ●.
	sevGlyph, sevSt := severityDot(defaultStr(e.Severity, "DEFAULT"))

	// Trace marker — filled dot if the entry has a trace_id (drillable),
	// dim · otherwise. Same accent color as the brand pixel block.
	traceGlyph := "·"
	traceStyle := dimStyle
	if e.Trace != "" {
		traceGlyph = "●"
		traceStyle = lipgloss.NewStyle().Foreground(active.Secondary)
	}

	svc := truncate(e.Service, colSvcW)
	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	msg = strings.ReplaceAll(msg, "\n", " ")

	// 2-char left gutter: pink ▌ on the active row, blank otherwise.
	var marker string
	if selected {
		marker = lipgloss.NewStyle().Foreground(active.Primary).Bold(true).Render("▌ ")
	} else {
		marker = "  "
	}

	mw := msgWidth(w) - 2 // gutter eats 2 cells
	if mw < 10 {
		mw = 10
	}
	msg = truncate(msg, mw)
	svcStyle := lipgloss.NewStyle().Foreground(colorForService(e.Service))

	cells := lipgloss.JoinHorizontal(lipgloss.Left,
		dimStyle.Render(fixed(timeStr, colTimeW)), "  ",
		sevSt.Render(fixed(sevGlyph, colSevW)), " ",
		traceStyle.Render(fixed(traceGlyph, colTrcW)), " ",
		svcStyle.Render(fixed(svc, colSvcW)), " ",
		fixed(msg, mw),
	)
	if selected {
		return marker + rowSelectedStyle.Width(w-2).Render(cells)
	}
	return marker + rowStyle.Width(w-2).Render(cells)
}

// keyPill renders a "k action" pill with a subtle panel bg — the key in
// brand color, the action label in muted body color.
func keyPill(k, v string) string {
	return lipgloss.NewStyle().
		Background(active.BgPanel).
		Padding(0, 1).
		Render(
			lipgloss.NewStyle().Background(active.BgPanel).Foreground(active.Primary).Bold(true).Render(k) +
				lipgloss.NewStyle().Background(active.BgPanel).Foreground(active.FgMuted).Render(" "+v),
		)
}

func (m *model) renderFooterOrSearch(w int) string {
	if m.searchActive {
		// bubbles/textinput View renders prompt + buffered text + blinking
		// cursor; we just frame it in our focused-input border and append a
		// short hint.
		hint := lipgloss.NewStyle().Foreground(active.FgMuted).Render(" enter run · esc cancel")
		hintW := lipgloss.Width(hint)
		box := inputFocusedStyle.Width(w - hintW - 2).Render(m.searchInput.View())
		return lipgloss.JoinHorizontal(lipgloss.Top, box, hint)
	}
	// bubbles/help renders ShortHelp from m.keys — auto-formatted, ellipsis
	// on overflow. Wrapped in a surface-bg strip so it reads as a footer.
	bar := m.help.View(m.keys)
	return lipgloss.NewStyle().
		Background(active.BgSurface).
		Foreground(active.FgBase).
		Padding(0, 1).
		Width(w).
		Render(bar)
}

// --- helpers ---

func fixed(s string, w int) string {
	return lipgloss.NewStyle().Width(w).Render(s)
}

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
