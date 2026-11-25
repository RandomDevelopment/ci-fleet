package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const (
	diskMeasurementDirectory       = "/run/ci-fleet-disk-admission"
	diskStopPercent                = 90
	diskResumePercent              = 85
	diskStopBytes            int64 = 10 * 1024 * 1024 * 1024
	diskResumeBytes          int64 = 15 * 1024 * 1024 * 1024
	diskMaxAge               int64 = 75
)

var invalidDiskJSON = errors.New("invalid disk measurement JSON")

type diskFilesystem struct {
	Roles          []string `json:"roles"`
	ObservedAt     *int64   `json:"observed_at"`
	CapacityBytes  *int64   `json:"capacity_bytes"`
	AvailableBytes *int64   `json:"available_bytes"`
	TotalInodes    *int64   `json:"total_inodes"`
	FreeInodes     *int64   `json:"free_inodes"`
	State          string   `json:"state"`
	Reason         string   `json:"reason"`
}

type diskMeasurement struct {
	SchemaVersion int              `json:"schema_version"`
	BootID        string           `json:"boot_id"`
	Controller    string           `json:"controller"`
	Filesystems   []diskFilesystem `json:"filesystems"`
}

type diskRecovery struct {
	StopUsedPercent      int   `json:"stop_used_percent"`
	ResumeUsedPercent    int   `json:"resume_used_percent"`
	StopAvailableBytes   int64 `json:"stop_available_bytes"`
	ResumeAvailableBytes int64 `json:"resume_available_bytes"`
	StopInodePercent     int   `json:"stop_inode_percent"`
	ResumeInodePercent   int   `json:"resume_inode_percent"`
	MaxAgeSeconds        int64 `json:"max_age_seconds"`
}

type diskAdmissionStatus struct {
	SchemaVersion int              `json:"schema_version"`
	State         string           `json:"state"`
	Reason        string           `json:"reason"`
	ObservedAt    *int64           `json:"observed_at"`
	BlockedSince  *int64           `json:"blocked_since"`
	RecoveredAt   *int64           `json:"recovered_at"`
	Filesystems   []diskFilesystem `json:"filesystems"`
	Recovery      diskRecovery     `json:"recovery"`
}

// The latch is deliberately process-local. A restart requires the recovery
// thresholds, never a persisted "ready" decision or reconstructed transition time.
type diskAdmission struct {
	mu         sync.Mutex
	read       func() (diskMeasurement, error)
	now        func() time.Time
	bootID     string
	controller string
	ready      bool
	lastNow    int64
	status     diskAdmissionStatus
}

func newDiskAdmission(controller string) *diskAdmission {
	boot, _ := os.ReadFile("/proc/sys/kernel/random/boot_id")
	return &diskAdmission{controller: controller, bootID: strings.TrimSpace(string(boot)), now: time.Now,
		read: func() (diskMeasurement, error) { return readDiskMeasurement(diskMeasurementDirectory) }}
}

func unknownFilesystems(reason string) []diskFilesystem {
	result := make([]diskFilesystem, 0, 3)
	for _, role := range []string{"workspace", "docker", "spool"} {
		result = append(result, diskFilesystem{Roles: []string{role}, State: "unknown", Reason: reason})
	}
	return result
}

func diskPolicy() diskRecovery {
	return diskRecovery{diskStopPercent, diskResumePercent, diskStopBytes, diskResumeBytes, diskStopPercent, diskResumePercent, diskMaxAge}
}

