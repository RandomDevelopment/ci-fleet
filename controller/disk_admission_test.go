package main

import (
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func diskInt(value int64) *int64 { return &value }
func healthyDiskMeasurement() diskMeasurement {
	return diskMeasurement{1, "test-boot", "example", []diskFilesystem{{
		Roles: []string{"workspace", "docker", "spool"}, ObservedAt: diskInt(1000),
		CapacityBytes: diskInt(1000 << 30), AvailableBytes: diskInt(500 << 30),
		TotalInodes: diskInt(1000000), FreeInodes: diskInt(500000), State: "measured", Reason: "measured",
	}}}
}
func admittedDisk() *diskAdmission {
	return &diskAdmission{bootID: "test-boot", controller: "example", now: func() time.Time { return time.Unix(1000, 0) },
		read: func() (diskMeasurement, error) { return healthyDiskMeasurement(), nil }}
}

func TestDiskIndependentTripsAndRecovery(t *testing.T) {
	for _, tc := range []struct {
		name, reason string
		change       func(*diskFilesystem)
	}{
		{"percentage", "usage_percent", func(f *diskFilesystem) { f.AvailableBytes = diskInt(100 << 30) }},
		{"bytes", "available_bytes", func(f *diskFilesystem) { f.CapacityBytes = diskInt(20 << 30); f.AvailableBytes = diskInt(9 << 30) }},
		{"inodes", "inodes", func(f *diskFilesystem) { f.FreeInodes = diskInt(100000) }},
		{"zero available", "usage_percent", func(f *diskFilesystem) { f.AvailableBytes = diskInt(0) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := admittedDisk()
			m := healthyDiskMeasurement()
			d.read = func() (diskMeasurement, error) {
				data, _ := json.Marshal(m)
				var clone diskMeasurement
				_ = json.Unmarshal(data, &clone)
				return clone, nil
			}
			if !d.check() {
				t.Fatal("healthy initial sample rejected")
			}
			tc.change(&m.Filesystems[0])
			if d.check() || d.snapshot().Reason != tc.reason {
				t.Fatalf("trip: %+v", d.snapshot())
			}
			m = healthyDiskMeasurement()
			m.Filesystems[0].AvailableBytes = diskInt(140 << 30)
			if d.check() || d.snapshot().Reason != "recovering" {
				t.Fatal("resumed above85%")
			}
			m.Filesystems[0].AvailableBytes = diskInt(150 << 30)
			if !d.check() || d.snapshot().RecoveredAt == nil {
				t.Fatal("failed recovery at85%")
			}
			m.Filesystems[0].CapacityBytes = diskInt(20 << 30)
			m.Filesystems[0].AvailableBytes = diskInt(diskStopBytes)
			if !d.check() {
				t.Fatal("stop floor is strictly below10GiB")
			}
			m.Filesystems[0].AvailableBytes = diskInt(diskStopBytes - 1)
			if d.check() {
				t.Fatal("bytes floor ignored")
			}
			m.Filesystems[0].AvailableBytes = diskInt(diskResumeBytes - 1)
			if d.check() {
				t.Fatal("resume floor ignored")
			}
			m.Filesystems[0].AvailableBytes = diskInt(diskResumeBytes)
			if !d.check() {
				t.Fatal("resume floor boundary failed")
			}
		})
	}
}

func TestDiskUnknownEvidenceRestartAndClock(t *testing.T) {
	for _, tc := range []struct {
		name, reason string
		change       func(*diskMeasurement)
	}{
		{"stale", "measurement_stale", func(m *diskMeasurement) { m.Filesystems[0].ObservedAt = diskInt(924) }},
		{"future", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].ObservedAt = diskInt(1001) }},
		{"boot", "measurement_invalid", func(m *diskMeasurement) { m.BootID = "other" }},
		{"identity", "measurement_invalid", func(m *diskMeasurement) { m.Controller = "other" }},
		{"zero total", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].CapacityBytes = diskInt(0) }},
		{"invalid available", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].AvailableBytes = diskInt(1001 << 30) }},
		{"no inode support", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].TotalInodes = diskInt(0) }},
		{"missing role", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].Roles = []string{"docker"} }},
		{"duplicate role", "measurement_invalid", func(m *diskMeasurement) { m.Filesystems[0].Roles = append(m.Filesystems[0].Roles, "docker") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := admittedDisk()
			m := healthyDiskMeasurement()
			tc.change(&m)
			d.read = func() (diskMeasurement, error) { return m, nil }
			if d.check() || d.snapshot().State != "unknown" || d.snapshot().Reason != tc.reason {
				t.Fatalf("bad unknown: %+v", d.snapshot())
			}
			if d.snapshot().ObservedAt != nil || d.snapshot().BlockedSince != nil {
				t.Fatal("invented timestamp")
			}
		})
	}
	d := admittedDisk()
	if !d.check() {
		t.Fatal("initial")
	}
	d.now = func() time.Time { return time.Unix(999, 0) }
	if d.check() {
		t.Fatal("backward clock admitted")
	}
	d = admittedDisk()
	d.read = func() (diskMeasurement, error) { return diskMeasurement{}, errors.New("missing") }
	if d.check() || d.snapshot().Reason != "measurement_unavailable" {
		t.Fatal("missing admitted")
	}
	d = admittedDisk()
	m := healthyDiskMeasurement()
	m.Filesystems[0].AvailableBytes = diskInt(140 << 30)
	d.read = func() (diskMeasurement, error) { return m, nil }
	if d.check() || d.snapshot().Reason != "recovering" || d.snapshot().BlockedSince != nil {
		t.Fatal("restart bypassed recovery or invented prior transition")
	}
}

