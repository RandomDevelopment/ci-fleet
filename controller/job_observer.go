package main

import (
	"context"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/actions/scaleset"
)

const (
	jobPrivateDirectory = "/var/lib/ci-fleet-observer/private"
	jobQueueSize        = 16
	jobBatchSize        = 256
	jobPeriod           = 30 * time.Second
	jobStartupBudget    = 100 * time.Millisecond
)

var (
	jobOwner  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)
	jobRepo   = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)
	jobRunner = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	jobScope  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
)

type jobRecord struct {
	JobID         string `json:"job_id,omitempty"`
	Owner         string `json:"owner,omitempty"`
	Repository    string `json:"repository,omitempty"`
	DisplayName   string `json:"display_name,omitempty"`
	DisplayCut    bool   `json:"display_truncated,omitempty"`
	RunID         string `json:"run_id,omitempty"`
	RunnerName    string `json:"runner_name,omitempty"`
	RunnerID      int    `json:"runner_id,omitempty"`
	QueuedAt      int64  `json:"queued_at,omitempty"`
	SetAssignedAt int64  `json:"scale_set_assigned_at,omitempty"`
	AssignedAt    int64  `json:"runner_assigned_at,omitempty"`
	FinishedAt    int64  `json:"finished_at,omitempty"`
	StartedAt     int64  `json:"start_observed_at,omitempty"`
	CompletedAt   int64  `json:"completion_observed_at,omitempty"`
	SeenAt        int64  `json:"last_observed_at"`
	State         string `json:"state"`
	Result        string `json:"result,omitempty"`
	Gap           string `json:"gap,omitempty"`
	Epoch         string `json:"activity_epoch,omitempty"`
}

type jobEvent struct {
	record jobRecord
	kind   string
}

type jobDemand struct {
	ObservedAt int64 `json:"observed_at"`
	Available  int   `json:"available"`
	Assigned   int   `json:"assigned"`
}

type jobBatch struct {
	events []jobEvent
	drops  uint64
	pollAt int64
	demand *jobDemand
}

type jobObserver struct {
	batches  chan jobBatch
	finish   chan bool
	done     chan struct{}
	drops    atomic.Uint64
	stopping atomic.Bool
}

func startJobObserver(parent context.Context, path, controller, scaleSet string, logger *slog.Logger) *jobObserver {
	ctx, cancel := context.WithTimeout(parent, jobStartupBudget)
	defer cancel()
	observer := &jobObserver{batches: make(chan jobBatch, jobQueueSize), finish: make(chan bool, 1), done: make(chan struct{})}
	ready := make(chan bool)
	go func() {
		defer close(observer.done)
		store, err := openJobHistory(path, controller, scaleSet)
		if err != nil {
			if !jobHistoryAbsent(err) {
				logger.Warn("job history unavailable")
			}
		} else {
			defer store.close()
		}
		select {
		case ready <- err == nil:
		case <-ctx.Done():
			return
		}
		if err == nil && observer.run(store) != nil {
			logger.Warn("job history unavailable")
		}
	}()
	select {
	case available := <-ready:
		if available {
			return observer
		}
	case <-ctx.Done():
		logger.Warn("job history startup deadline elapsed")
	}
	return nil
}

func (o *jobObserver) stop(graceful bool) {
	if o == nil {
		return
	}
	o.stopping.Store(true)
	o.finish <- graceful
	timer := time.NewTimer(jobStartupBudget)
	defer timer.Stop()
	select {
	case <-o.done:
	case <-timer.C:
	}
}

func (o *jobObserver) run(store *jobHistory) error {
	ticker := time.NewTicker(jobPeriod)
	defer ticker.Stop()
	var poll int64
	var demand *jobDemand
	for {
		var events []jobEvent
		var drops uint64
		collect := func(batch jobBatch) {
			events = append(events, batch.events...)
			drops += batch.drops
			poll = max(poll, batch.pollAt)
			if batch.demand != nil {
				demand = batch.demand
			}
		}
		var stopped, graceful bool
		select {
		case first := <-o.batches:
			collect(first)
		case <-ticker.C:
		case graceful = <-o.finish:
			stopped = true
		}
		// A dropped batch follows everything already queued when its loss is recorded.
		lost := o.drops.Swap(0)
		count := len(o.batches)
		for i := 0; i < count; i++ {
			collect(<-o.batches)
		}
		if err := store.update(time.Now().Unix(), poll, demand, drops+lost, events, stopped, graceful); err != nil {
			return err
		}
		if stopped {
			return nil
		}
	}
}

func (o *jobObserver) submit(batch jobBatch) {
	select {
	case o.batches <- batch:
	default:
		o.drops.Add(batch.drops + uint64(len(batch.events)))
	}
}

