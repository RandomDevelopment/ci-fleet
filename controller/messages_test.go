package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
)

func TestMessageAcquisitionPrecedesCallbacksAndAdmittedCleanupSurvivesCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	const first = "ci-fleet-example-0123abcd"
	const second = "ci-fleet-example-89abcdef"
	client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
		MessageID: 9, Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{RunnerRequestID: 71}}},
		JobStartedMessages:   []*scaleset.JobStarted{{RunnerName: first}, {RunnerName: second}},
		JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: first}, {RunnerName: second}},
	}}
	var acquired, removals atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if acquired.Load() != 1 || client.acknowledged.Load() {
			t.Error("cleanup ran before acquisition or after acknowledgment")
		}
		switch {
		case strings.HasSuffix(r.URL.Path, "/logs"):
			w.WriteHeader(http.StatusOK)
		case r.Method == http.MethodDelete:
			if removals.Add(1) == 1 {
				cancel()
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected Docker request: %s %s", r.Method, r.URL)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer server.Close()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost(server.URL), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, messageClient: client,
		logger: slog.New(slog.DiscardHandler), config: Config{FleetInstance: "example", MaxRunners: 2, StatusFile: t.TempDir() + "/status.json"}}
	scaler.runners.addIdle(first, "first-id")
	scaler.runners.addBusy(second, "second-id")
	client.acquireJobs = func(ctx context.Context, ids []int64) ([]int64, error) {
		if ctx.Err() != nil || !slices.Equal(ids, []int64{71}) {
			t.Fatalf("invalid acquisition: %v, %v", ids, ctx.Err())
		}
		if _, busy := scaler.runners.counts(); busy != 1 {
			t.Fatal("job-start callbacks ran before acquisition")
		}
		acquired.Add(1)
		return ids, nil
	}
	l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 2})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Run(ctx, scaler); !errors.Is(err, context.Canceled) {
		t.Fatalf("listener result: %v", err)
	}
	if !client.acknowledged.Load() || removals.Load() != 2 || scaler.runners.count() != 0 {
		t.Fatalf("cancellation interrupted admitted cleanup: ack=%v removed=%d runners=%d", client.acknowledged.Load(), removals.Load(), scaler.runners.count())
	}
}

func TestMessageReplayAfterCleanupOrAcknowledgmentFailure(t *testing.T) {
	for _, failure := range []string{"cleanup", "acknowledgment"} {
		t.Run(failure, func(t *testing.T) {
			const first = "ci-fleet-example-0123abcd"
			const second = "ci-fleet-example-89abcdef"
			client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
				MessageID: 10, Statistics: &scaleset.RunnerScaleSetStatistic{},
				JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{RunnerRequestID: 71}}},
				JobStartedMessages:   []*scaleset.JobStarted{{RunnerName: first}, {RunnerName: second}},
				JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: first}, {RunnerName: second}},
			}}
			if failure == "acknowledgment" {
				client.deleteError = errors.New("acknowledgment failed")
			}
			var acquisitions, firstRemovals, secondRemovals atomic.Int32
			var firstGone, secondGone atomic.Bool
			client.acquireJobs = func(_ context.Context, ids []int64) ([]int64, error) {
				if !slices.Equal(ids, []int64{71}) {
					t.Fatalf("changed request IDs on replay: %v", ids)
				}
				acquisitions.Add(1)
				return nil, nil // The service can return no IDs for an already acquired job.
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/logs"):
					w.WriteHeader(http.StatusOK)
				case strings.HasSuffix(r.URL.Path, "/"+first+"/json") && firstGone.Load(),
					strings.HasSuffix(r.URL.Path, "/"+second+"/json") && secondGone.Load():
					w.WriteHeader(http.StatusNotFound)
					json.NewEncoder(w).Encode(map[string]string{"message": "No such container"})
				case r.Method == http.MethodDelete && strings.HasSuffix(r.URL.Path, "/first-id"):
					firstRemovals.Add(1)
					firstGone.Store(true)
					w.WriteHeader(http.StatusNoContent)
				case r.Method == http.MethodDelete && strings.HasSuffix(r.URL.Path, "/second-id"):
					if secondRemovals.Add(1) == 1 && failure == "cleanup" {
						w.WriteHeader(http.StatusInternalServerError)
						json.NewEncoder(w).Encode(map[string]string{"message": "cleanup failed"})
						return
					}
					secondGone.Store(true)
					w.WriteHeader(http.StatusNoContent)
				default:
					t.Errorf("unexpected Docker request: %s %s", r.Method, r.URL)
					w.WriteHeader(http.StatusInternalServerError)
				}
			}))
			defer server.Close()
			docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost(server.URL), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
			if err != nil {
				t.Fatal(err)
			}
			defer docker.Close()
			scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, messageClient: client,
				logger: slog.New(slog.DiscardHandler), config: Config{FleetInstance: "example", MaxRunners: 2, StatusFile: t.TempDir() + "/status.json"}}
			scaler.runners.addBusy(first, "first-id")
			scaler.runners.addBusy(second, "second-id")
			l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 2})
			if err != nil {
				t.Fatal(err)
			}
			if err := l.Run(context.Background(), scaler); err == nil || !strings.Contains(err.Error(), failure+" failed") {
				t.Fatalf("lost batch failure: %v", err)
			}
			wantRetained := failure == "cleanup"
			if client.acknowledged.Load() || scaler.runners.contains(first, "first-id") || scaler.runners.contains(second, "second-id") != wantRetained {
				t.Fatal("failed batch was acknowledged or cleanup tracking was lost")
			}
			client.polls, client.deleteError = 0, nil
			l, err = listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 2})
			if err != nil {
				t.Fatal(err)
			}
			if err := l.Run(context.Background(), scaler); !errors.Is(err, context.Canceled) {
				t.Fatalf("batch replay failed: %v", err)
			}
			wantSecondRemovals := int32(1)
			if wantRetained {
				wantSecondRemovals = 2
			}
			if !client.acknowledged.Load() || acquisitions.Load() != 2 || firstRemovals.Load() != 1 || secondRemovals.Load() != wantSecondRemovals || scaler.runners.count() != 0 {
				t.Fatalf("replay changed effects: ack=%v acquisitions=%d removals=%d/%d runners=%d", client.acknowledged.Load(), acquisitions.Load(), firstRemovals.Load(), secondRemovals.Load(), scaler.runners.count())
			}
		})
	}
}

