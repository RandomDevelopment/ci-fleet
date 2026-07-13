// Portions of this file follow the actions/scaleset Docker example.
// See NOTICE and THIRD_PARTY_NOTICES.md for attribution.
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	dockerclient "github.com/docker/docker/client"
	"github.com/google/uuid"
)

const labelPrefix = "io.randomdevelopment.ci-fleet."

type Scaler struct {
	runners        runnerState
	dockerClient   *dockerclient.Client
	scalesetClient *scaleset.Client
	logger         *slog.Logger
	config         Config
	scaleSetID     int
}

func (s *Scaler) HandleDesiredRunnerCount(ctx context.Context, count int) (int, error) {
	current := s.runners.count()
	target := min(s.config.MaxRunners, s.config.MinRunners+count)
	for i := current; i < target; i++ {
		if _, err := s.startRunner(ctx); err != nil {
			return s.runners.count(), fmt.Errorf("start runner: %w", err)
		}
	}
	return s.runners.count(), nil
}

func (s *Scaler) HandleJobStarted(_ context.Context, job *scaleset.JobStarted) error {
	if !s.runners.markBusy(job.RunnerName) {
		return fmt.Errorf("job started for unknown runner %q", job.RunnerName)
	}
	s.logger.Info("job started", "runner", job.RunnerName, "jobID", job.JobID)
	return nil
}

func (s *Scaler) HandleJobCompleted(ctx context.Context, job *scaleset.JobCompleted) error {
	id, ok := s.runners.markDone(job.RunnerName)
	if !ok {
		return fmt.Errorf("job completed for unknown runner %q", job.RunnerName)
	}
	s.logger.Info("job completed", "runner", job.RunnerName, "jobID", job.JobID)
	return s.logAndRemove(ctx, job.RunnerName, id)
}

func (s *Scaler) startRunner(ctx context.Context) (string, error) {
	name := fmt.Sprintf("ci-fleet-%s-%s", s.config.FleetInstance, uuid.NewString()[:8])
	jit, err := s.scalesetClient.GenerateJitRunnerConfig(ctx, &scaleset.RunnerScaleSetJitRunnerSetting{Name: name}, s.scaleSetID)
	if err != nil {
		return "", fmt.Errorf("generate JIT config: %w", err)
	}
	now := time.Now().UTC()
	labels := map[string]string{
		labelPrefix+"managed": "true",
		labelPrefix+"kind": "runner",
		labelPrefix+"instance": s.config.FleetInstance,
		labelPrefix+"scale-set": s.config.ScaleSetName,
		labelPrefix+"created-at": fmt.Sprint(now.Unix()),
		labelPrefix+"expires-at": fmt.Sprint(now.Add(s.config.RunnerTTL).Unix()),
	}
	created, err := s.dockerClient.ContainerCreate(ctx,
		&container.Config{
			Image: s.config.RunnerImage, User: "runner", Cmd: []string{"/home/runner/run.sh"},
			Env: []string{"ACTIONS_RUNNER_INPUT_JITCONFIG=" + jit.EncodedJITConfig}, Labels: labels,
		},
		&container.HostConfig{
			Binds: []string{"/var/run/docker.sock:/var/run/docker.sock"},
			GroupAdd: []string{s.config.DockerGID},
			Resources: container.Resources{Memory: s.config.RunnerMemory, NanoCPUs: s.config.RunnerCPUs * 1_000_000_000},
			LogConfig: container.LogConfig{Type: "json-file", Config: map[string]string{"max-size": "10m", "max-file": "3"}},
			SecurityOpt: []string{"no-new-privileges=true"},
		}, nil, nil, name)
	if err != nil {
		return "", fmt.Errorf("create runner container: %w", err)
	}
	if err := s.dockerClient.ContainerStart(ctx, created.ID, container.StartOptions{}); err != nil {
		_ = s.dockerClient.ContainerRemove(context.WithoutCancel(ctx), created.ID, container.RemoveOptions{Force: true})
		return "", fmt.Errorf("start runner container: %w", err)
	}
	s.runners.addIdle(name, created.ID)
	s.logger.Info("runner started", "runner", name, "containerID", created.ID)
	return name, nil
}

func (s *Scaler) recoverStale(ctx context.Context) error {
	f := filters.NewArgs(
		filters.Arg("label", labelPrefix+"managed=true"),
		filters.Arg("label", labelPrefix+"kind=runner"),
		filters.Arg("label", labelPrefix+"instance="+s.config.FleetInstance),
	)
	containers, err := s.dockerClient.ContainerList(ctx, container.ListOptions{All: true, Filters: f})
	if err != nil { return fmt.Errorf("list stale runner containers: %w", err) }
	for _, c := range containers {
		name := c.ID[:12]
		if len(c.Names) > 0 { name = c.Names[0] }
		s.logger.Warn("removing stale runner from prior controller lifetime", "runner", name)
		if err := s.logAndRemove(ctx, name, c.ID); err != nil { return err }
	}
	return nil
}

func (s *Scaler) logAndRemove(ctx context.Context, name, id string) error {
	logs, err := s.dockerClient.ContainerLogs(ctx, id, container.LogsOptions{ShowStdout: true, ShowStderr: true, Timestamps: true, Tail: "2000"})
	if err == nil {
		_, _ = fmt.Fprintf(os.Stdout, "--- runner log: %s ---\n", name)
		_, _ = io.Copy(os.Stdout, logs)
		_ = logs.Close()
	} else {
		s.logger.Warn("could not collect runner logs", "runner", name, "error", err)
	}
	if err := s.dockerClient.ContainerRemove(ctx, id, container.RemoveOptions{Force: true, RemoveVolumes: true}); err != nil {
		return fmt.Errorf("remove runner %s: %w", name, err)
	}
	return nil
}

func (s *Scaler) shutdown(ctx context.Context) {
	for name, id := range s.runners.drain() {
		if err := s.logAndRemove(ctx, name, id); err != nil {
			s.logger.Error("runner shutdown failed", slog.String("runner", name), slog.String("error", err.Error()))
		}
	}
}

var _ listener.Scaler = (*Scaler)(nil)