func TestDiskMultipleFilesystemsPreserveKnownBlockerAndUnknown(t *testing.T) {
	d := admittedDisk()
	m := healthyDiskMeasurement()
	m.Filesystems[0].Roles = []string{"workspace", "spool"}
	m.Filesystems[0].AvailableBytes = diskInt(0)
	m.Filesystems = append(m.Filesystems, unknownFilesystems("measurement_unavailable")[1])
	d.read = func() (diskMeasurement, error) { return m, nil }
	if d.check() || d.snapshot().State != "low_disk" || d.snapshot().ObservedAt != nil {
		t.Fatalf("known blocker hidden: %+v", d.snapshot())
	}
	if d.snapshot().Filesystems[1].State != "unknown" || d.snapshot().Filesystems[1].AvailableBytes != nil {
		t.Fatal("unknown converted to zero")
	}
}

func TestDiskStatusPublicationDoesNotRefreshMeasurement(t *testing.T) {
	d := admittedDisk()
	d.check()
	before := *d.snapshot().ObservedAt
	s := &Scaler{disk: d, runners: newRunnerState(), logger: slog.New(slog.DiscardHandler), config: Config{StatusFile: t.TempDir() + "/status.json"}}
	s.writeStatus()
	s.writeStatus()
	if *d.snapshot().ObservedAt != before {
		t.Fatal("publication refreshed probe")
	}
}

func TestDiskProtectedRead(t *testing.T) {
	// Use the repository's protected parent rather than /tmp's intentionally
	// world-writable ancestry; this performs no privileged system mutation.
	dir, err := os.MkdirTemp(".", ".disk-read-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	dir, err = filepath.Abs(dir)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(healthyDiskMeasurement())
	path := filepath.Join(dir, "measurement.json")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if os.Getuid() == 0 {
		if _, err := readDiskMeasurement(dir); err != nil {
			t.Fatal(err)
		}
	} else {
		if _, err := readDiskMeasurement(dir); err == nil {
			t.Fatal("non-root writer admitted")
		}
		return
	}
	for _, mode := range []os.FileMode{0644, 0666} {
		_ = os.Chmod(path, mode)
		if _, err := readDiskMeasurement(dir); err == nil {
			t.Fatal("unsafe mode")
		}
	}
	_ = os.Chmod(path, 0600)
	_ = os.Link(path, path+".link")
	if _, err := readDiskMeasurement(dir); err == nil {
		t.Fatal("hardlink")
	}
	_ = os.Remove(path + ".link")
	_ = os.Remove(path)
	_ = os.Symlink("missing", path)
	if _, err := readDiskMeasurement(dir); err == nil {
		t.Fatal("symlink")
	}
	_ = os.Remove(path)
	_ = syscall.Mkfifo(path, 0600)
	if _, err := readDiskMeasurement(dir); err == nil {
		t.Fatal("fifo")
	}
}
