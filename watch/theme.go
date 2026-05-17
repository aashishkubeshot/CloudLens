// Themes: two carefully tuned palettes (Midnight + Dawn), runtime-switchable
// via `T`. Each palette is a struct of Lipgloss colors; `applyTheme` rebinds
// the package-level styles so existing render code keeps working unchanged.
package main

import (
	"fmt"
	"hash/fnv"
	"image/color"
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/lucasb-eyer/go-colorful"
)

// v2 note: `lipgloss.Color(...)` is now a *function* returning image/color.Color
// rather than a string type. Palette fields are typed accordingly.
//
// BrandGradStart/End are kept as hex strings (not color.Color) because the
// gradient renderer interpolates in LUV color space — easier to start from
// strings and let go-colorful parse than to round-trip through color.Color.
type palette struct {
	Name string

	BgBase    color.Color
	BgSurface color.Color
	BgPanel   color.Color
	BgBoost   color.Color
	FgBase    color.Color
	FgMuted   color.Color
	Primary   color.Color
	Secondary color.Color
	Accent    color.Color
	Success   color.Color
	Warning   color.Color
	Error     color.Color

	BrandGradStart string // hex
	BrandGradEnd   string // hex

	ServicePalette []color.Color
}

// Rosé Pine Moon — the medium-dark variant of the Rosé Pine family. The
// canonical palette: love (#eb6f92) as the brand pink, iris (#c4a7e7) as
// the lavender accent, foam (#9ccfd8) for active cyan touches, gold
// (#f6c177) for warnings. Brand gradient runs love → iris across the
// "CloudLens" wordmark. Reference: https://rosepinetheme.com
var moon = &palette{
	Name:           "rose-pine-moon",
	BgBase:         lipgloss.Color("#232136"),
	BgSurface:      lipgloss.Color("#2a273f"),
	BgPanel:        lipgloss.Color("#393552"),
	BgBoost:        lipgloss.Color("#44415a"),
	FgBase:         lipgloss.Color("#e0def4"),
	FgMuted:        lipgloss.Color("#908caa"),
	Primary:        lipgloss.Color("#eb6f92"), // love
	Secondary:      lipgloss.Color("#c4a7e7"), // iris
	Accent:         lipgloss.Color("#9ccfd8"), // foam
	Success:        lipgloss.Color("#9ccfd8"),
	Warning:        lipgloss.Color("#f6c177"), // gold
	Error:          lipgloss.Color("#eb6f92"),
	BrandGradStart: "#eb6f92", // love
	BrandGradEnd:   "#c4a7e7", // iris
	ServicePalette: []color.Color{
		lipgloss.Color("#eb6f92"), lipgloss.Color("#c4a7e7"),
		lipgloss.Color("#ea9a97"), lipgloss.Color("#3e8fb0"),
		lipgloss.Color("#9ccfd8"), lipgloss.Color("#f6c177"),
		lipgloss.Color("#56949f"), lipgloss.Color("#a87cf7"),
		lipgloss.Color("#d7827e"), lipgloss.Color("#917caa"),
	},
}

// Rosé Pine Dawn — the light variant. Paper-white base with the same
// love/iris/foam/gold story at lower luminance so accents read against the
// lighter ground. Reference: https://rosepinetheme.com/palette/ingredients
var dawn = &palette{
	Name:           "rose-pine-dawn",
	BgBase:         lipgloss.Color("#faf4ed"),
	BgSurface:      lipgloss.Color("#fffaf3"),
	BgPanel:        lipgloss.Color("#f2e9e1"),
	BgBoost:        lipgloss.Color("#dfdad9"),
	FgBase:         lipgloss.Color("#575279"),
	FgMuted:        lipgloss.Color("#797593"),
	Primary:        lipgloss.Color("#b4637a"), // love
	Secondary:      lipgloss.Color("#907aa9"), // iris
	Accent:         lipgloss.Color("#56949f"), // foam
	Success:        lipgloss.Color("#56949f"),
	Warning:        lipgloss.Color("#ea9d34"), // gold
	Error:          lipgloss.Color("#b4637a"),
	BrandGradStart: "#b4637a", // love
	BrandGradEnd:   "#907aa9", // iris
	ServicePalette: []color.Color{
		lipgloss.Color("#b4637a"), lipgloss.Color("#907aa9"),
		lipgloss.Color("#d7827e"), lipgloss.Color("#286983"),
		lipgloss.Color("#56949f"), lipgloss.Color("#ea9d34"),
		lipgloss.Color("#7c3aed"), lipgloss.Color("#0e7490"),
		lipgloss.Color("#a16207"), lipgloss.Color("#9f1239"),
	},
}

