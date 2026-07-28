package main

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
)

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
