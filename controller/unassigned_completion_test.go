package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
)

func unassignedCancellation() scaleset.JobCompleted {
	return scaleset.JobCompleted{Result: "canceled", JobMessageBase: scaleset.JobMessageBase{JobID: "unassigned-job", RunnerRequestID: 7}}
}

func TestUnassignedCancellationRequiresUnambiguousIdentity(t *testing.T) {
	for _, test := range []struct {
		name string
		edit func(*scaleset.JobCompleted)
	}{
		{name: "unassigned cancellation"},
		{name: "missing result", edit: func(j *scaleset.JobCompleted) { j.Result = "" }},
		{name: "success", edit: func(j *scaleset.JobCompleted) { j.Result = "success" }},
		{name: "failure", edit: func(j *scaleset.JobCompleted) { j.Result = "failure" }},
		{name: "different spelling", edit: func(j *scaleset.JobCompleted) { j.Result = "cancelled" }},
		{name: "different case", edit: func(j *scaleset.JobCompleted) { j.Result = "Canceled" }},
		{name: "runner ID", edit: func(j *scaleset.JobCompleted) { j.RunnerID = 1 }},
		{name: "negative runner ID", edit: func(j *scaleset.JobCompleted) { j.RunnerID = -1 }},
		{name: "runner assignment", edit: func(j *scaleset.JobCompleted) { j.RunnerAssignTime = time.Unix(1, 0) }},
		{name: "missing job ID", edit: func(j *scaleset.JobCompleted) { j.JobID = "" }},
		{name: "missing request ID", edit: func(j *scaleset.JobCompleted) { j.RunnerRequestID = 0 }},
		{name: "negative request ID", edit: func(j *scaleset.JobCompleted) { j.RunnerRequestID = -1 }},
		{name: "unrelated name", edit: func(j *scaleset.JobCompleted) { j.RunnerName = "other-runner" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			job := unassignedCancellation()
			if test.edit != nil {
				test.edit(&job)
			}
			scaler := &Scaler{runners: newRunnerState(), logger: slog.New(slog.DiscardHandler), config: Config{FleetInstance: "example"}}
			scaler.runners.addBusy("ci-fleet-example-0123abcd", "retained")
			err := scaler.HandleJobCompleted(context.Background(), &job)
			if (err != nil) != (test.edit != nil) {
				t.Fatalf("completion error = %v", err)
			}
			if count, busy := scaler.runners.counts(); count != 1 || busy != 1 {
				t.Fatalf("unassigned event changed runners: count=%d busy=%d", count, busy)
			}
			if err := scaler.HandleJobStarted(context.Background(), &scaleset.JobStarted{}); err == nil {
				t.Fatal("blank job start was accepted")
			}
		})
	}
}

func TestUnknownBlankCompletionRetainsDiagnosticFields(t *testing.T) {
	var output bytes.Buffer
	scaler := &Scaler{runners: newRunnerState(), logger: slog.New(slog.NewJSONHandler(&output, nil))}
	job := scaleset.JobCompleted{Result: "unexpected", RunnerID: 12, JobMessageBase: scaleset.JobMessageBase{
		JobID: "unknown-job", RunnerRequestID: 13, WorkflowRunID: 14, RunnerAssignTime: time.Unix(1, 0).UTC(),
	}}
	if err := scaler.HandleJobCompleted(context.Background(), &job); err == nil {
		t.Fatal("contradictory blank completion was accepted")
	}
	var event map[string]any
	if err := json.NewDecoder(&output).Decode(&event); err != nil {
		t.Fatal(err)
	}
	for key, want := range map[string]any{"runner": "", "result": "unexpected", "runnerID": float64(12),
		"jobID": "unknown-job", "runnerRequestID": float64(13), "workflowRunID": float64(14), "runnerAssignTime": "1970-01-01T00:00:01Z"} {
		if event[key] != want {
			t.Errorf("diagnostic %s = %v, want %v", key, event[key], want)
		}
	}
}

func TestUnassignedCompletionPreservesTheRestOfTheSDKBatch(t *testing.T) {
	for _, invalid := range []bool{false, true} {
		name := "documented cancellation"
		if invalid {
			name = "contradictory cancellation"
		}
		t.Run(name, func(t *testing.T) {
			job := unassignedCancellation()
			if invalid {
				job.RunnerID = 1
			}
			const live = "ci-fleet-example-0123abcd"
			client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
				MessageID: 8, Statistics: &scaleset.RunnerScaleSetStatistic{},
				JobCompletedMessages: []*scaleset.JobCompleted{&job, {RunnerName: live, Result: "canceled"}},
			}}
			var removals atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if client.acknowledged.Load() {
					t.Error("SDK acknowledged the batch before cleanup")
				}
				switch {
				case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/containers/live-id/logs"):
					w.WriteHeader(http.StatusOK)
				case r.Method == http.MethodDelete && strings.HasSuffix(r.URL.Path, "/containers/live-id"):
					removals.Add(1)
					w.WriteHeader(http.StatusNoContent)
				default:
					t.Errorf("unexpected Docker request: %s %s", r.Method, r.URL)
					w.WriteHeader(http.StatusInternalServerError)
				}
			}))
			defer server.Close()
			docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
			if err != nil {
				t.Fatal(err)
			}
			defer docker.Close()
			scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, logger: slog.New(slog.DiscardHandler),
				config: Config{FleetInstance: "example", MaxRunners: 1, StatusFile: t.TempDir() + "/status.json"}}
			scaler.runners.addBusy(live, "live-id")
			l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 1})
			if err != nil {
				t.Fatal(err)
			}
			err = l.Run(context.Background(), scaler)
			if client.acknowledged.Load() == invalid {
				t.Fatal("SDK acknowledgment did not match batch success")
			}
			if invalid {
				if err == nil || !strings.Contains(err.Error(), "unknown runner") || client.polls != 1 || removals.Load() != 0 || scaler.runners.count() != 1 {
					t.Fatalf("invalid completion was not rejected: err=%v polls=%d removals=%d", err, client.polls, removals.Load())
				}
			} else if !errors.Is(err, context.Canceled) || client.polls != 2 || removals.Load() != 1 || scaler.runners.count() != 0 {
				t.Fatalf("valid cancellation interrupted the batch: err=%v polls=%d removals=%d", err, client.polls, removals.Load())
			}
		})
	}
}