var active *palette

func init() { applyTheme(moon) }

// applyTheme rebinds every style var to the new palette. Called once at init
// and again whenever the user toggles `T`.
func applyTheme(p *palette) {
	active = p
	headerStyle = lipgloss.NewStyle().Foreground(p.Primary).Bold(true).Padding(0, 1)
	headerProjectStyle = lipgloss.NewStyle().Foreground(p.Secondary)
	statusBarStyle = lipgloss.NewStyle().Background(p.BgSurface).Foreground(p.FgBase).Padding(0, 1)
	footerStyle = lipgloss.NewStyle().Foreground(p.FgMuted).Background(p.BgSurface).Padding(0, 1)
	footerKeyStyle = lipgloss.NewStyle().Foreground(p.Primary).Bold(true)
	tableHeaderStyle = lipgloss.NewStyle().Foreground(p.FgMuted).Padding(0, 1)
	rowStyle = lipgloss.NewStyle().Padding(0, 1)
	rowSelectedStyle = lipgloss.NewStyle().Background(p.BgBoost).Padding(0, 1)
	dimStyle = lipgloss.NewStyle().Foreground(p.FgMuted)
	traceMarkStyle = lipgloss.NewStyle().Foreground(p.Accent).Bold(true)
	modeLiveStyle = lipgloss.NewStyle().Foreground(p.Success).Bold(true)
	modePausedStyle = lipgloss.NewStyle().Foreground(p.Warning).Bold(true)
	modeSearchStyle = lipgloss.NewStyle().Foreground(p.Accent).Bold(true)
	unseenStyle = lipgloss.NewStyle().Foreground(p.Warning).Bold(true)
	loadingStyle = lipgloss.NewStyle().Foreground(p.FgMuted).Italic(true)
	errorBannerStyle = lipgloss.NewStyle().Foreground(p.Error).Bold(true).Padding(0, 1)
	panelStyle = lipgloss.NewStyle().Background(p.BgPanel).Foreground(p.FgBase).Border(lipgloss.RoundedBorder()).BorderForeground(p.Primary).Padding(1, 2)
	panelAccentStyle = lipgloss.NewStyle().Background(p.BgPanel).Foreground(p.FgBase).Border(lipgloss.RoundedBorder()).BorderForeground(p.Accent).Padding(0, 1)
	modalTitleStyle = lipgloss.NewStyle().Foreground(p.FgMuted)
	modalKeyStyle = lipgloss.NewStyle().Foreground(p.Primary).Bold(true)
	modalBodyStyle = lipgloss.NewStyle().Foreground(p.FgBase)
	checkmarkOnStyle = lipgloss.NewStyle().Foreground(p.Accent).Bold(true)
	checkmarkOffStyle = lipgloss.NewStyle().Foreground(p.FgMuted)
	inputStyle = lipgloss.NewStyle().Background(p.BgPanel).Foreground(p.FgBase).Padding(0, 1)
	inputFocusedStyle = lipgloss.NewStyle().Background(p.BgPanel).Foreground(p.FgBase).Padding(0, 1).Border(lipgloss.RoundedBorder()).BorderForeground(p.Accent)
}

