package main

import (
	"context"
	"testing"

	"github.com/actions/scaleset"
)

type fakeScaleSetAdmin struct {
	group   *scaleset.RunnerGroup
	set     *scaleset.RunnerScaleSet
	detail  *scaleset.RunnerScaleSet
	deleted bool
}

func (f *fakeScaleSetAdmin) GetRunnerGroupByName(context.Context, string) (*scaleset.RunnerGroup, error) {
	return f.group, nil
}

func (f *fakeScaleSetAdmin) GetRunnerScaleSet(context.Context, int, string) (*scaleset.RunnerScaleSet, error) {
	return f.set, nil
}

func (f *fakeScaleSetAdmin) GetRunnerScaleSetByID(context.Context, int) (*scaleset.RunnerScaleSet, error) {
	return f.detail, nil
}

func (f *fakeScaleSetAdmin) DeleteRunnerScaleSet(context.Context, int) error {
	f.deleted = true
	return nil
}

func TestRemoveIdleScaleSet(t *testing.T) {
	cfg := Config{RunnerGroup: "trusted-private-ci", ScaleSetName: "docker-ci-example"}
	idle := scaleset.RunnerScaleSetStatistic{}
	newAdmin := func(statistics *scaleset.RunnerScaleSetStatistic) *fakeScaleSetAdmin {
		return &fakeScaleSetAdmin{
			group: &scaleset.RunnerGroup{ID: 7, Name: cfg.RunnerGroup},
			set: &scaleset.RunnerScaleSet{ID: 42, Name: cfg.ScaleSetName, RunnerGroupID: 7, Statistics: statistics},
		}
	}

	admin := newAdmin(&idle)
	deleted, err := removeIdleScaleSet(context.Background(), cfg, admin)
	if err != nil || !deleted || !admin.deleted {
		t.Fatalf("idle scale set was not deleted: deleted=%v called=%v err=%v", deleted, admin.deleted, err)
	}

	absent := newAdmin(&idle)
	absent.set = nil
	deleted, err = removeIdleScaleSet(context.Background(), cfg, absent)
	if err != nil || deleted || absent.deleted {
		t.Fatalf("absent scale set should be a no-op: deleted=%v called=%v err=%v", deleted, absent.deleted, err)
	}

	withoutStatistics := newAdmin(nil)
	withoutStatistics.detail = &scaleset.RunnerScaleSet{ID: 42, Name: cfg.ScaleSetName, RunnerGroupID: 7, Statistics: &idle}
	deleted, err = removeIdleScaleSet(context.Background(), cfg, withoutStatistics)
	if err != nil || !deleted || !withoutStatistics.deleted {
		t.Fatalf("detailed idle statistics were not used: deleted=%v called=%v err=%v", deleted, withoutStatistics.deleted, err)
	}

	busy := map[string]scaleset.RunnerScaleSetStatistic{
		"available_jobs":     {TotalAvailableJobs: 1},
		"acquired_jobs":      {TotalAcquiredJobs: 1},
		"assigned_jobs":      {TotalAssignedJobs: 1},
		"running_jobs":       {TotalRunningJobs: 1},
		"registered_runners": {TotalRegisteredRunners: 1},
		"busy_runners":       {TotalBusyRunners: 1},
		"idle_runners":       {TotalIdleRunners: 1},
	}
	for name, statistics := range busy {
		t.Run(name, func(t *testing.T) {
			admin := newAdmin(&statistics)
			deleted, err := removeIdleScaleSet(context.Background(), cfg, admin)
			if err == nil || deleted || admin.deleted {
				t.Fatalf("non-idle scale set was deletable: deleted=%v called=%v err=%v", deleted, admin.deleted, err)
			}
		})
	}
}
