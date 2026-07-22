package main

import (
	"context"
	"fmt"

	"github.com/actions/scaleset"
)

type scaleSetAdmin interface {
	GetRunnerGroupByName(context.Context, string) (*scaleset.RunnerGroup, error)
	GetRunnerScaleSet(context.Context, int, string) (*scaleset.RunnerScaleSet, error)
	GetRunnerScaleSetByID(context.Context, int) (*scaleset.RunnerScaleSet, error)
	DeleteRunnerScaleSet(context.Context, int) error
}

func removeIdleScaleSet(ctx context.Context, cfg Config, client scaleSetAdmin) (bool, error) {
	runnerGroupID := 1
	if cfg.RunnerGroup != scaleset.DefaultRunnerGroup {
		group, err := client.GetRunnerGroupByName(ctx, cfg.RunnerGroup)
		if err != nil {
			return false, fmt.Errorf("find runner group: %w", err)
		}
		runnerGroupID = group.ID
	}
	set, err := client.GetRunnerScaleSet(ctx, runnerGroupID, cfg.ScaleSetName)
	if err != nil {
		return false, fmt.Errorf("find runner scale set: %w", err)
	}
	if set == nil {
		return false, nil
	}
	if set.Name != cfg.ScaleSetName || set.RunnerGroupID != runnerGroupID {
		return false, fmt.Errorf("runner scale set identity does not match the selected configuration")
	}
	if set.Statistics == nil {
		set, err = client.GetRunnerScaleSetByID(ctx, set.ID)
		if err != nil {
			return false, fmt.Errorf("read runner scale set statistics: %w", err)
		}
		if set == nil || set.Name != cfg.ScaleSetName || set.RunnerGroupID != runnerGroupID {
			return false, fmt.Errorf("runner scale set identity changed while checking statistics")
		}
	}
	if set.Statistics == nil {
		return false, fmt.Errorf("runner scale set has no idle-state statistics")
	}
	if *set.Statistics != (scaleset.RunnerScaleSetStatistic{}) {
		return false, fmt.Errorf("runner scale set is not idle")
	}
	if err := client.DeleteRunnerScaleSet(ctx, set.ID); err != nil {
		return false, fmt.Errorf("delete idle runner scale set: %w", err)
	}
	return true, nil
}

func deleteIdleScaleSet(ctx context.Context) error {
	cfg, err := configFromEnv()
	if err != nil {
		return fmt.Errorf("configuration: %w", err)
	}
	client, err := cfg.scaleSetClient()
	if err != nil {
		return fmt.Errorf("create scale-set client: %w", err)
	}
	deleted, err := removeIdleScaleSet(ctx, cfg, client)
	if err != nil {
		return err
	}
	if deleted {
		fmt.Printf("deleted idle runner scale set %q\n", cfg.ScaleSetName)
	} else {
		fmt.Printf("runner scale set %q is already absent\n", cfg.ScaleSetName)
	}
	return nil
}