func TestAcquisitionFailureLeavesBatchUnacknowledgedAndRunnersIntact(t *testing.T) {
	client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
		MessageID: 11, Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{RunnerRequestID: 71}}},
		JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: "retained"}},
	}}
	want := errors.New("acquisition unavailable")
	client.acquireJobs = func(context.Context, []int64) ([]int64, error) { return nil, want }
	scaler := &Scaler{runners: newRunnerState(), messageClient: client, logger: slog.New(slog.DiscardHandler),
		config: Config{MaxRunners: 1, StatusFile: t.TempDir() + "/status.json"}}
	scaler.runners.addBusy("retained", "retained-id")
	l, err := listener.New(client, listener.Config{ScaleSetID: 42, MaxRunners: 1})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Run(context.Background(), scaler); !errors.Is(err, want) {
		t.Fatalf("lost acquisition error: %v", err)
	}
	if client.acknowledged.Load() || !scaler.runners.contains("retained", "retained-id") {
		t.Fatal("acquisition failure acknowledged the message or changed runner ownership")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := scaler.Scale(ctx, client.message); !errors.Is(err, context.Canceled) {
		t.Fatalf("already canceled batch was admitted: %v", err)
	}
}

type unavailableJitClient struct{ calls int }

func (c *unavailableJitClient) GenerateJitRunnerConfig(context.Context, *scaleset.RunnerScaleSetJitRunnerSetting, int) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	c.calls++
	return nil, errors.New("JIT temporarily unavailable")
}

func TestNilPollReconcilesUsingInitialAssignedJobCount(t *testing.T) {
	jit := &unavailableJitClient{}
	scaler := &Scaler{runners: newRunnerState(), scalesetClient: jit, logger: slog.New(slog.DiscardHandler),
		maintenanceLockPath: t.TempDir() + "/maintenance.lock",
		config:              Config{MaxRunners: 2, StatusFile: t.TempDir() + "/status.json"}}
	scaler.runners.addIdle("one", "one-id")
	scaler.runners.addIdle("two", "two-id")
	if err := scaler.Scale(context.Background(), &scaleset.RunnerScaleSetMessage{
		MessageID: listener.InitialMessageID, Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 2},
	}); err != nil {
		t.Fatal(err)
	}
	scaler.runners.forget("one", "one-id")
	if err := scaler.Scale(context.Background(), nil); err == nil || !strings.Contains(err.Error(), "JIT temporarily unavailable") || jit.calls != 1 {
		t.Fatalf("idle poll forgot outstanding demand: calls=%d err=%v", jit.calls, err)
	}
	if err := scaler.Scale(context.Background(), &scaleset.RunnerScaleSetMessage{}); err == nil {
		t.Fatal("message without statistics was accepted")
	}
}