// Style vars (rebuilt by applyTheme).
var (
	headerStyle        lipgloss.Style
	headerProjectStyle lipgloss.Style
	statusBarStyle     lipgloss.Style
	footerStyle        lipgloss.Style
	footerKeyStyle     lipgloss.Style
	tableHeaderStyle   lipgloss.Style
	rowStyle           lipgloss.Style
	rowSelectedStyle   lipgloss.Style
	dimStyle           lipgloss.Style
	traceMarkStyle     lipgloss.Style
	modeLiveStyle      lipgloss.Style
	modePausedStyle    lipgloss.Style
	modeSearchStyle    lipgloss.Style
	unseenStyle        lipgloss.Style
	loadingStyle       lipgloss.Style
	errorBannerStyle   lipgloss.Style
	panelStyle         lipgloss.Style
	panelAccentStyle   lipgloss.Style
	modalTitleStyle    lipgloss.Style
	modalKeyStyle      lipgloss.Style
	modalBodyStyle     lipgloss.Style
	checkmarkOnStyle   lipgloss.Style
	checkmarkOffStyle  lipgloss.Style
	inputStyle         lipgloss.Style
	inputFocusedStyle  lipgloss.Style
)

func colorForService(svc string) color.Color {
	if svc == "" {
		return active.FgMuted
	}
	h := fnv.New32a()
	_, _ = h.Write([]byte(svc))
	pal := active.ServicePalette
	return pal[int(h.Sum32())%len(pal)]
}

// hexOf converts an image/color.Color back to a "#rrggbb" string. Used to
// bridge our lipgloss-v2 palette into the bubbles v1 widgets (which still
// require lipgloss v1 Style values).
func hexOf(c color.Color) string {
	r, g, b, _ := c.RGBA()
	return fmt.Sprintf("#%02x%02x%02x", r>>8, g>>8, b>>8)
}

// gradientText renders `text` with a per-character color interpolation from
// `start` to `end` (hex strings). LUV-space blending so the steps feel
// perceptually even instead of dipping through muddy mid-tones.
func gradientText(text, startHex, endHex string) string {
	runes := []rune(text)
	n := len(runes)
	if n == 0 {
		return ""
	}
	start, _ := colorful.Hex(startHex)
	end, _ := colorful.Hex(endHex)
	var b strings.Builder
	for i, r := range runes {
		t := 0.0
		if n > 1 {
			t = float64(i) / float64(n-1)
		}
		c := start.BlendLuv(end, t).Clamped()
		b.WriteString(
			lipgloss.NewStyle().
				Foreground(lipgloss.Color(c.Hex())).
				Bold(true).
				Render(string(r)),
		)
	}
	return b.String()
}

func severityStyle(sev string) lipgloss.Style {
	base := lipgloss.NewStyle()
	switch sev {
	case "ERROR", "CRITICAL", "ALERT", "EMERGENCY":
		return base.Foreground(active.Error).Bold(true)
	case "WARNING":
		return base.Foreground(active.Warning)
	case "INFO", "NOTICE":
		return base.Foreground(active.FgBase)
	default:
		return base.Foreground(active.FgMuted)
	}
}

// severityDot returns the glyph + colored style used for severity rendering
// in rows / detail / help. ERROR-family is a filled red dot, WARNING is a
// triangle for visual distinction, INFO/NOTICE is the foam accent, anything
// else is a dim placeholder.
func severityDot(sev string) (string, lipgloss.Style) {
	base := lipgloss.NewStyle()
	switch sev {
	case "ERROR", "CRITICAL", "ALERT", "EMERGENCY":
		return "●", base.Foreground(active.Error).Bold(true)
	case "WARNING":
		return "▲", base.Foreground(active.Warning).Bold(true)
	case "INFO", "NOTICE":
		return "●", base.Foreground(active.Accent)
	default:
		return "●", base.Foreground(active.FgMuted)
	}
}
