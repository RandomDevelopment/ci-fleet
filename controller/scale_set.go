package main

import (
	"context"
	"fmt"
	"slices"

	"github.com/actions/scaleset"
)

type scaleSetStartup interface {
	GetRunnerGroupByName(context.Context, string) (*scaleset.RunnerGroup, error)
	GetRunnerScaleSet(context.Context, int, string) (*scaleset.RunnerScaleSet, error)
	CreateRunnerScaleSet(context.Context, *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error)
	UpdateRunnerScaleSet(context.Context, int, *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error)
}

func ensureScaleSet(ctx context.Context, cfg Config, client scaleSetStartup) (*scaleset.RunnerScaleSet, error) {
	runnerGroupID := 1
	if cfg.RunnerGroup != scaleset.DefaultRunnerGroup {
		group, err := client.GetRunnerGroupByName(ctx, cfg.RunnerGroup)
		if err != nil {
			return nil, fmt.Errorf("find runner group: %w", err)
		}
		runnerGroupID = group.ID
	}
	set, err := client.GetRunnerScaleSet(ctx, runnerGroupID, cfg.ScaleSetName)
	if err != nil {
		return nil, fmt.Errorf("find runner scale set: %w", err)
	}
	if set == nil {
		set, err = client.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{
			Name: cfg.ScaleSetName, RunnerGroupID: runnerGroupID,
			Labels:        cfg.buildLabels(),
			RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
		})
		if err != nil {
			return nil, fmt.Errorf("create runner scale set: %w", err)
		}
	}
	if err := validateScaleSet(cfg, runnerGroupID, set); err != nil {
		return nil, err
	}
	if !set.RunnerSetting.DisableUpdate {
		id := set.ID
		updated := *set
		updated.Labels = slices.Clone(set.Labels)
		updated.RunnerSetting.DisableUpdate = true
		set, err = client.UpdateRunnerScaleSet(ctx, id, &updated)
		if err != nil {
			return nil, fmt.Errorf("disable runner updates: %w", err)
		}
		if err := validateScaleSet(cfg, runnerGroupID, set); err != nil {
			return nil, err
		}
		if set.ID != id {
			return nil, fmt.Errorf("runner scale set identity changed while disabling runner updates")
		}
		if !set.RunnerSetting.DisableUpdate {
			return nil, fmt.Errorf("runner scale set did not disable runner updates")
		}
	}
	return set, nil
}

func validateScaleSet(cfg Config, runnerGroupID int, set *scaleset.RunnerScaleSet) error {
	if set == nil || set.ID <= 0 || set.Name != cfg.ScaleSetName || set.RunnerGroupID != runnerGroupID {
		return fmt.Errorf("runner scale set identity does not match the selected configuration")
	}
	labels := make([]string, 0, len(set.Labels))
	for _, label := range set.Labels {
		labels = append(labels, label.Name)
	}
	expected := slices.Clone(cfg.Labels)
	slices.Sort(labels)
	slices.Sort(expected)
	if !slices.Equal(labels, expected) {
		return fmt.Errorf("runner scale set labels do not match the selected configuration")
	}
	return nil
}
