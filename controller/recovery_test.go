package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/actions/scaleset"
	dockerclient "github.com/docker/docker/client"
)

func TestRecoverRunnersPreservesJobsAndCapacity(t *testing.T) {
	type priorRunner struct {
		instance, set, state string
	}
	runners := map[string]priorRunner{
		"running":        {"example", "docker-ci-example", "running"},
		"paused":         {"example", "docker-ci-example", "paused"},
		"restarting":     {"example", "docker-ci-example", "restarting"},
		"exited":         {"example", "docker-ci-example", "exited"},
		"created":        {"example", "docker-ci-example", "created"},
		"other-instance": {"other", "docker-ci-example", "running"},
		"other-set":      {"example", "docker-ci-other", "exited"},
	}
	waits := map[string]chan struct{}{}
	watchDone := map[string]chan struct{}{}
	for _, id := range []string{"running", "paused", "restarting"} {
		waits[id] = make(chan struct{})
		watchDone[id] = make(chan struct{})
	}
	var mu sync.Mutex
	removed := map[string]bool{}
	deleted := make(chan string, len(runners))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		path := strings.TrimPrefix(r.URL.Path, "/v1.48")
		if path == "/containers/json" {
			var filters map[string]map[string]bool
			if err := json.Unmarshal([]byte(r.URL.Query().Get("filters")), &filters); err != nil {
				t.Error(err)
			}
			for _, label := range []string{"managed=true", "kind=runner", "instance=example", "scale-set=docker-ci-example"} {
				if !filters["label"][labelPrefix+label] {
					t.Errorf("missing ownership filter %s", label)
				}
			}
			if r.URL.Query().Get("all") != "1" {
				t.Error("inactive runners were not requested")
			}
			var result []map[string]any
			for id := range runners {
				// Inspection must determine current state, even if the list is stale.
				result = append(result, map[string]any{"Id": id, "Names": []string{"/ci-fleet-example-" + id}, "State": "exited"})
			}
			json.NewEncoder(w).Encode(result)
			return
		}
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) < 2 || parts[0] != "containers" {
			t.Errorf("unexpected Docker request %s %s", r.Method, r.URL)
			http.Error(w, "unexpected request", http.StatusNotFound)
			return
		}
		id := parts[1]
		prior, ok := runners[id]
		if !ok {
			t.Errorf("unknown runner %s", id)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		switch {
		case len(parts) == 3 && parts[2] == "json":
			if wait := waits[id]; wait != nil {
				select {
				case <-wait:
					prior.state = "exited"
				default:
				}
			}
			json.NewEncoder(w).Encode(map[string]any{
				"Id": id, "Name": "/ci-fleet-example-" + id,
				"Config": map[string]any{"Labels": map[string]string{
					labelPrefix + "managed": "true", labelPrefix + "kind": "runner",
					labelPrefix + "instance": prior.instance, labelPrefix + "scale-set": prior.set,
				}},
				"State": map[string]any{"Status": prior.state, "Running": prior.state == "running", "Paused": prior.state == "paused", "Restarting": prior.state == "restarting"},
			})
		case len(parts) == 3 && parts[2] == "wait":
			if waits[id] == nil {
				t.Errorf("watched inactive or unrelated runner %s", id)
				w.WriteHeader(http.StatusNotFound)
				return
			}
			select {
			case <-waits[id]:
				json.NewEncoder(w).Encode(map[string]any{"StatusCode": 0})
			case <-r.Context().Done():
			}
			close(watchDone[id])
		case len(parts) == 3 && parts[2] == "logs":
			w.WriteHeader(http.StatusOK)
		case len(parts) == 2 && r.Method == http.MethodDelete:
			force := r.URL.Query().Get("force") == "1"
			if (id == "exited" || id == "created") && force {
				t.Error("startup cleanup forced removal of an inactive runner")
			}
			if id == "paused" && force {
				t.Error("exit watcher forced container removal")
			}
			if id == "other-instance" || id == "other-set" || id == "restarting" {
				t.Errorf("removed surviving or unrelated runner %s", id)
			}
			mu.Lock()
			removed[id] = true
			mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
			deleted <- id
		default:
			t.Errorf("unexpected Docker request %s %s", r.Method, r.URL)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	defer func() {
		for id, wait := range waits {
			select {
			case <-wait:
			default:
				close(wait)
			}
			select {
			case <-watchDone[id]:
			case <-time.After(time.Second):
				t.Errorf("watcher for %s did not finish", id)
			}
		}
	}()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler),
		config: Config{FleetInstance: "example", ScaleSetName: "docker-ci-example", MaxRunners: 3, StatusFile: t.TempDir() + "/status.json"}}
	ctx := context.Background()
	if err := scaler.recoverRunners(ctx); err != nil {
		t.Fatal(err)
	}
	if count, busy := scaler.runners.counts(); count != 3 || busy != 3 {
		t.Fatalf("surviving runners were not counted: current=%d busy=%d", count, busy)
	}
	mu.Lock()
	if len(removed) != 2 || !removed["exited"] || !removed["created"] {
		t.Errorf("startup removed %v", removed)
	}
	mu.Unlock()
	// A surviving job may have exited while its completion event was queued.
	for _, id := range []string{"exited", "created"} {
		name := "ci-fleet-example-" + id
		if err := scaler.HandleJobStarted(ctx, &scaleset.JobStarted{RunnerName: name}); err != nil {
			t.Fatalf("late start for recovered inactive runner rejected: %v", err)
		}
		if err := scaler.HandleJobCompleted(ctx, &scaleset.JobCompleted{RunnerName: name}); err != nil {
			t.Fatalf("late completion for recovered inactive runner rejected: %v", err)
		}
	}
	if count, err := scaler.HandleDesiredRunnerCount(ctx, 3); err != nil || count != 3 {
		t.Fatalf("recovery did not preserve capacity: count=%d err=%v", count, err)
	}
	name := "ci-fleet-example-running"
	if err := scaler.HandleJobStarted(ctx, &scaleset.JobStarted{RunnerName: name}); err != nil {
		t.Fatalf("replayed job start rejected: %v", err)
	}
	if err := scaler.HandleJobCompleted(ctx, &scaleset.JobCompleted{RunnerName: name}); err != nil {
		t.Fatalf("surviving job completion rejected: %v", err)
	}
	close(waits["running"])
	if scaler.runners.contains(name, "running") {
		t.Fatal("completed surviving runner remained tracked")
	}
	// A runner exit without a completion event must release capacity and clean up.
	close(waits["paused"])
	timeout := time.After(2 * time.Second)