func (o *jobObserver) batch(message *scaleset.RunnerScaleSetMessage, now, pollAt int64, active func(string) bool) {
	batch := jobBatch{pollAt: pollAt}
	if message == nil {
		o.submit(batch)
		return
	}
	if stats := message.Statistics; stats != nil && stats.TotalAvailableJobs >= 0 && stats.TotalAssignedJobs >= 0 {
		batch.demand = &jobDemand{ObservedAt: now, Available: stats.TotalAvailableJobs, Assigned: stats.TotalAssignedJobs}
	}
	if len(o.batches) == cap(o.batches) {
		o.drops.Add(uint64(len(message.JobStartedMessages)) + uint64(len(message.JobCompletedMessages)))
		return
	}
	// Preserve terminal evidence first if the upstream batch exceeds the observer budget.
	completions := min(len(message.JobCompletedMessages), jobBatchSize)
	starts := min(len(message.JobStartedMessages), jobBatchSize-completions)
	batch.drops = uint64(len(message.JobCompletedMessages)-completions) + uint64(len(message.JobStartedMessages)-starts)
	for _, job := range message.JobStartedMessages[:starts] {
		if job == nil {
			batch.drops++
			continue
		}
		r := projectJob(job.JobMessageBase, job.RunnerName, job.RunnerID, now)
		r.StartedAt, r.State = now, "running"
		if !active(r.RunnerName) {
			r.State, r.Gap = "unknown", "runner_activity_unverified"
		}
		batch.events = append(batch.events, jobEvent{record: r, kind: "start"})
	}
	for _, job := range message.JobCompletedMessages[:completions] {
		if job == nil {
			batch.drops++
			continue
		}
		r := projectJob(job.JobMessageBase, job.RunnerName, job.RunnerID, now)
		r.CompletedAt, r.State = now, "completed"
		result := ""
		if len(job.Result) <= 32 {
			result = strings.ToLower(job.Result)
		}
		switch result {
		case "succeeded", "success":
			r.Result = "success"
		case "failed", "failure":
			r.Result = "failure"
		case "canceled", "cancelled":
			r.Result = "cancelled"
		case "skipped", "timed_out", "neutral", "action_required", "stale", "startup_failure":
			r.Result = result
		default:
			r.Gap = "result_unknown"
		}
		batch.events = append(batch.events, jobEvent{record: r, kind: "complete"})
	}
	o.submit(batch)
}

func (o *jobObserver) runner(name, kind string) {
	if o != nil && !o.stopping.Load() && jobRunner.MatchString(name) {
		event := jobEvent{record: jobRecord{RunnerName: name, SeenAt: time.Now().Unix()}, kind: kind}
		o.submit(jobBatch{events: []jobEvent{event}})
	}
}

func projectJob(job scaleset.JobMessageBase, runner string, runnerID int, now int64) jobRecord {
	record := jobRecord{SeenAt: now}
	if len(job.JobID) <= 256 && utf8.ValidString(job.JobID) && strings.IndexFunc(job.JobID, unicode.IsControl) < 0 {
		record.JobID = strings.Clone(job.JobID)
	}
	if jobOwner.MatchString(job.OwnerName) && jobRepo.MatchString(job.RepositoryName) && job.RepositoryName != "." && job.RepositoryName != ".." {
		record.Owner, record.Repository = strings.Clone(job.OwnerName), strings.Clone(job.RepositoryName)
	}
	if job.WorkflowRunID > 0 {
		record.RunID = strconv.FormatInt(job.WorkflowRunID, 10)
	}
	if jobRunner.MatchString(runner) {
		record.RunnerName = strings.Clone(runner)
	}
	if runnerID > 0 {
		record.RunnerID = runnerID
	}
	var name strings.Builder
	for _, r := range job.JobDisplayName {
		if unicode.IsControl(r) {
			r = ' '
		}
		if name.Len()+utf8.RuneLen(r) > 512 {
			record.DisplayCut = true
			break
		}
		name.WriteRune(r)
	}
	record.DisplayName = name.String()
	record.QueuedAt = jobSourceTime(job.QueueTime, now)
	record.SetAssignedAt = jobSourceTime(job.ScaleSetAssignTime, now)
	record.AssignedAt = jobSourceTime(job.RunnerAssignTime, now)
	record.FinishedAt = jobSourceTime(job.FinishTime, now)
	return record
}

func jobSourceTime(source time.Time, now int64) int64 {
	if seconds := source.Unix(); seconds > 0 && seconds <= now+300 {
		return seconds
	}
	return 0
}
