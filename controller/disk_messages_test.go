package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
)

func TestDiskBlockedPinnedListenerAcknowledgesOfferAndCompletesActiveJob(t *testing.T) {
	var acquisitions, removals atomic.Int32
	client := &lifecycleBatchClient{message: &scaleset.RunnerScaleSetMessage{
		MessageID: 20, Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{RunnerRequestID: 99}}},
		JobStartedMessages:   []*scaleset.JobStarted{{RunnerName: "prior-assignment"}},
		JobCompletedMessages: []*scaleset.JobCompleted{{RunnerName: "prior-assignment", Result: "success"}},
	}, acquireJobs: func(context.Context, []int64) ([]int64, error) { acquisitions.Add(1); return nil, nil }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if client.acknowledged.Load() {
			t.Error("batch acknowledged before completion")
		}
		switch {
		case strings.HasSuffix(r.URL.Path, "/logs"):
			w.WriteHeader(200)
		case r.Method == http.MethodDelete:
			removals.Add(1)
			w.WriteHeader(204)
		default:
			t.Errorf("unexpected mutation %s", r.URL.Path)
			w.WriteHeader(500)
		}
	}))
	defer server.Close()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost(server.URL), dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	disk := admittedDisk()
	disk.read = func() (diskMeasurement, error) {
		m := healthyDiskMeasurement()
		m.Filesystems[0].AvailableBytes = diskInt(0)
		return m, nil
	}
	scaler := &Scaler{disk: disk, runners: newRunnerState(), dockerClient: docker, messageClient: client, logger: slog.New(slog.DiscardHandler), config: Config{MaxRunners: 2, StatusFile: t.TempDir() + "/status.json"}}
	scaler.enrollment = &controllerEnrollment{ScaleSetName: "example-set", MatchedAt: 1, SessionOpenedAt: 2}
	scaler.runners.addIdle("prior-assignment", "active-id")
	scaler.runners.addIdle("prewarm", "idle-id")
	l, err := listener.New(diskAwareSession{Client: client, disk: disk, pollAt: &scaler.listenerPollAt}, listener.Config{ScaleSetID: 42, MaxRunners: 2})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Run(context.Background(), scaler); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if !client.acknowledged.Load() || acquisitions.Load() != 0 || removals.Load() != 1 || !scaler.runners.contains("prewarm", "idle-id") {
		t.Fatal("disk block acquired an offer, interrupted cleanup, or stopped a prewarmed runner")
	}
	if !slices.Equal(client.pollCapacity, []int{0, 0}) {
		t.Fatalf("listener advertised capacity during pressure: %v", client.pollCapacity)
	}
	data, err := os.ReadFile(scaler.config.StatusFile)
	if err != nil {
		t.Fatal(err)
	}
	var status controllerStatus
	if err := json.Unmarshal(data, &status); err != nil {
		t.Fatal(err)
	}
	if status.Enrollment == nil || status.Enrollment.MatchedAt != 1 || status.Enrollment.SessionOpenedAt != 2 ||
		status.Enrollment.PolledAt <= status.Enrollment.SessionOpenedAt ||
		status.Enrollment.PolledAt != scaler.listenerPollAt.Load() {
		t.Fatalf("status did not retain the successful poll across listener failure: %+v", status.Enrollment)
	}
	if status.DiskAdmission.State != "low_disk" || status.DiskAdmission.Reason != "usage_percent" {
		t.Fatalf("missing low-disk status: %+v", status.DiskAdmission)
	}
	fs := status.DiskAdmission.Filesystems[0]
	if fs.CapacityBytes == nil || *fs.CapacityBytes != 1000<<30 || fs.AvailableBytes == nil || *fs.AvailableBytes != 0 {
		t.Fatalf("missing disk capacity or available bytes: %+v", fs)
	}
	t.Logf("admission=%s reason=%s capacity_bytes=%d available_bytes=%d acquisitions=%d completed=%d",
		status.DiskAdmission.State, status.DiskAdmission.Reason, *fs.CapacityBytes, *fs.AvailableBytes, acquisitions.Load(), removals.Load())
}

// This proves local handling of a subsequently offered ID, NOT that GitHub
// re-offers an acknowledged but unacquired job. Server liveness is a separate gate.
func TestDiskRecoveryAcquiresOnlyWhenServiceOffersAgain(t *testing.T) {
	available := int64(100 << 30)
	d := admittedDisk()
	d.read = func() (diskMeasurement, error) {
		m := healthyDiskMeasurement()
		m.Filesystems[0].AvailableBytes = diskInt(available)
		return m, nil
	}
	var calls int
	client := &lifecycleBatchClient{acquireJobs: func(_ context.Context, ids []int64) ([]int64, error) {
		calls++
		if len(ids) != 1 || ids[0] != 99 {
			t.Fatalf("unexpected IDs %v", ids)
		}
		return ids, nil
	}}
	jit := &unavailableJitClient{}
	s := &Scaler{disk: d, runners: newRunnerState(), messageClient: client, scalesetClient: jit,
		logger: slog.New(slog.DiscardHandler), maintenanceLockPath: t.TempDir() + "/lock",
		config: Config{MaxRunners: 1, StatusFile: t.TempDir() + "/status.json"}}
	message := &scaleset.RunnerScaleSetMessage{
		Statistics:           &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{RunnerRequestID: 99}}},
		JobAssignedMessages:  []*scaleset.JobAssigned{{}},
	}
	ctx := context.Background()
	if err := s.Scale(ctx, message); err != nil {
		t.Fatal(err)
	}
	if calls != 0 || jit.calls != 0 {
		t.Fatal("acquired an offer or created a runner under pressure")
	}
	available = 140 << 30
	if err := s.Scale(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if calls != 0 || jit.calls != 0 {
		t.Fatal("admission resumed before reaching the recovery threshold")
	}
	available = 150 << 30
	session := diskAwareSession{Client: client, disk: d}
	if _, err := session.GetMessage(ctx, 0, 1); err != nil || !slices.Equal(client.pollCapacity, []int{1}) {
		t.Fatalf("configured capacity did not resume: %v, %v", client.pollCapacity, err)
	}
	if err := s.Scale(ctx, nil); err == nil || !strings.Contains(err.Error(), "JIT temporarily unavailable") || calls != 0 || jit.calls != 1 {
		t.Fatalf("recovery failed to reconcile existing demand: acquired=%d JIT=%d error=%v", calls, jit.calls, err)
	}
	s.runners.addIdle("warm", "id")
	if err := s.Scale(ctx, message); err != nil || calls != 1 {
		t.Fatalf("re-offered job was not acquired after recovery: acquired=%d error=%v", calls, err)
	}
}
