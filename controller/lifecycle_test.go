package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
	"github.com/google/uuid"
)

type lifecycleBatchClient struct {
	message      *scaleset.RunnerScaleSetMessage
	polls        int
	acknowledged atomic.Bool
}

func (c *lifecycleBatchClient) Session() scaleset.RunnerScaleSetSession {
	return scaleset.RunnerScaleSetSession{SessionID: uuid.New(), Statistics: &scaleset.RunnerScaleSetStatistic{}}
}

func (c *lifecycleBatchClient) GetMessage(context.Context, int, int) (*scaleset.RunnerScaleSetMessage, error) {
	c.polls++
	if c.polls == 1 {
		return c.message, nil
	}
	return nil, context.Canceled
}

func (c *lifecycleBatchClient) DeleteMessage(_ context.Context, id int) error {
	if id != c.message.MessageID {
		return fmt.Errorf("unexpected message ID %d", id)
	}
	c.acknowledged.Store(true)
	return nil
}

func (c *lifecycleBatchClient) AcquireJobs(context.Context, []int64) ([]int64, error) {
	return nil, fmt.Errorf("unexpected job acquisition")
}

func TestLateEventsAfterRestartDoNotDiscardTheRestOfTheSDKBatch(t *testing.T) {
	const gone = "ci-fleet-example-0123abcd"
	const live = "ci-fleet-example-89abcdef"
	client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
		MessageID: 7, Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobStartedMessages:   []*scaleset.JobStarted{{RunnerName: gone}, {RunnerName: live}},
		JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: gone}, {RunnerName: gone}, {RunnerName: live}, {RunnerName: live}},
	}}
	var removed atomic.Bool
	var removals, missingInspections atomic.Int32
	exited := make(chan struct{})
	var stop sync.Once
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		path := strings.TrimPrefix(r.URL.Path, "/v1.48")
		switch {
		case path == "/containers/json":
			// The earlier controller removed gone; only the surviving job can be recovered.
			json.NewEncoder(w).Encode([]map[string]string{{"Id": "live-id"}})
		case path == "/containers/"+gone+"/json":
			if !client.acknowledged.Load() {
				t.Error("expected the pinned SDK to acknowledge the batch before callbacks")
			}
			missingInspections.Add(1)
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]string{"message": "No such container"})
		case path == "/containers/live-id/json" || path == "/containers/"+live+"/json":
			if removed.Load() {
				w.WriteHeader(http.StatusNotFound)
				json.NewEncoder(w).Encode(map[string]string{"message": "No such container"})
				return
			}
			json.NewEncoder(w).Encode(map[string]any{
				"Id": "live-id", "Name": "/" + live, "State": map[string]bool{"Running": true},
				"Config": map[string]any{"Labels": map[string]string{
					labelPrefix + "managed": "true", labelPrefix + "kind": "runner",
					labelPrefix + "instance": "example", labelPrefix + "scale-set": "docker-ci-example",
				}},
			})
		case path == "/containers/live-id/wait":
			select {
			case <-exited:
				json.NewEncoder(w).Encode(map[string]int{"StatusCode": 0})
			case <-r.Context().Done():
			}
		case path == "/containers/live-id/logs":
			w.WriteHeader(http.StatusOK)
		case path == "/containers/live-id" && r.Method == http.MethodDelete:
			removals.Add(1)
			removed.Store(true)
			stop.Do(func() { close(exited) })
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected Docker request %s %s", r.Method, r.URL)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	defer stop.Do(func() { close(exited) })
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler),
		config: Config{FleetInstance: "example", ScaleSetName: "docker-ci-example", MaxRunners: 1, StatusFile: t.TempDir() + "/status.json"}}
	if err := scaler.recoverRunners(context.Background()); err != nil {
		t.Fatal(err)
	}
	if scaler.runners.contains(gone, "gone-id") {
		t.Fatal("restart unexpectedly recovered the already-removed runner")
	}
	l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 1})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Run(context.Background(), scaler); !errors.Is(err, context.Canceled) {
		t.Fatalf("late events interrupted the SDK batch: %v", err)
	}
	if !client.acknowledged.Load() || client.polls != 2 || missingInspections.Load() != 3 {
		t.Fatalf("batch was not fully processed: acknowledged=%v polls=%d late events=%d", client.acknowledged.Load(), client.polls, missingInspections.Load())
	}
	if !removed.Load() || removals.Load() != 1 || scaler.runners.count() != 0 {
		t.Fatalf("later completion or duplicate was mishandled: removed=%v removals=%d runners=%d", removed.Load(), removals.Load(), scaler.runners.count())
	}
}

func TestUntrackedRunnerEventsRetainUnrelatedAndDockerErrors(t *testing.T) {
	for _, test := range []struct {
		name   string
		status int
		want   string
	}{
		{name: "ci-fleet-other-0123abcd", want: "unknown runner"},
		{name: "ci-fleet-example-other-0123abcd", want: "unknown runner"},
		{name: "ci-fleet-example-0123abcg", want: "unknown runner"},
		{name: "ci-fleet-example-0123abcd0", want: "unknown runner"},
		{name: "ci-fleet-example-0123abcd", status: http.StatusOK, want: "unknown runner"},
		{name: "ci-fleet-example-0123abcd", status: http.StatusInternalServerError, want: "inspect untracked runner"},
	} {
		t.Run(fmt.Sprintf("%s/%d", test.name, test.status), func(t *testing.T) {
			var inspections atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				inspections.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if test.status == 0 || !strings.HasSuffix(r.URL.Path, "/"+test.name+"/json") || r.Method != http.MethodGet {
					t.Errorf("unrelated event accessed Docker: %s %s", r.Method, r.URL)
					w.WriteHeader(http.StatusInternalServerError)
					return
				}
				w.WriteHeader(test.status)
				json.NewEncoder(w).Encode(map[string]string{"Id": "untracked", "message": "inspection error"})
			}))
			defer server.Close()
			docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
			if err != nil {
				t.Fatal(err)
			}
			defer docker.Close()
			scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler), config: Config{FleetInstance: "example"}}
			ctx := context.Background()
			for _, err := range []error{
				scaler.HandleJobStarted(ctx, &scaleset.JobStarted{RunnerName: test.name}),
				scaler.HandleJobCompleted(ctx, &scaleset.JobCompleted{RunnerName: test.name}),
			} {
				if err == nil || !strings.Contains(err.Error(), test.want) {
					t.Errorf("untracked runner error was suppressed: %v", err)
				}
			}
			wantInspections := int32(0)
			if test.status != 0 {
				wantInspections = 2
			}
			if inspections.Load() != wantInspections {
				t.Errorf("inspections=%d, want %d", inspections.Load(), wantInspections)
			}
		})
	}
}
