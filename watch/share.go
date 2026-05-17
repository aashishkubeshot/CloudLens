// Clipboard via OSC 52 + markdown briefs sized for handing off to an agent
// that has the `cloudlens` MCP registered. Three shapes: single entry, full
// cross-service trace, and service-focused context window. Mirrors the
// Python `share.py` so a brief looks the same regardless of which TUI
// produced it.
package main

import (
	"encoding/base64"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
)

// copyToClipboard writes the text to the terminal's clipboard via OSC 52.
// Works in iTerm2, kitty, Alacritty, Windows Terminal, recent gnome-terminal,
// and the VS Code integrated terminal with OSC 52 enabled.
func copyToClipboard(text string) {
	enc := base64.StdEncoding.EncodeToString([]byte(text))
	// CSI / OSC are written to stderr so they don't pollute stdout pipes.
	_, _ = os.Stderr.Write([]byte("\x1b]52;c;" + enc + "\x07"))
}

func cloudLoggingURL(project string, e logEntry) string {
	parts := []string{`resource.type="cloud_run_revision"`}
	if e.Service != "" {
		parts = append(parts, fmt.Sprintf(`resource.labels.service_name="%s"`, e.Service))
	}
	if e.Trace != "" {
		parts = append(parts, fmt.Sprintf(`trace="projects/%s/traces/%s"`, project, e.Trace))
	}
	query := strings.Join(parts, " AND ")
	return fmt.Sprintf(
		"https://console.cloud.google.com/logs/query;query=%s?project=%s",
		url.QueryEscape(query), project,
	)
}

func metaLines(e logEntry, project string) []string {
	out := []string{fmt.Sprintf("- **Project:** `%s`", project)}
	if !e.Time.IsZero() {
		out = append(out, fmt.Sprintf("- **Time:** `%s`", e.Time.UTC().Format(time.RFC3339Nano)))
	}
	if e.Service != "" {
		line := fmt.Sprintf("- **Service:** `%s`", e.Service)
		if e.Revision != "" {
			line += fmt.Sprintf("  (revision `%s`)", e.Revision)
		}
		out = append(out, line)
	}
	out = append(out, fmt.Sprintf("- **Severity:** `%s`", defaultStr(e.Severity, "DEFAULT")))
	if e.HTTP != "" {
		out = append(out, fmt.Sprintf("- **HTTP:** `%s`", e.HTTP))
	}
	if e.Trace != "" {
		out = append(out, fmt.Sprintf("- **Trace ID:** `%s`", e.Trace))
	}
	return out
}

func mcpHooksForEntry(e logEntry) []string {
	var out []string
	if e.Trace != "" {
		out = append(out, fmt.Sprintf(
			"- `get_logs_by_trace(\"%s\")` — every entry across services for this request, oldest → newest",
			e.Trace,
		))
	}
	if e.Service != "" {
		out = append(out,
			fmt.Sprintf("- `get_health(\"%s\")` — request count, error rate, latency p50/p95/p99 for the last hour", e.Service),
			fmt.Sprintf("- `diff_windows(\"%s\")` — what changed vs the previous window (deploys, error rate, latency deltas)", e.Service),
			fmt.Sprintf("- `get_errors(\"%s\")` — other failing entries (severity≥ERROR or 5xx) in the window", e.Service),
			fmt.Sprintf("- `list_revisions(\"%s\", hours=24)` — recent deploys with traffic split", e.Service),
		)
	}
	return out
}

func formatEntryShare(e logEntry, project string) string {
	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	if msg == "" {
		msg = "(no message)"
	}
	var b strings.Builder
	b.WriteString("# CloudLens log share\n\n")
	b.WriteString("Help me debug this Cloud Run log. If you have the `cloudlens` MCP registered, run the tool calls under **CloudLens MCP hooks** below to investigate — they're literal tool names with arguments filled in.\n\n")
	b.WriteString("## The log\n\n")
	b.WriteString(strings.Join(metaLines(e, project), "\n"))
	b.WriteString("\n\n**Message:**\n```\n")
	b.WriteString(msg)
	b.WriteString("\n```\n")
	if hooks := mcpHooksForEntry(e); len(hooks) > 0 {
		b.WriteString("\n## CloudLens MCP hooks\n\n")
		b.WriteString(strings.Join(hooks, "\n"))
		b.WriteString("\n")
	}
	b.WriteString("\n## Cloud Logging Explorer\n\n")
	b.WriteString(cloudLoggingURL(project, e))
	b.WriteString("\n")
	return b.String()
}

func entryLine(e logEntry, svcWidth int, anchor bool) string {
	ts := "?"
	if !e.Time.IsZero() {
		ts = e.Time.UTC().Format("15:04:05.000")
	}
	sev := defaultStr(e.Severity, "DEFAULT")
	msg := e.Message
	if msg == "" {
		msg = e.HTTP
	}
	msg = strings.ReplaceAll(msg, "\n", " ")
	if len(msg) > 280 {
		msg = msg[:279] + "…"
	}
	marker := " "
	if anchor {
		marker = "▶"
	}
	if svcWidth > 0 {
		return fmt.Sprintf("%s [%s] %-8s %-*s %s", marker, ts, sev, svcWidth, e.Service, msg)
	}
	return fmt.Sprintf("%s [%s] %-8s %s", marker, ts, sev, msg)
}

