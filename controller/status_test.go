package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRunnerStateMakesExitAndCompletionIdempotent(t *testing.T) {
	state := newRunnerState()
	state.addIdle("runner", "container-1")
	if state.markExited("runner", "container-2") { t.Fatal("removed replacement runner for stale exit") }
	if current, _ := state.counts(); current != 1 { t.Fatalf("current=%d, want 1", current) }
	if !state.markExited("runner", "container-1") { t.Fatal("matching exited runner was not removed") }
	if current, _ := state.counts(); current != 0 { t.Fatalf("current=%d, want 0", current) }
	if !state.markBusy("runner") { t.Fatal("late job start rejected exited runner") }
	if current, busy := state.counts(); current != 0 || busy != 0 { t.Fatalf("late start restored exited runner: current=%d busy=%d", current, busy) }
	if id, cleanup, ok := state.markDone("runner"); !ok || cleanup || id != "container-1" {
		t.Fatalf("late completion = id %q cleanup %t ok %t", id, cleanup, ok)
	}

	state.addIdle("normal", "container-2")
	if !state.contains("normal", "container-2") { t.Fatal("tracked runner was not found") }
	if _, cleanup, ok := state.markDone("normal"); !ok || !cleanup { t.Fatal("normal completion skipped cleanup") }
	if state.contains("normal", "container-2") { t.Fatal("completed runner remained tracked") }
	if state.markExited("normal", "container-2") { t.Fatal("exit won after normal completion") }
}

func TestWriteStatusReportsRunnerCountsWithoutControllingExecution(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	scaler := &Scaler{
		runners: newRunnerState(),
		logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		config: Config{FleetInstance: "example-ci-01", MaxRunners: 6, StatusFile: path},
	}
	scaler.runners.addIdle("idle", "1")
	scaler.runners.addIdle("busy", "2")
	if !scaler.runners.markBusy("busy") {
		t.Fatal("runner did not become busy")
	}
	scaler.writeStatus()
	var got controllerStatus
	body, err := os.ReadFile(path)
	if err != nil { t.Fatal(err) }
	if err := json.Unmarshal(body, &got); err != nil { t.Fatal(err) }
	if got.Controller != "example-ci-01" || got.Current != 2 || got.Busy != 1 || got.Maximum != 6 {
		t.Fatalf("unexpected status: %+v", got)
	}

	// Reporting is advisory: an unwritable destination has no return path into scaling.
	scaler.config.StatusFile = filepath.Join(path, "impossible")
	scaler.writeStatus()
	if current, busy := scaler.runners.counts(); current != 2 || busy != 1 {
		t.Fatalf("status failure changed runner state: current=%d busy=%d", current, busy)
	}
}

func TestWriteStatusPreservesPreviousSnapshotOnEncodingFailure(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	previous := []byte(`{"previous":true}`)
	if err := os.WriteFile(path, previous, 0o644); err != nil { t.Fatal(err) }
	scaler := &Scaler{
		runners: newRunnerState(),
		logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		config: Config{FleetInstance: "example-ci-01", MaxRunners: 1, StatusFile: path},
	}
	original := encodeControllerStatus
	encodeControllerStatus = func(file *os.File, _ controllerStatus) error {
		_, _ = file.WriteString("truncated")
		return errors.New("filesystem full")
	}
	defer func() { encodeControllerStatus = original }()
	scaler.writeStatus()
	got, err := os.ReadFile(path)
	if err != nil { t.Fatal(err) }
	if string(got) != string(previous) { t.Fatalf("status replaced after encoding failure: %q", got) }
}

func TestStatusWritesUsePublicationLock(t *testing.T) {
	scaler := &Scaler{
		runners: newRunnerState(),
		logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		config: Config{FleetInstance: "example-ci-01", MaxRunners: 1, StatusFile: filepath.Join(t.TempDir(), "status.json")},
	}
	statusWriteMu.Lock()
	done := make(chan struct{})
	go func() { scaler.writeStatus(); close(done) }()
	select {
	case <-done:
		statusWriteMu.Unlock()
		t.Fatal("status write bypassed publication lock")
	case <-time.After(20 * time.Millisecond):
		statusWriteMu.Unlock()
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("status write did not resume after publication lock")
	}
}

func TestStatusPublisherRefreshesIdleSnapshot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	scaler := &Scaler{
		runners: newRunnerState(),
		logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
		config: Config{FleetInstance: "example-ci-01", MaxRunners: 1, StatusFile: path},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		scaler.publishStatus(ctx, time.Millisecond)
		close(done)
	}()
	deadline := time.After(time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			cancel()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("idle status publisher did not stop")
			}
			return
		}
		select {
		case <-deadline:
			t.Fatal("idle status publisher did not refresh snapshot")
		case <-time.After(time.Millisecond):
		}
	}
}
