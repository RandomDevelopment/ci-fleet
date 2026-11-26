package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"math"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

const (
	jobSlots     = 5000
	jobSlotBytes = 4096
	jobStateSize = 8192
	jobMaxAge    = 7 * 24 * 60 * 60
)

type jobHistoryState struct {
	Version    int               `json:"version"`
	Boot       string            `json:"boot"`
	Controller string            `json:"controller"`
	ScaleSet   string            `json:"scale_set"`
	Generation string            `json:"generation"`
	Epoch      string            `json:"activity_epoch"`
	Open       bool              `json:"session_open"`
	Dirty      bool              `json:"dirty"`
	PollAt     int64             `json:"poll_at"`
	Demand     *jobDemand        `json:"demand,omitempty"`
	Gaps       map[string]uint64 `json:"gaps"`
	Slots      int               `json:"slots"`
	Records    int               `json:"record_count"`
	RecordFile string            `json:"record_file"`
}

type jobHistory struct {
	dir        *os.File
	file       *os.File
	state      jobHistoryState
	records    []jobRecord
	index      map[string]int
	pending    map[int]struct{}
	nextExpiry int64
}

var errUnsafeJobHistory = errors.New("unsafe job history")

func jobHistoryAbsent(err error) bool { return errors.Is(err, unix.ENOENT) }

func jobNoACL(fd int) error {
	for _, attribute := range []string{"system.posix_acl_access", "system.posix_acl_default"} {
		if _, err := unix.Fgetxattr(fd, attribute, nil); err != unix.ENODATA && err != unix.ENOTSUP {
			return errUnsafeJobHistory
		}
	}
	return nil
}

func openJobDirectory(path string) (*os.File, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, errUnsafeJobHistory
	}
	const flags = unix.O_RDONLY | unix.O_DIRECTORY | unix.O_CLOEXEC | unix.O_NOFOLLOW
	fd, err := unix.Open("/", flags, 0)
	if err != nil {
		return nil, err
	}
	current := ""
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	for i, name := range parts {
		child, err := unix.Openat(fd, name, flags, 0)
		unix.Close(fd)
		if err != nil {
			return nil, err
		}
		fd = child
		current += "/" + name
		var stat unix.Stat_t
		if err := unix.Fstat(fd, &stat); err != nil {
			unix.Close(fd)
			return nil, err
		}
		mode := stat.Mode & 07777
		if stat.Uid != 0 || (mode&0022 != 0 && !(current == "/tmp" && mode&01000 != 0)) ||
			(i == len(parts)-2 && (mode != 0750 || jobNoACL(fd) != nil)) ||
			(i == len(parts)-1 && (mode != 0700 || jobNoACL(fd) != nil)) {
			unix.Close(fd)
			return nil, errUnsafeJobHistory
		}
	}
	return os.NewFile(uintptr(fd), path), nil
}

