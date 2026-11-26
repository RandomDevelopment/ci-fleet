package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/actions/scaleset"
	"golang.org/x/sys/unix"
)

func jobFixture(t *testing.T) string {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("protected job store requires root ownership")
	}
	base := filepath.Join(t.TempDir(), "observer")
	path := filepath.Join(base, "private")
	if err := os.MkdirAll(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(base, 0750); err != nil {
		t.Fatal(err)
	}
	return path
}

func fixtureHistory(t *testing.T, path string) *jobHistory {
	t.Helper()
	history, err := openJobHistory(path, "example", "docker-ci-example")
	if err != nil {
		t.Fatal(err)
	}
	return history
}

func testJob(id, name string, now int64, completed bool) jobEvent {
	row := jobRecord{JobID: id, RunnerName: name, SeenAt: now, StartedAt: now, State: "running"}
	if completed {
		row.StartedAt = 0
		row.CompletedAt, row.State, row.Result = now, "completed", "success"
		return jobEvent{kind: "complete", record: row}
	}
	return jobEvent{kind: "start", record: row}
}

func persistJobs(t *testing.T, history *jobHistory, now int64, jobs ...jobEvent) {
	t.Helper()
	if err := history.update(now, now, nil, 0, jobs, false, false); err != nil {
		t.Fatal(err)
	}
}

func TestJobHistoryRecoversTerminalAndUnknownAssociation(t *testing.T) {
	path := jobFixture(t)
	now := int64(1_800_000_000)
	history := fixtureHistory(t, path)
	if !history.state.Open || history.state.Dirty {
		t.Fatal("listener marker was not committed")
	}
	if _, err := openJobHistory(path, "example", "docker-ci-example"); err == nil {
		t.Fatal("a second writer opened the controller's history")
	}
	complete := testJob("opaque:176", "runner-done", now, true)
	incomplete := testJob("unfinished", "runner-lost", now, false)
	recovered := jobEvent{kind: "recover", record: jobRecord{RunnerName: "runner-recovered", SeenAt: now}}
	persistJobs(t, history, now, complete, incomplete, recovered)
	firstEpoch := history.state.Epoch
	generation := history.state.Generation
	persistJobs(t, history, now+1, complete)
	if history.state.Generation != generation {
		t.Fatal("duplicate completion changed the generation")
	}
	history.close()

	history = fixtureHistory(t, path)
	defer history.close()
	if history.state.Gaps["unclean_session"] != 1 || history.state.Epoch == firstEpoch {
		t.Fatalf("restart did not retain an unknown coverage gap: %+v", history.state)
	}
	lateStart := testJob("opaque:176", "runner-done", now+2, false)
	conflictingCompletion := complete
	conflictingCompletion.record.Result = "failure"
	exit := jobEvent{kind: "exit", record: jobRecord{RunnerName: "runner-lost", SeenAt: now + 2}}
	persistJobs(t, history, now+2, lateStart, conflictingCompletion, exit)
	done := history.records[history.index["job:opaque:176"]]
	missing := history.records[history.index["job:unfinished"]]
	unknown := history.records[history.index["runner:runner-recovered"]]
	if done.Result != "success" || done.State != "completed" || done.StartedAt != now+2 || done.CompletedAt != now {
		t.Fatalf("late and conflicting evidence changed terminal result: %+v", done)
	}
	if missing.State != "unknown" || missing.Result != "" || missing.Gap != "runner_exited" ||
		unknown.JobID != "" || unknown.Gap != "recovered_association_unknown" {
		t.Fatalf("missing completion or runner association was invented: %+v %+v", missing, unknown)
	}
	if err := history.update(now+2, now+2, nil, 0, nil, true, true); err != nil {
		t.Fatal(err)
	}
	if history.state.Open || history.state.Gaps["unclean_session"] != 1 {
		t.Fatal("graceful shutdown lost historical gap or kept the open marker")
	}
}