func (d *diskAdmission) check() bool {
	if d == nil {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	value, err := d.read()
	now := d.now().Unix()
	reason := ""
	if err != nil {
		reason = "measurement_unavailable"
		if errors.Is(err, invalidDiskJSON) {
			reason = "measurement_invalid"
		}
	} else if d.bootID == "" || value.BootID != d.bootID || value.Controller != d.controller || !validDiskMeasurement(value) || now < d.lastNow {
		reason = "measurement_invalid"
	}
	d.lastNow = now
	filesystems := append([]diskFilesystem(nil), value.Filesystems...)
	if reason != "" {
		filesystems = unknownFilesystems(reason)
	}
	var observed *int64
	blocked, unknown, recovering := "", "", false
	for i := range filesystems {
		fs := &filesystems[i]
		if fs.State == "unknown" {
			unknown = fs.Reason
			continue
		}
		if *fs.ObservedAt > now {
			*fs = diskFilesystem{Roles: fs.Roles, State: "unknown", Reason: "measurement_invalid"}
			unknown = fs.Reason
			continue
		}
		if now-*fs.ObservedAt > diskMaxAge {
			fs.State, fs.Reason = "unknown", "measurement_stale"
			unknown = fs.Reason
			continue
		}
		if observed == nil || *fs.ObservedAt < *observed {
			stamp := *fs.ObservedAt
			observed = &stamp
		}
		fs.State, fs.Reason = "ready", "ready"
		switch {
		case percentAtLeast(*fs.CapacityBytes-*fs.AvailableBytes, *fs.CapacityBytes, diskStopPercent):
			fs.State, fs.Reason = "blocked", "usage_percent"
		case *fs.AvailableBytes < diskStopBytes:
			fs.State, fs.Reason = "blocked", "available_bytes"
		case percentAtLeast(*fs.TotalInodes-*fs.FreeInodes, *fs.TotalInodes, diskStopPercent):
			fs.State, fs.Reason = "blocked", "inodes"
		case !d.ready && (percentAbove(*fs.CapacityBytes-*fs.AvailableBytes, *fs.CapacityBytes, diskResumePercent) ||
			*fs.AvailableBytes < diskResumeBytes || percentAbove(*fs.TotalInodes-*fs.FreeInodes, *fs.TotalInodes, diskResumePercent)):
			fs.State, fs.Reason = "blocked", "recovering"
			recovering = true
		}
		if fs.State == "blocked" && fs.Reason != "recovering" && blocked == "" {
			blocked = fs.Reason
		}
	}
	state, reason := "ready", "ready"
	// A proven blocker wins over uncertainty; every per-filesystem failure is
	// still visible. Unknown is never permission to admit.
	switch {
	case blocked != "":
		state, reason = "low_disk", blocked
	case unknown != "":
		state, reason = "unknown", unknown
	case recovering:
		state, reason = "blocked", "recovering"
	}
	if unknown != "" {
		observed = nil
	}
	previous := d.ready
	d.ready = state == "ready"
	blockedSince, recoveredAt := d.status.BlockedSince, d.status.RecoveredAt
	if previous && !d.ready {
		stamp := now
		blockedSince = &stamp
	}
	if !previous && d.ready && d.status.State != "" {
		stamp := now
		recoveredAt = &stamp
	}
	if d.ready {
		blockedSince = nil
	}
	d.status = diskAdmissionStatus{1, state, reason, observed, blockedSince, recoveredAt, filesystems, diskPolicy()}
	return d.ready
}

// Integer quotient/remainder comparisons avoid float rounding and int64 overflow.
func percentAtLeast(used, total int64, percent int64) bool {
	threshold := total/100*percent + (total%100*percent+99)/100
	return used >= threshold
}
func percentAbove(used, total int64, percent int64) bool {
	threshold := total/100*percent + total%100*percent/100
	return used > threshold
}

func validDiskMeasurement(value diskMeasurement) bool {
	if value.SchemaVersion != 1 || len(value.Filesystems) < 1 || len(value.Filesystems) > 3 {
		return false
	}
	seen := map[string]bool{}
	for _, fs := range value.Filesystems {
		if len(fs.Roles) == 0 {
			return false
		}
		for _, role := range fs.Roles {
			if seen[role] || (role != "workspace" && role != "docker" && role != "spool") {
				return false
			}
			seen[role] = true
		}
		if fs.State == "unknown" {
			if fs.Reason != "measurement_unavailable" || fs.ObservedAt != nil || fs.CapacityBytes != nil || fs.AvailableBytes != nil || fs.TotalInodes != nil || fs.FreeInodes != nil {
				return false
			}
			continue
		}
		if fs.State != "measured" || fs.Reason != "measured" || fs.ObservedAt == nil || *fs.ObservedAt <= 0 || fs.CapacityBytes == nil || fs.AvailableBytes == nil || fs.TotalInodes == nil || fs.FreeInodes == nil {
			return false
		}
		if *fs.CapacityBytes <= 0 || *fs.AvailableBytes < 0 || *fs.AvailableBytes > *fs.CapacityBytes || *fs.TotalInodes <= 0 || *fs.FreeInodes < 0 || *fs.FreeInodes > *fs.TotalInodes {
			return false
		}
	}
	return len(seen) == 3
}

func (d *diskAdmission) snapshot() diskAdmissionStatus {
	if d == nil {
		return diskAdmissionStatus{SchemaVersion: 1, State: "unknown", Reason: "starting", Filesystems: unknownFilesystems("starting"), Recovery: diskPolicy()}
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.status.State == "" {
		return (*diskAdmission)(nil).snapshot()
	}
	result := d.status
	result.Filesystems = append([]diskFilesystem(nil), d.status.Filesystems...)
	return result
}

func readDiskMeasurement(directory string) (diskMeasurement, error) {
	var result diskMeasurement
	// Open every ancestor without following symlinks. Only the dedicated export
	// is mounted; no host root, Docker storage or private installation tree.
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return result, err
	}
	defer func() { unix.Close(fd) }()
	for _, part := range strings.Split(strings.TrimPrefix(directory, "/"), "/") {
		if part == "" || part == "." || part == ".." {
			return result, fmt.Errorf("invalid export path")
		}
		next, err := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return result, err
		}
		unix.Close(fd)
		fd = next
		var st unix.Stat_t
		if err := unix.Fstat(fd, &st); err != nil || st.Uid != 0 || st.Mode&0022 != 0 {
			return result, fmt.Errorf("untrusted export ancestry")
		}
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil || st.Mode&0777 != 0700 || !diskNoACL(fd) {
		return result, fmt.Errorf("untrusted export directory")
	}
	fileFD, err := unix.Openat(fd, "measurement.json", unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return result, err
	}
	file := os.NewFile(uintptr(fileFD), "measurement.json")
	defer file.Close()
	if err := unix.Fstat(fileFD, &st); err != nil || st.Uid != 0 || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Mode&0777 != 0600 || st.Nlink != 1 || st.Size > 16384 || !diskNoACL(fileFD) {
		return result, fmt.Errorf("untrusted export file")
	}
	data, err := io.ReadAll(io.LimitReader(file, 16385))
	if err != nil || len(data) > 16384 {
		return result, fmt.Errorf("invalid export size")
	}
	return decodeDiskMeasurement(data)
}

func decodeDiskMeasurement(data []byte) (diskMeasurement, error) {
	var result diskMeasurement
	if !uniqueDiskJSON(json.NewDecoder(bytes.NewReader(data)), 0) {
		return result, invalidDiskJSON
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&result); err != nil {
		return result, invalidDiskJSON
	}
	if decoder.Decode(new(any)) != io.EOF {
		return result, invalidDiskJSON
	}
	return result, nil
}

func uniqueDiskJSON(decoder *json.Decoder, depth int) bool {
	if depth > 16 {
		return false
	}
	token, err := decoder.Token()
	if err != nil {
		return false
	}
	switch token {
	case json.Delim('{'):
		seen := map[string]bool{}
		for decoder.More() {
			key, err := decoder.Token()
			if err != nil {
				return false
			}
			name, ok := key.(string)
			name = strings.ToLower(name) // encoding/json matches struct keys case-insensitively.
			if !ok || seen[name] {
				return false
			}
			seen[name] = true
			if !uniqueDiskJSON(decoder, depth+1) {
				return false
			}
		}
		end, err := decoder.Token()
		return err == nil && end == json.Delim('}')
	case json.Delim('['):
		for decoder.More() {
			if !uniqueDiskJSON(decoder, depth+1) {
				return false
			}
		}
		end, err := decoder.Token()
		return err == nil && end == json.Delim(']')
	default:
		_, delimiter := token.(json.Delim)
		return !delimiter
	}
}

func diskNoACL(fd int) bool {
	for _, name := range []string{"system.posix_acl_access", "system.posix_acl_default"} {
		_, err := unix.Fgetxattr(fd, name, nil)
		if err != unix.ENODATA && err != unix.ENOTSUP {
			return false
		}
	}
	return true
}
