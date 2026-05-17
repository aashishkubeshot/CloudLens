// Cloud Run service listing — just enough to populate the picker modal.
// One call: list services across all locations ("-") and return their names.
package main

import (
	"context"
	"errors"
	"sort"
	"strings"

	run "cloud.google.com/go/run/apiv2"
	"cloud.google.com/go/run/apiv2/runpb"
	"google.golang.org/api/iterator"
)

func listServices(ctx context.Context, project string) ([]string, error) {
	client, err := run.NewServicesClient(ctx)
	if err != nil {
		return nil, err
	}
	defer client.Close()

	it := client.ListServices(ctx, &runpb.ListServicesRequest{
		Parent: "projects/" + project + "/locations/-",
	})
	var out []string
	for {
		s, err := it.Next()
		if errors.Is(err, iterator.Done) {
			break
		}
		if err != nil {
			return nil, err
		}
		// s.Name = "projects/<p>/locations/<r>/services/<name>"
		parts := strings.Split(s.GetName(), "/")
		if len(parts) > 0 {
			out = append(out, parts[len(parts)-1])
		}
	}
	sort.Strings(out)
	return out, nil
}