func formatTraceShare(traceID string, entries []logEntry, project string) string {
	if len(entries) == 0 {
		return fmt.Sprintf("# CloudLens trace share — `%s`\n\nTrace not found in the last 24h.\n", traceID)
	}
	svcSet := map[string]struct{}{}
	for _, e := range entries {
		if e.Service != "" {
			svcSet[e.Service] = struct{}{}
		}
	}
	services := make([]string, 0, len(svcSet))
	for s := range svcSet {
		services = append(services, s)
	}
	sort.Strings(services)
	maxSvc := 10
	for _, s := range services {
		if len(s) > maxSvc {
			maxSvc = len(s)
		}
	}

	var b strings.Builder
	fmt.Fprintf(&b, "# CloudLens trace share — `%s`\n\n", traceID)
	b.WriteString("Help me debug this Cloud Run request. The full cross-service log stitch is below, oldest → newest.\n\n")
	fmt.Fprintf(&b, "- **Project:** `%s`\n", project)
	fmt.Fprintf(&b, "- **Trace ID:** `%s`\n", traceID)
	fmt.Fprintf(&b, "- **Services touched:** %s\n", strings.Join(bt(services), ", "))
	fmt.Fprintf(&b, "- **Entries:** %d\n", len(entries))
	fmt.Fprintf(&b, "- **Span:** `%s` → `%s`\n\n",
		entries[0].Time.UTC().Format(time.RFC3339),
		entries[len(entries)-1].Time.UTC().Format(time.RFC3339),
	)
	b.WriteString("## Entries\n\n```\n")
	for _, e := range entries {
		b.WriteString(entryLine(e, maxSvc, false))
		b.WriteByte('\n')
	}
	b.WriteString("```\n\n## CloudLens MCP hooks\n\n")
	fmt.Fprintf(&b, "- `get_trace(\"%s\")` — span tree with per-span timing (requires OTel instrumentation; falls back to `{\"found\": false}` otherwise)\n", traceID)
	for _, s := range services {
		fmt.Fprintf(&b, "- `diff_windows(\"%s\")` — recent regressions in `%s`\n", s, s)
	}
	return b.String()
}

func formatContextShare(anchor logEntry, entries []logEntry, project string) string {
	var b strings.Builder
	svc := defaultStr(anchor.Service, "?")
	ts := "?"
	if !anchor.Time.IsZero() {
		ts = anchor.Time.UTC().Format(time.RFC3339Nano)
	}

	fmt.Fprintf(&b, "# CloudLens context share — `%s` around `%s`\n\n", svc, ts)
	b.WriteString("Help me debug this. Below are the log entries from this service before and after the row I'm focused on (marked with ▶).\n\n")
	fmt.Fprintf(&b, "- **Project:** `%s`\n", project)
	fmt.Fprintf(&b, "- **Service:** `%s`\n", svc)
	fmt.Fprintf(&b, "- **Anchor time:** `%s`\n", ts)
	fmt.Fprintf(&b, "- **Anchor severity:** `%s`\n", defaultStr(anchor.Severity, "DEFAULT"))
	if anchor.Trace != "" {
		fmt.Fprintf(&b, "- **Anchor trace:** `%s`\n", anchor.Trace)
	}
	if anchor.HTTP != "" {
		fmt.Fprintf(&b, "- **Anchor http:** `%s`\n", anchor.HTTP)
	}
	fmt.Fprintf(&b, "- **Entries in window:** %d\n\n", len(entries))
	b.WriteString("## Entries (oldest → newest)\n\n```\n")
	for _, e := range entries {
		isAnchor := e.InsertID != "" && e.InsertID == anchor.InsertID
		if !isAnchor {
			isAnchor = e.Time.Equal(anchor.Time) && e.Message == anchor.Message
		}
		b.WriteString(entryLine(e, 0, isAnchor))
		b.WriteByte('\n')
	}
	b.WriteString("```\n\n## CloudLens MCP hooks\n\n")
	if anchor.Trace != "" {
		fmt.Fprintf(&b, "- `get_logs_by_trace(\"%s\")` — cross-service stitch for the anchor request\n", anchor.Trace)
	}
	if svc != "?" {
		fmt.Fprintf(&b, "- `get_health(\"%s\")` — service health snapshot\n", svc)
		fmt.Fprintf(&b, "- `diff_windows(\"%s\")` — what changed in the window\n", svc)
		fmt.Fprintf(&b, "- `get_recent_traces(\"%s\", failing_only=True)` — recent failing requests in `%s`\n", svc, svc)
	}
	return b.String()
}

func bt(ss []string) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = "`" + s + "`"
	}
	return out
}

func defaultStr(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}
