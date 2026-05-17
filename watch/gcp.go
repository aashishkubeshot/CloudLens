// Cloud Logging client: filter builder, initial paginated fetch, and the
// live-tail server-streaming RPC. `TailLogEntries` only emits entries
// written *after* the stream is established, so we pair it with a one-shot
// `ListLogEntries` for the lookback window.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	logging "cloud.google.com/go/logging/apiv2"
	"cloud.google.com/go/logging/apiv2/loggingpb"
	"google.golang.org/api/iterator"
	logtype "google.golang.org/genproto/googleapis/logging/type"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// logEntry is the compact shape rendered in the TUI. Field names mirror the
// Python `format_entry` output so future feature work stays consistent.
type logEntry struct {
	Time     time.Time
	Severity string
	Service  string
	Revision string
	Message  string
	Trace    string
	HTTP     string
	InsertID string
}

// buildFilter constructs a Cloud Logging filter for Cloud Run entries.
// sinceMinutes>0 adds a `timestamp>=...` clause (used by initial fetch);
// pass 0 for the streaming tail since the server only sends new entries.
func buildFilter(services []string, sinceMinutes float64) string {
	parts := []string{`resource.type="cloud_run_revision"`}
	switch len(services) {
	case 0:
		// no service filter — all Cloud Run services
	case 1:
		parts = append(parts, fmt.Sprintf(`resource.labels.service_name="%s"`, services[0]))
	default:
		clauses := make([]string, 0, len(services))
		for _, s := range services {
			clauses = append(clauses, fmt.Sprintf(`resource.labels.service_name="%s"`, s))
		}
		parts = append(parts, "("+strings.Join(clauses, " OR ")+")")
	}
	// Drop the universal Cloud Run noise the Python build_filter also drops.
	parts = append(parts,
		`NOT logName:"cloudaudit.googleapis.com"`,
		`NOT labels."run.googleapis.com/sidecar":*`,
	)
	if sinceMinutes > 0 {
		ts := time.Now().UTC().Add(-time.Duration(sinceMinutes * float64(time.Minute)))
		parts = append(parts, fmt.Sprintf(`timestamp>="%s"`, ts.Format("2006-01-02T15:04:05Z")))
	}
	return strings.Join(parts, " AND ")
}

// listEntries runs a single paginated query with a custom filter & order.
// Returns up to `limit` entries in the order Cloud Logging gives them.
func listEntries(ctx context.Context, project, filter, orderBy string, limit int) ([]logEntry, error) {
	client, err := logging.NewClient(ctx)
	if err != nil {
		return nil, err
	}
	defer client.Close()

	it := client.ListLogEntries(ctx, &loggingpb.ListLogEntriesRequest{
		ResourceNames: []string{"projects/" + project},
		Filter:        filter,
		OrderBy:       orderBy,
		PageSize:      int32(limit),
	})
	out := make([]logEntry, 0, limit)
	for len(out) < limit {
		e, err := it.Next()
		if errors.Is(err, iterator.Done) {
			break
		}
		if err != nil {
			return nil, err
		}
		out = append(out, convertEntry(e))
	}
	return out, nil
}

// initialFetch grabs the last `limit` entries in the window. Cloud Logging
// returns them descending; we reverse to ascending so the UI can append.
func initialFetch(ctx context.Context, project string, services []string, hours float64, limit int) ([]logEntry, error) {
	out, err := listEntries(ctx, project, buildFilter(services, hours*60), "timestamp desc", limit)
	if err != nil {
		return nil, err
	}
	reverse(out)
	return out, nil
}

// searchLogs is a server-side substring search across the given services
// (nil = all). Returns up to `limit` entries oldest → newest.
func searchLogs(ctx context.Context, project, text string, services []string, hours float64, limit int) ([]logEntry, error) {
	base := buildFilter(services, hours*60)
	// Cloud Logging interprets `"..."` as a substring search across textPayload
	// and jsonPayload.message — same as Python's search(regex=False).
	filter := base + fmt.Sprintf(" AND %q", text)
	out, err := listEntries(ctx, project, filter, "timestamp desc", limit)
	if err != nil {
		return nil, err
	}
	reverse(out)
	return out, nil
}

// getLogsByTrace returns every entry for a trace_id, project-wide, oldest →
// newest. Crucially: no service *or resource type* filter — that's what makes
// it stitch across services and across runtimes (Cloud Run, Functions, GKE,
// GAE, GCE, Pub/Sub triggers, etc.). `hours` bounds the query.
func getLogsByTrace(ctx context.Context, project, traceID string, hours float64, limit int) ([]logEntry, error) {
	since := time.Now().UTC().Add(-time.Duration(hours * float64(time.Hour)))
	filter := fmt.Sprintf(
		`trace="projects/%s/traces/%s" AND timestamp>="%s"`,
		project, traceID, since.Format("2006-01-02T15:04:05Z"),
	)
	return listEntries(ctx, project, filter, "timestamp asc", limit)
}

// getContextBefore returns up to `limit` entries from svc with ts ≤ anchor,
// descending (caller reverses to ascending if needed).
func getContextBefore(ctx context.Context, project, svc string, anchor time.Time, windowMinutes float64, limit int) ([]logEntry, error) {
	since := anchor.Add(-time.Duration(windowMinutes * float64(time.Minute)))
	until := anchor.Add(time.Microsecond)
	filter := fmt.Sprintf(
		`resource.type="cloud_run_revision" AND resource.labels.service_name="%s" `+
			`AND timestamp>="%s" AND timestamp<"%s" `+
			`AND NOT logName:"cloudaudit.googleapis.com" AND NOT labels."run.googleapis.com/sidecar":*`,
		svc, since.UTC().Format("2006-01-02T15:04:05.000000Z"),
		until.UTC().Format("2006-01-02T15:04:05.000000Z"),
	)
	return listEntries(ctx, project, filter, "timestamp desc", limit)
}

