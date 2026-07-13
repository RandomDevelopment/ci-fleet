// Portions of this file follow the actions/scaleset Docker example.
// See NOTICE and THIRD_PARTY_NOTICES.md for attribution.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	dockerclient "github.com/docker/docker/client"
)

var (
	version = "dev"
	commitSHA = "unknown"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	cfg, err := configFromEnv()
	if err != nil { return fmt.Errorf("configuration: %w", err) }
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	client, err := cfg.scaleSetClient()
	if err != nil { return fmt.Errorf("create scale-set client: %w", err) }
	runnerGroupID := 1
	if cfg.RunnerGroup != scaleset.DefaultRunnerGroup {
		group, err := client.GetRunnerGroupByName(ctx, cfg.RunnerGroup)
		if err != nil { return fmt.Errorf("find runner group: %w", err) }
		runnerGroupID = group.ID
	}
	set, err := client.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{
		Name: cfg.ScaleSetName, RunnerGroupID: runnerGroupID,
		Labels: cfg.buildLabels(),
		RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
	})
	if err != nil { return fmt.Errorf("create runner scale set: %w", err) }
	client.SetSystemInfo(systemInfo(set.ID))
	defer func() {
		if err := client.DeleteRunnerScaleSet(context.WithoutCancel(ctx), set.ID); err != nil {
			logger.Error("delete runner scale set", "scaleSetID", set.ID, "error", err)
		}
	}()

	docker, err := dockerclient.NewClientWithOpts(dockerclient.FromEnv, dockerclient.WithAPIVersionNegotiation())
	if err != nil { return fmt.Errorf("create Docker client: %w", err) }
	defer docker.Close()
	if _, err := docker.Ping(ctx); err != nil { return fmt.Errorf("ping Docker: %w", err) }
	if _, err := docker.ImageInspect(ctx, cfg.RunnerImage); err != nil {
		return fmt.Errorf("inspect runner image %q (build it before starting the controller): %w", cfg.RunnerImage, err)
	}

	scaler := &Scaler{runners: newRunnerState(), dockerClient: docker, scalesetClient: client, logger: logger, config: cfg, scaleSetID: set.ID}
	if err := scaler.recoverStale(ctx); err != nil { return err }
	defer scaler.shutdown(context.WithoutCancel(ctx))
	hostname, err := os.Hostname()
	if err != nil { return fmt.Errorf("get hostname: %w", err) }
	session, err := client.MessageSessionClient(ctx, set.ID, cfg.FleetInstance+"@"+hostname)
	if err != nil { return fmt.Errorf("create message session: %w", err) }
	defer session.Close(context.Background())
	l, err := listener.New(session, listener.Config{ScaleSetID: set.ID, MaxRunners: cfg.MaxRunners, Logger: logger.WithGroup("listener")})
	if err != nil { return fmt.Errorf("create listener: %w", err) }
	logger.Info("controller ready", "scaleSet", cfg.ScaleSetName, "minRunners", cfg.MinRunners, "maxRunners", cfg.MaxRunners)
	if err := l.Run(ctx, scaler); err != nil && !errors.Is(err, context.Canceled) {
		return fmt.Errorf("listener: %w", err)
	}
	return nil
}