waitForExit:
	for {
		select {
		case id := <-deleted:
			if id == "paused" {
				break waitForExit
			}
		case <-timeout:
			t.Fatal("recovered runner exit was not cleaned up")
		}
	}
	deadline := time.Now().Add(time.Second)
	for scaler.runners.count() != 1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if count, busy := scaler.runners.counts(); count != 1 || busy != 1 {
		t.Fatalf("completion and exit did not release capacity: current=%d busy=%d", count, busy)
	}
	if err := scaler.HandleJobCompleted(ctx, &scaleset.JobCompleted{RunnerName: "ci-fleet-example-paused"}); err != nil {
		t.Fatalf("late completion for recovered exit rejected: %v", err)
	}
	// Leave the last active runner untracked before releasing its simulated wait.
	scaler.runners.markDone("ci-fleet-example-restarting")
}

func TestRecoveryNeverForcesARunnerThatStartsDuringCleanup(t *testing.T) {
	removed := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/containers/json"):
			json.NewEncoder(w).Encode([]map[string]string{{"Id": "prior"}})
		case strings.HasSuffix(r.URL.Path, "/prior/json"):
			json.NewEncoder(w).Encode(map[string]any{"Name": "/ci-fleet-example-prior", "State": map[string]any{"Status": "exited"},
				"Config": map[string]any{"Labels": map[string]string{labelPrefix + "managed": "true", labelPrefix + "kind": "runner", labelPrefix + "instance": "example", labelPrefix + "scale-set": "docker-ci-example"}}})
		case strings.HasSuffix(r.URL.Path, "/logs"):
			w.WriteHeader(http.StatusOK)
		case r.Method == http.MethodDelete:
			if r.URL.Query().Get("force") == "1" {
				removed = true
				w.WriteHeader(http.StatusNoContent)
				return
			}
			// Docker rejects non-forced removal if it started since inspection.
			w.WriteHeader(http.StatusConflict)
			json.NewEncoder(w).Encode(map[string]string{"message": "container is running"})
		default:
			t.Errorf("unexpected request %s", r.URL)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler), config: Config{FleetInstance: "example", ScaleSetName: "docker-ci-example"}}
	if err := scaler.recoverRunners(context.Background()); err == nil {
		t.Fatal("startup must retry after the concurrent start")
	}
	if removed {
		t.Fatal("startup killed a runner that became active")
	}
}

