package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var statusWriteMu sync.Mutex
var encodeControllerStatus = func(file *os.File, value controllerStatus) error {
	return json.NewEncoder(file).Encode(value)
}

type controllerStatus struct {
	Controller      string                `json:"controller"`
	SoftwareVersion string                `json:"software_version"`
	Current         int                   `json:"current"`
	Busy            int                   `json:"busy"`
	Maximum         int                   `json:"maximum"`
	GeneratedAt     int64                 `json:"generated_at"`
	DiskAdmission   diskAdmissionStatus   `json:"disk_admission"`
	Enrollment      *controllerEnrollment `json:"enrollment,omitempty"`
}

type controllerEnrollment struct {
	ConfigCommit    string   `json:"config_commit,omitempty"`
	ScaleSetName    string   `json:"scale_set_name"`
	RoutingLabels   []string `json:"routing_labels"`
	MatchedAt       int64    `json:"matched_at"`
	SessionOpenedAt int64    `json:"session_opened_at"`
	PolledAt        int64    `json:"polled_at,omitempty"`
}

func (s *Scaler) writeStatus() {
	statusWriteMu.Lock()
	defer statusWriteMu.Unlock()
	current, busy := s.runners.counts()
	softwareVersion := commitSHA
	if softwareVersion == "unknown" {
		softwareVersion = version
	}
	value := controllerStatus{
		Controller: s.config.FleetInstance, SoftwareVersion: softwareVersion,
		Current: current, Busy: busy, Maximum: s.config.MaxRunners,
		DiskAdmission: s.disk.snapshot(),
	}
	if s.enrollment != nil {
		enrollment := *s.enrollment
		enrollment.PolledAt = s.listenerPollAt.Load()
		value.Enrollment = &enrollment
	}
	value.GeneratedAt = time.Now().Unix()
	directory := filepath.Dir(s.config.StatusFile)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		s.logger.Warn("write controller status", "error", err)
		return
	}
	temporary, err := os.CreateTemp(directory, ".status-*")
	if err != nil {
		s.logger.Warn("write controller status", "error", err)
		return
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err = temporary.Chmod(0o644); err == nil {
		err = encodeControllerStatus(temporary, value)
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, s.config.StatusFile)
	}
	if err != nil {
		s.logger.Warn("write controller status", "error", err)
	}
}

func (s *Scaler) publishStatus(ctx context.Context, ticks <-chan time.Time) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticks:
			s.disk.check()
			s.writeStatus()
		}
	}
}
