package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
)

func TestOverlappingRunnerCleanupPreservesTheListenerBatch(t *testing.T) {
	const runner = "ci-fleet-example-0123abcd"
	const other = "ci-fleet-example-89abcdef"
	removing := make(chan struct{})
	finishRemoval := make(chan struct{})
	removed := make(chan struct{})
	var finish sync.Once
	var removals, otherRemovals atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		path := strings.TrimPrefix(r.URL.Path, "/v1.48")
		switch {
		case path == "/containers/runner-id/wait":
			switch r.URL.Query().Get("condition") {
			case "not-running":
				json.NewEncoder(w).Encode(map[string]int{"StatusCode": 0})
			case "removed":
				// Release the watcher's DELETE only once completion waits for removal.
				finish.Do(func() { close(finishRemoval) })
				select {
				case <-removed:
					json.NewEncoder(w).Encode(map[string]int{"StatusCode": 0})
				case <-r.Context().Done():
				}
			default:
				t.Errorf("unexpected wait condition: %s", r.URL)
				w.WriteHeader(http.StatusBadRequest)
			}
		case path == "/containers/runner-id/json":
			json.NewEncoder(w).Encode(map[string]any{"State": map[string]bool{"Running": false}})
		case strings.HasSuffix(path, "/logs"):
			w.WriteHeader(http.StatusOK)
		case path == "/containers/runner-id" && r.Method == http.MethodDelete:
			if removals.Add(1) == 1 {
				if r.URL.Query().Get("force") == "1" {
					t.Error("exit watcher forced removal")
				}
				close(removing)
				select {
				case <-finishRemoval:
					close(removed)
					w.WriteHeader(http.StatusNoContent)
				case <-r.Context().Done():
				}
			} else {
				if r.URL.Query().Get("force") != "1" {
					t.Error("job completion did not force removal")
				}
				w.WriteHeader(http.StatusConflict)
				json.NewEncoder(w).Encode(map[string]string{"message": "removal of container runner-id is already in progress"})
			}
		case path == "/containers/other-id" && r.Method == http.MethodDelete:
			otherRemovals.Add(1)
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected Docker request %s %s", r.Method, r.URL)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	defer finish.Do(func() { close(finishRemoval) })
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler),
		config: Config{MaxRunners: 2, StatusFile: t.TempDir() + "/status.json"}}
	scaler.runners.addBusy(runner, "runner-id")
	scaler.runners.addBusy(other, "other-id")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	watchDone := make(chan struct{})
	go func() {
		scaler.watchRunner(ctx, runner, "runner-id")
		close(watchDone)
	}()
	select {
	case <-removing:
	case <-ctx.Done():
		t.Fatal("watcher did not begin removal")
	}
	client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
		MessageID: 7, Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: runner}, {RunnerName: other}},
	}}
	l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 2})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Run(ctx, scaler); !errors.Is(err, context.Canceled) || ctx.Err() != nil {
		t.Fatalf("overlapping cleanup interrupted the listener batch: %v", err)
	}
	select {
	case <-watchDone:
	case <-ctx.Done():
		t.Fatal("exit watcher did not finish")
	}
	if !client.acknowledged.Load() || client.polls != 2 || removals.Load() != 2 || otherRemovals.Load() != 1 {
		t.Fatalf("batch did not finish: acknowledged=%v polls=%d removals=%d other removals=%d", client.acknowledged.Load(), client.polls, removals.Load(), otherRemovals.Load())
	}
	if scaler.runners.contains(runner, "runner-id") || scaler.runners.count() != 0 {
		t.Fatal("completed runners retained lifecycle state or capacity")
	}
	select {
	case <-removed:
	default:
		t.Fatal("cleanup succeeded before container removal")
	}
}

func TestRunnerRemovalResults(t *testing.T) {
	const inProgress = "removal of container runner-id is already in progress"
	for _, test := range []struct {
		name, message, waitBody, want string
		removeStatus, waitStatus      int
		cancelWait                    bool
	}{
		{name: "removed", removeStatus: http.StatusNoContent},
		{name: "already absent", removeStatus: http.StatusNotFound},
		{name: "daemon error", removeStatus: http.StatusInternalServerError, message: "daemon failure", want: "daemon failure"},
		{name: "permission denied", removeStatus: http.StatusForbidden, message: "permission denied", want: "permission denied"},
		{name: "unrelated conflict", removeStatus: http.StatusConflict, message: "container is running", want: "container is running"},
		{name: "another container", removeStatus: http.StatusConflict, message: "removal of container other-id is already in progress", want: "other-id"},
		{name: "wrong error class", removeStatus: http.StatusInternalServerError, message: inProgress, want: inProgress},
		{name: "removed during wait", removeStatus: http.StatusConflict, message: inProgress, waitStatus: http.StatusOK, waitBody: `{"StatusCode":0}`},
		{name: "absent before wait", removeStatus: http.StatusConflict, message: inProgress, waitStatus: http.StatusNotFound},
		{name: "wait daemon error", removeStatus: http.StatusConflict, message: inProgress, waitStatus: http.StatusInternalServerError, want: "wait failure"},
		{name: "wait result error", removeStatus: http.StatusConflict, message: inProgress, waitStatus: http.StatusOK, waitBody: `{"Error":{"Message":"removal failed"}}`, want: "removal failed"},
		{name: "incomplete wait response", removeStatus: http.StatusConflict, message: inProgress, waitStatus: http.StatusOK, waitBody: `{"StatusCode":`, want: "unexpected EOF"},
		{name: "deadline", removeStatus: http.StatusConflict, message: inProgress, want: "context deadline exceeded"},
		{name: "cancellation", removeStatus: http.StatusConflict, message: inProgress, want: "context canceled", cancelWait: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 250*time.Millisecond)
			defer cancel()
			var waits atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/logs"):
					w.WriteHeader(http.StatusOK)
				case r.Method == http.MethodDelete:
					w.WriteHeader(test.removeStatus)
					if test.removeStatus != http.StatusNoContent {
						json.NewEncoder(w).Encode(map[string]string{"message": test.message})
					}
				case strings.HasSuffix(r.URL.Path, "/wait"):
					waits.Add(1)
					if r.URL.Query().Get("condition") != "removed" {
						t.Error("wait did not require container removal")
					}
					if test.waitStatus == 0 {
						if test.cancelWait {
							cancel()
						}
						<-r.Context().Done()
						return
					}
					w.WriteHeader(test.waitStatus)
					if test.waitStatus != http.StatusOK {
						json.NewEncoder(w).Encode(map[string]string{"message": "wait failure"})
					} else {
						w.Write([]byte(test.waitBody))
					}
				default:
					t.Errorf("unexpected Docker request %s %s", r.Method, r.URL)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
			if err != nil {
				t.Fatal(err)
			}
			defer docker.Close()
			scaler := &Scaler{dockerClient: docker, logger: slog.New(slog.DiscardHandler)}
			err = scaler.logAndRemove(ctx, "runner", "runner-id", true)
			if test.want == "" && err != nil || test.want != "" && (err == nil || !strings.Contains(err.Error(), test.want)) {
				t.Fatalf("cleanup error = %v, want %q", err, test.want)
			}
			wantWaits := int32(0)
			if test.removeStatus == http.StatusConflict && test.message == inProgress {
				wantWaits = 1
			}
			if waits.Load() != wantWaits {
				t.Fatalf("removal waits = %d, want %d", waits.Load(), wantWaits)
			}
		})
	}
}
