// cloudlens-watch — interactive Cloud Run log viewer (Go / Bubble Tea).
//
// Auth: Application Default Credentials.
// Project: --project, $GOOGLE_CLOUD_PROJECT, or $GCP_PROJECT.
package main

import (
	"flag"
	"fmt"
	"os"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

func main() {
	var (
		servicesArg string
		project     string
		hours       float64
	)
	flag.StringVar(&servicesArg, "services", "", "comma-separated service names (default: all Cloud Run services in the project)")
	flag.StringVar(&servicesArg, "s", "", "alias for --services")
	flag.StringVar(&project, "project", "", "GCP project ID (default: $GOOGLE_CLOUD_PROJECT)")
	flag.StringVar(&project, "p", "", "alias for --project")
	flag.Float64Var(&hours, "hours", 0.5, "initial lookback window in hours")
	flag.Parse()

	if project == "" {
		project = firstNonEmpty(os.Getenv("GOOGLE_CLOUD_PROJECT"), os.Getenv("GCP_PROJECT"))
	}
	if project == "" {
		fmt.Fprintln(os.Stderr, "Set --project or GOOGLE_CLOUD_PROJECT to your GCP project ID.")
		os.Exit(1)
	}

	m := newModel(project, parseServices(servicesArg), hours)
	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func parseServices(s string) []string {
	if s == "" {
		return nil
	}
	var out []string
	for _, p := range strings.Split(s, ",") {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func firstNonEmpty(ss ...string) string {
	for _, s := range ss {
		if s != "" {
			return s
		}
	}
	return ""
}