func TestJobHistoryBoundsAndIdleSweep(t *testing.T) {
	path := jobFixture(t)
	now := int64(1_800_000_000)
	history := fixtureHistory(t, path)
	defer history.close()
	events := make([]jobEvent, jobSlots+1)
	for i := range events {
		events[i] = testJob(fmt.Sprintf("job-%d", i), "", now+int64(i), i != 0)
		events[i].record.DisplayName = strings.Repeat("あ", 170)
	}
	started := time.Now()
	persistJobs(t, history, now+jobSlots, events...)
	elapsed := time.Since(started)
	var allocated int64
	entries, err := os.ReadDir(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		var info unix.Stat_t
		if err := unix.Stat(filepath.Join(path, entry.Name()), &info); err != nil {
			t.Fatal(err)
		}
		allocated += info.Blocks * 512
	}
	t.Logf("private store at capacity: %d records, %d allocated bytes across %d files, fixture write %s",
		len(history.index), allocated, len(entries), elapsed)
	if len(history.index) != jobSlots || history.state.Gaps["retention_cap"] != 1 || history.state.Gaps["retention_nonterminal"] != 1 || allocated > 32<<20 {
		t.Fatalf("private history exceeded its bounds: %+v", history.state)
	}
	persistJobs(t, history, now+jobSlots+jobMaxAge)
	if len(history.index) != 0 || history.state.Gaps["retention_expired"] != jobSlots {
		t.Fatalf("records remained after an idle sweep: %+v", history.state)
	}
	frame := make([]byte, jobSlotBytes)
	if _, err := history.file.ReadAt(frame, 0); err != nil || strings.Trim(string(frame), "\x00") != "" {
		t.Fatalf("expired payload remains on disk: %v", err)
	}
}

func TestJobHistoryRecoversTruncatedSlotWithoutInventingResults(t *testing.T) {
	path := jobFixture(t)
	now := int64(1_800_000_000)
	history := fixtureHistory(t, path)
	persistJobs(t, history, now, testJob("first", "runner-1", now, true), testJob("last", "runner-2", now, false))
	if err := history.file.Truncate(jobSlotBytes + 7); err != nil {
		t.Fatal(err)
	}
	history.close()
	history = fixtureHistory(t, path)
	defer history.close()
	if history.state.Gaps["storage_corruption"] == 0 || history.state.Gaps["unclean_session"] == 0 ||
		len(history.index) != 1 || history.records[0].Result != "success" {
		t.Fatalf("recovery lost intact terminal evidence or did not expose loss: %+v", history.state)
	}
}

func TestJobHistoryDetectsZeroedSlotWithoutLosingOtherResults(t *testing.T) {
	path := jobFixture(t)
	now := int64(1_800_000_000)
	history := fixtureHistory(t, path)
	persistJobs(t, history, now, testJob("first", "runner-1", now, true), testJob("last", "runner-2", now, false))
	if _, err := history.file.WriteAt(make([]byte, jobSlotBytes), jobSlotBytes); err != nil {
		t.Fatal(err)
	}
	if err := history.update(now+1, now+1, nil, 0, nil, false, false); err == nil {
		t.Fatal("writer accepted an externally changed records file")
	}
	history.close()
	history = fixtureHistory(t, path)
	defer history.close()
	if history.state.Gaps["storage_corruption"] == 0 || history.state.Records != 1 ||
		history.records[0].Result != "success" {
		t.Fatalf("valid historical result or corruption evidence lost: %+v", history.state)
	}
}

func TestJobObserverBoundsAndAllowlistsListenerMetadata(t *testing.T) {
	now := int64(1_800_000_000)
	o := &jobObserver{batches: make(chan jobBatch, 1)}
	base := scaleset.JobMessageBase{JobID: "opaque/job:abc", OwnerName: "owner", RepositoryName: "../unsafe",
		WorkflowRunID: 9007199254740993, JobDisplayName: strings.Repeat("😀", 130), JobWorkflowRef: "private-ref",
		QueueTime: time.Unix(now-10, 0), RunnerAssignTime: time.Unix(now+500, 0)}
	o.batch(&scaleset.RunnerScaleSetMessage{
		JobStartedMessages: []*scaleset.JobStarted{{JobMessageBase: base, RunnerName: "runner-1"}, {JobMessageBase: base}},
		Statistics:         &scaleset.RunnerScaleSetStatistic{TotalAvailableJobs: 2, TotalAssignedJobs: 3},
	}, now, now, func(name string) bool { return name == "runner-1" })
	o.batch(&scaleset.RunnerScaleSetMessage{JobStartedMessages: []*scaleset.JobStarted{{JobMessageBase: base}}}, now, now, nil)
	batch := <-o.batches
	if len(batch.events) != 2 || batch.pollAt != now || batch.demand.Assigned != 3 {
		t.Fatalf("listener events and liveness were split: %+v", batch)
	}
	r := batch.events[0].record
	if r.Owner != "" || r.Repository != "" || r.RunID != "9007199254740993" || r.JobID != base.JobID ||
		len(r.DisplayName) != 512 || !r.DisplayCut || r.QueuedAt != now-10 || r.AssignedAt != 0 ||
		o.drops.Load() != 1 || batch.events[1].record.State != "unknown" {
		t.Fatalf("wrong listener projection: %+v, dropped %d", r, o.drops.Load())
	}
}