func TestRecoveredRunnerSurvivesDockerRestartNotification(t *testing.T) {
	stop := make(chan struct{})
	watchingRestart := make(chan struct{})
	deleted := make(chan struct{})
	var waitCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/containers/json"):
			json.NewEncoder(w).Encode([]map[string]string{{"Id": "prior"}})
		case strings.HasSuffix(r.URL.Path, "/prior/json"):
			running := true
			select {
			case <-stop:
				running = false
			default:
			}
			json.NewEncoder(w).Encode(map[string]any{"Name": "/ci-fleet-example-prior",
				"State":  map[string]any{"Running": running, "Restarting": running && waitCalls.Load() > 0},
				"Config": map[string]any{"Labels": map[string]string{labelPrefix + "managed": "true", labelPrefix + "kind": "runner", labelPrefix + "instance": "example", labelPrefix + "scale-set": "docker-ci-example"}}})
		case strings.HasSuffix(r.URL.Path, "/wait"):
			if waitCalls.Add(1) > 1 {
				close(watchingRestart)
				select {
				case <-stop:
				case <-r.Context().Done():
					return
				}
			}
			// Docker wakes an already-installed waiter when it begins restarting.
			json.NewEncoder(w).Encode(map[string]any{"StatusCode": 0})
		case strings.HasSuffix(r.URL.Path, "/logs"):
			w.WriteHeader(http.StatusOK)
		case r.Method == http.MethodDelete:
			if r.URL.Query().Get("force") == "1" {
				t.Error("exit watcher forced container removal")
			}
			select {
			case <-stop:
			default:
				t.Error("removed runner during Docker restart")
			}
			w.WriteHeader(http.StatusNoContent)
			close(deleted)
		default:
			t.Errorf("unexpected request %s", r.URL)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	defer func() {
		select {
		case <-stop:
		default:
			close(stop)
		}
	}()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler),
		config: Config{FleetInstance: "example", ScaleSetName: "docker-ci-example", StatusFile: t.TempDir() + "/status.json"}}
	if err := scaler.recoverRunners(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-watchingRestart:
	case <-time.After(2 * time.Second):
		t.Fatal("watcher did not preserve restarting runner")
	}
	if count, busy := scaler.runners.counts(); count != 1 || busy != 1 {
		t.Fatalf("restarting runner lost capacity tracking: current=%d busy=%d", count, busy)
	}
	select {
	case <-deleted:
		t.Fatal("restarting runner removed")
	default:
	}
	close(stop)
	select {
	case <-deleted:
	case <-time.After(2 * time.Second):
		t.Fatal("final exit was not cleaned up")
	}
	deadline := time.Now().Add(time.Second)
	for scaler.runners.count() != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if scaler.runners.count() != 0 {
		t.Fatal("final exit retained runner capacity")
	}
}