func openJobFile(dir *os.File, name string, flags int, limit int64) (*os.File, error) {
	fd, err := unix.Openat(int(dir.Fd()), name, flags|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0600)
	if err != nil {
		return nil, err
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Uid != 0 || stat.Nlink != 1 ||
		stat.Mode != unix.S_IFREG|0600 || stat.Size > limit || jobNoACL(fd) != nil {
		unix.Close(fd)
		return nil, errUnsafeJobHistory
	}
	return os.NewFile(uintptr(fd), name), nil
}

func (h *jobHistory) validateDirectory() error {
	// Use a fresh descriptor so reading the directory never changes the lock's offset.
	fd, err := unix.Openat(int(h.dir.Fd()), ".", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	scan := os.NewFile(uintptr(fd), "private")
	defer scan.Close()
	names, err := scan.Readdirnames(7)
	if err != nil && err != io.EOF {
		return err
	}
	for _, name := range names {
		var limit int64
		switch name {
		case "records":
			limit = jobSlots * jobSlotBytes
		case "state.json", "state.json.tmp":
			limit = jobStateSize
		case "attempt.json", "attempt.json.tmp":
			// The publisher validates these under the export lock and can replace them concurrently.
			continue
		default:
			return errUnsafeJobHistory
		}
		file, err := openJobFile(h.dir, name, unix.O_RDONLY, limit)
		if err != nil {
			return err
		}
		file.Close()
	}
	return nil
}

func openJobHistory(path, controller, scaleSet string) (_ *jobHistory, err error) {
	if !jobScope.MatchString(controller) || !jobScope.MatchString(scaleSet) {
		return nil, errUnsafeJobHistory
	}
	dir, err := openJobDirectory(path)
	if err != nil {
		return nil, err
	}
	h := &jobHistory{dir: dir, index: make(map[string]int), pending: make(map[int]struct{})}
	defer func() {
		if err != nil {
			h.close()
		}
	}()
	if err = unix.Flock(int(dir.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return nil, err
	}
	defer unix.Flock(int(dir.Fd()), unix.LOCK_UN)
	if err = h.validateDirectory(); err != nil {
		return nil, err
	}
	h.file, err = openJobFile(dir, "records", unix.O_RDWR, jobSlots*jobSlotBytes)
	if errors.Is(err, unix.ENOENT) {
		h.file, err = openJobFile(dir, "records", unix.O_RDWR|unix.O_CREAT|unix.O_EXCL, jobSlots*jobSlotBytes)
	}
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(int(h.file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return nil, err
	}
	read, readErr := openJobFile(dir, "state.json", unix.O_RDONLY, jobStateSize)
	if readErr == nil {
		payload, readErr := io.ReadAll(io.LimitReader(read, jobStateSize+1))
		read.Close()
		if readErr != nil {
			return nil, readErr
		}
		if err := decodeJobJSON(payload, &h.state); err != nil || h.state.Version != 1 ||
			h.state.Gaps == nil || h.state.Slots < 0 || h.state.Slots > jobSlots ||
			h.state.Records < 0 || h.state.Records > h.state.Slots {
			return nil, errUnsafeJobHistory
		}
	} else if !errors.Is(readErr, unix.ENOENT) {
		return nil, readErr
	}
	if h.state.Version == 1 && h.state.Controller != controller {
		return nil, errUnsafeJobHistory
	}
	reset := h.state.Version == 0 || h.state.ScaleSet != scaleSet
	if reset {
		h.state = jobHistoryState{Version: 1, Controller: controller, ScaleSet: scaleSet, Gaps: map[string]uint64{"initial_unknown": 1}}
	}
	if h.state.Open || h.state.Dirty {
		h.gap("unclean_session", 1)
	}
	info, err := h.file.Stat()
	if err != nil {
		return nil, err
	}
	digest, err := h.recordFile()
	if err != nil {
		return nil, err
	}
	if info.Size() != int64(h.state.Slots)*jobSlotBytes || (!reset && digest != h.state.RecordFile) {
		h.gap("storage_corruption", 1)
	}
	bootBytes, bootErr := os.ReadFile("/proc/sys/kernel/random/boot_id")
	boot := strings.TrimSpace(string(bootBytes))
	if _, parseErr := uuid.Parse(boot); bootErr != nil || parseErr != nil {
		return nil, errUnsafeJobHistory
	}
	h.state.Boot = boot
	h.state.Open, h.state.Dirty, h.state.PollAt, h.state.Demand = true, true, 0, nil
	h.state.Epoch, h.state.Generation = uuid.NewString(), uuid.NewString()
	if reset {
		if err = h.file.Truncate(0); err != nil {
			return nil, err
		}
		if err = h.file.Sync(); err != nil {
			return nil, err
		}
	}
	if err = h.writeState(); err != nil {
		return nil, err
	}
	if !reset {
		err = h.load(info.Size())
		if len(h.index) != h.state.Records {
			h.gap("storage_corruption", 1)
		}
	}
	if err != nil {
		return nil, err
	}
	if err = h.flush(); err != nil {
		return nil, err
	}
	return h, nil
}

func (h *jobHistory) close() {
	if h.file != nil {
		h.file.Close()
	}
	h.dir.Close()
}

func decodeJobJSON(payload []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if decoder.Decode(new(any)) != io.EOF {
		return errUnsafeJobHistory
	}
	return nil
}

func (h *jobHistory) load(size int64) error {
	if remainder := size % jobSlotBytes; remainder != 0 {
		size -= remainder
		if err := h.file.Truncate(size); err != nil {
			return err
		}
	}
	frame := make([]byte, jobSlotBytes)
	for offset := int64(0); offset < size; offset += jobSlotBytes {
		if _, err := h.file.ReadAt(frame, offset); err != nil {
			return err
		}
		slot := len(h.records)
		var record jobRecord
		length := binary.LittleEndian.Uint32(frame)
		if length != 0 {
			key := ""
			if length <= jobSlotBytes-8 && crc32.ChecksumIEEE(frame[8:8+length]) == binary.LittleEndian.Uint32(frame[4:]) &&
				decodeJobJSON(frame[8:8+length], &record) == nil && record.SeenAt > 0 {
				key = jobKey(record)
			}
			if _, duplicate := h.index[key]; key == "" || duplicate {
				h.gap("storage_corruption", 1)
				record = jobRecord{}
				h.pending[slot] = struct{}{}
			} else {
				h.index[key] = slot
			}
		} else if !bytes.Equal(frame, make([]byte, jobSlotBytes)) {
			h.gap("storage_corruption", 1)
			h.pending[slot] = struct{}{}
		}
		h.records = append(h.records, record)
	}
	return nil
}

func (h *jobHistory) recordFile() (string, error) {
	var stat unix.Stat_t
	if err := unix.Fstat(int(h.file.Fd()), &stat); err != nil {
		return "", err
	}
	if stat.Nlink != 1 {
		return "", errUnsafeJobHistory
	}
	identity := fmt.Sprintf("%d:%d:%d:%d", stat.Dev, stat.Ino, stat.Size, stat.Ctim.Nano())
	return fmt.Sprintf("%x", sha256.Sum256([]byte(identity))), nil
}

func (h *jobHistory) writeState() error {
	payload, err := json.Marshal(h.state)
	if err != nil || len(payload) > jobStateSize {
		return errUnsafeJobHistory
	}
	dir := int(h.dir.Fd())
	if err := unix.Unlinkat(dir, "state.json.tmp", 0); err != nil && !errors.Is(err, unix.ENOENT) {
		return err
	}
	tmp, err := openJobFile(h.dir, "state.json.tmp", unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL, jobStateSize)
	if err != nil {
		return err
	}
	defer tmp.Close()
	if _, err := tmp.Write(payload); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := unix.Renameat(dir, "state.json.tmp", dir, "state.json"); err != nil {
		return err
	}
	return h.dir.Sync()
}

func (h *jobHistory) flush() error {
	frame := make([]byte, jobSlotBytes)
	for slot := range h.pending {
		clear(frame)
		if record := h.records[slot]; record.SeenAt != 0 {
			var buffer bytes.Buffer
			encoder := json.NewEncoder(&buffer)
			encoder.SetEscapeHTML(false)
			if err := encoder.Encode(record); err != nil {
				return err
			}
			payload := bytes.TrimSuffix(buffer.Bytes(), []byte("\n"))
			if len(payload) > jobSlotBytes-8 {
				return errUnsafeJobHistory
			}
			binary.LittleEndian.PutUint32(frame, uint32(len(payload)))
			binary.LittleEndian.PutUint32(frame[4:], crc32.ChecksumIEEE(payload))
			copy(frame[8:], payload)
		}
		if _, err := h.file.WriteAt(frame, int64(slot*jobSlotBytes)); err != nil {
			return err
		}
	}
	if err := h.file.Sync(); err != nil {
		return err
	}
	clear(h.pending)
	h.state.Dirty = false
	h.state.Slots, h.state.Records = len(h.records), len(h.index)
	var err error
	h.state.RecordFile, err = h.recordFile()
	if err != nil {
		return err
	}
	return h.writeState()
}

func (h *jobHistory) gap(code string, count uint64) {
	h.state.Gaps[code] += min(count, math.MaxInt64-h.state.Gaps[code])
}

func jobKey(record jobRecord) string {
	if record.JobID != "" {
		return "job:" + record.JobID
	}
	if record.RunnerName != "" {
		return "runner:" + record.RunnerName
	}
	return ""
}

func (h *jobHistory) set(slot int, record jobRecord) {
	if oldKey := jobKey(h.records[slot]); oldKey != "" {
		delete(h.index, oldKey)
	}
	h.records[slot] = record
	h.pending[slot] = struct{}{}
	if record.SeenAt != 0 {
		h.index[jobKey(record)] = slot
		expires := record.SeenAt + jobMaxAge
		if h.nextExpiry == 0 || expires < h.nextExpiry {
			h.nextExpiry = expires
		}
	}
}

func (h *jobHistory) place(record jobRecord) {
	for slot, old := range h.records {
		if old.SeenAt == 0 {
			h.set(slot, record)
			return
		}
	}
	if len(h.records) < jobSlots {
		h.records = append(h.records, jobRecord{})
		h.set(len(h.records)-1, record)
		return
	}
	oldest := 0
	for slot, candidate := range h.records {
		if candidate.SeenAt < h.records[oldest].SeenAt {
			oldest = slot
		}
	}
	h.gap("retention_cap", 1)
	if h.records[oldest].State != "completed" {
		h.gap("retention_nonterminal", 1)
	}
	h.set(oldest, record)
}

func (h *jobHistory) apply(event jobEvent) bool {
	r := event.record
	if event.kind == "recover" || event.kind == "exit" {
		matched := false
		for slot, old := range h.records {
			if old.RunnerName != r.RunnerName {
				continue
			}
			matched = true
			if event.kind == "exit" && old.Gap == "recovered_association_unknown" {
				old.Gap = "runner_exit_without_job"
				h.set(slot, old)
			}
			if event.kind == "exit" && old.State == "running" {
				old.State, old.Gap, old.Epoch = "unknown", "runner_exited", ""
				old.SeenAt = max(old.SeenAt, r.SeenAt)
				h.set(slot, old)
			}
		}
		if !matched {
			r.State, r.Gap = "unknown", "recovered_association_unknown"
			if event.kind == "exit" {
				r.Gap = "runner_exit_without_job"
			}
			h.place(r)
		}
		return true
	}
	if r.JobID == "" {
		return false
	}
	slot, exists := h.index[jobKey(r)]
	if exists {
		old := h.records[slot]
		if (old.Owner != "" && r.Owner != "" && (!strings.EqualFold(old.Owner, r.Owner) || !strings.EqualFold(old.Repository, r.Repository))) ||
			(old.RunID != "" && r.RunID != "" && old.RunID != r.RunID) ||
			(old.RunnerName != "" && r.RunnerName != "" && old.RunnerName != r.RunnerName) {
			return false
		}
	}
	if association, ok := h.index["runner:"+r.RunnerName]; ok {
		if h.records[association].Gap == "runner_exit_without_job" && r.State == "running" {
			r.State, r.Gap = "unknown", "runner_exited"
		}
		h.set(association, jobRecord{})
	}
	if !exists {
		if r.State == "running" {
			r.Epoch = h.state.Epoch
		}
		h.place(r)
		return true
	}
	old := h.records[slot]
	updated := old
	if event.kind == "complete" && (old.CompletedAt == 0 || old.Result == "" && r.Result != "") {
		if updated.CompletedAt == 0 {
			updated.CompletedAt = r.CompletedAt
		}
		updated.State, updated.Result, updated.Gap, updated.Epoch = r.State, r.Result, r.Gap, ""
	}
	if old.StartedAt == 0 && r.StartedAt != 0 {
		updated.StartedAt = r.StartedAt
	}
	if event.kind == "start" && r.State == "unknown" && old.State == "running" {
		updated.State, updated.Gap, updated.Epoch = r.State, r.Gap, ""
	}
	mergeJobMetadata(&updated, r)
	if updated != old {
		updated.SeenAt = max(updated.SeenAt, r.SeenAt)
		h.set(slot, updated)
	}
	return true
}

func mergeJobMetadata(dst *jobRecord, src jobRecord) {
	if dst.Owner == "" {
		dst.Owner, dst.Repository = src.Owner, src.Repository
	}
	if dst.DisplayName == "" {
		dst.DisplayName, dst.DisplayCut = src.DisplayName, src.DisplayCut
	}
	if dst.RunID == "" {
		dst.RunID = src.RunID
	}
	if dst.RunnerName == "" {
		dst.RunnerName = src.RunnerName
	}
	if dst.RunnerID == 0 {
		dst.RunnerID = src.RunnerID
	}
	if dst.QueuedAt == 0 {
		dst.QueuedAt = src.QueuedAt
	}
	if dst.SetAssignedAt == 0 {
		dst.SetAssignedAt = src.SetAssignedAt
	}
	if dst.AssignedAt == 0 {
		dst.AssignedAt = src.AssignedAt
	}
	if dst.FinishedAt == 0 {
		dst.FinishedAt = src.FinishedAt
	}
}

func (h *jobHistory) update(now, poll int64, demand *jobDemand, drops uint64, events []jobEvent, stopped, graceful bool) error {
	before := h.state
	if h.nextExpiry == 0 || h.nextExpiry <= now {
		h.nextExpiry = 0
		for slot, record := range h.records {
			if record.SeenAt == 0 {
				continue
			}
			expires := record.SeenAt + jobMaxAge
			if expires <= now {
				h.set(slot, jobRecord{})
				h.gap("retention_expired", 1)
				if record.State != "completed" {
					h.gap("retention_nonterminal", 1)
				}
			} else if h.nextExpiry == 0 || expires < h.nextExpiry {
				h.nextExpiry = expires
			}
		}
	}
	for _, event := range events {
		if !h.apply(event) {
			drops++
		}
	}
	if drops > 0 {
		h.gap("event_loss", drops)
		h.state.Epoch = uuid.NewString()
	}
	if stopped && graceful {
		h.state.Open = false
	}
	h.state.PollAt, h.state.Demand = poll, demand
	changed := len(h.pending) > 0 || drops > 0 || h.state.Open != before.Open
	if !changed && poll == before.PollAt && demand == before.Demand {
		return nil
	}
	if err := unix.Flock(int(h.dir.Fd()), unix.LOCK_EX); err != nil {
		return err
	}
	defer unix.Flock(int(h.dir.Fd()), unix.LOCK_UN)
	if err := h.validateDirectory(); err != nil {
		return err
	}
	digest, err := h.recordFile()
	if err != nil || digest != h.state.RecordFile {
		return errUnsafeJobHistory
	}
	if !changed {
		return h.writeState()
	}
	h.state.Generation = uuid.NewString()
	h.state.Dirty = len(h.pending) > 0
	if err := h.writeState(); err != nil {
		return err
	}
	if h.state.Dirty {
		if err := h.flush(); err != nil {
			return fmt.Errorf("flush job history: %w", err)
		}
	}
	return nil
}