// getContextAfter — same shape, ts > anchor, ascending.
func getContextAfter(ctx context.Context, project, svc string, anchor time.Time, windowMinutes float64, limit int) ([]logEntry, error) {
	since := anchor.Add(time.Microsecond)
	until := anchor.Add(time.Duration(windowMinutes * float64(time.Minute)))
	filter := fmt.Sprintf(
		`resource.type="cloud_run_revision" AND resource.labels.service_name="%s" `+
			`AND timestamp>="%s" AND timestamp<"%s" `+
			`AND NOT logName:"cloudaudit.googleapis.com" AND NOT labels."run.googleapis.com/sidecar":*`,
		svc, since.UTC().Format("2006-01-02T15:04:05.000000Z"),
		until.UTC().Format("2006-01-02T15:04:05.000000Z"),
	)
	return listEntries(ctx, project, filter, "timestamp asc", limit)
}

func reverse(entries []logEntry) {
	for i, j := 0, len(entries)-1; i < j; i, j = i+1, j-1 {
		entries[i], entries[j] = entries[j], entries[i]
	}
}

// streamTail opens a TailLogEntries RPC and pushes batches to `onBatch`
// until the context is cancelled. Cloud Logging keeps streams open ~1 hour;
// we silently reopen on EOF and report errors to `onError` with backoff.
func streamTail(
	ctx context.Context,
	project string,
	services []string,
	onBatch func([]logEntry),
	onError func(error),
) {
	for {
		if err := tailOnce(ctx, project, services, onBatch); err != nil {
			if ctx.Err() != nil {
				return
			}
			// io.EOF means the server closed the 1h session cleanly — just
			// reconnect. Other errors surface to the UI before retrying.
			if !errors.Is(err, io.EOF) {
				onError(err)
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(3 * time.Second):
			}
			continue
		}
		if ctx.Err() != nil {
			return
		}
	}
}

func tailOnce(
	ctx context.Context,
	project string,
	services []string,
	onBatch func([]logEntry),
) error {
	client, err := logging.NewClient(ctx)
	if err != nil {
		return err
	}
	defer client.Close()

	stream, err := client.TailLogEntries(ctx)
	if err != nil {
		return err
	}
	req := &loggingpb.TailLogEntriesRequest{
		ResourceNames: []string{"projects/" + project},
		Filter:        buildFilter(services, 0),
	}
	if err := stream.Send(req); err != nil {
		return err
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			return err
		}
		if len(resp.Entries) == 0 {
			continue
		}
		batch := make([]logEntry, 0, len(resp.Entries))
		for _, e := range resp.Entries {
			batch = append(batch, convertEntry(e))
		}
		onBatch(batch)
	}
}

func convertEntry(e *loggingpb.LogEntry) logEntry {
	out := logEntry{
		InsertID: e.GetInsertId(),
		Time:     tsToTime(e.GetTimestamp()),
		Severity: e.GetSeverity().String(),
		Message:  extractMessage(e),
	}
	if r := e.GetResource(); r != nil {
		if l := r.GetLabels(); l != nil {
			out.Service = l["service_name"]
			out.Revision = l["revision_name"]
		}
	}
	if t := e.GetTrace(); t != "" {
		// "projects/<p>/traces/<id>" → "<id>"
		if i := strings.LastIndex(t, "/"); i >= 0 {
			out.Trace = t[i+1:]
		} else {
			out.Trace = t
		}
	}
	if h := e.GetHttpRequest(); h != nil {
		out.HTTP = summarizeHTTP(h)
	}
	return out
}

func tsToTime(ts *timestamppb.Timestamp) time.Time {
	if ts == nil {
		return time.Time{}
	}
	return ts.AsTime()
}

func extractMessage(e *loggingpb.LogEntry) string {
	if t := strings.TrimSpace(e.GetTextPayload()); t != "" {
		return t
	}
	j := e.GetJsonPayload()
	if j == nil {
		return ""
	}
	fields := j.GetFields()
	for _, k := range []string{"message", "msg", "event", "log", "error"} {
		if v, ok := fields[k]; ok {
			if s := strings.TrimSpace(v.GetStringValue()); s != "" {
				return s
			}
		}
	}
	// Fall back to compact JSON.
	b, err := json.Marshal(j.AsMap())
	if err != nil {
		return ""
	}
	return truncate(string(b), 600)
}

func summarizeHTTP(h *logtype.HttpRequest) string {
	var parts []string
	if m := h.GetRequestMethod(); m != "" {
		parts = append(parts, m)
	}
	if u := h.GetRequestUrl(); u != "" {
		// Strip protocol+host for brevity — show the path only.
		if i := strings.Index(u, "://"); i >= 0 {
			if j := strings.Index(u[i+3:], "/"); j >= 0 {
				u = u[i+3+j:]
			}
		}
		parts = append(parts, u)
	}
	if s := h.GetStatus(); s != 0 {
		parts = append(parts, fmt.Sprintf("%d", s))
	}
	return strings.Join(parts, " ")
}

func truncate(s string, n int) string {
	if n <= 0 || len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}
