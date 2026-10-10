// Portions of this file follow the actions/scaleset Docker example.
// See NOTICE and THIRD_PARTY_NOTICES.md for attribution.
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	dockerclient "github.com/docker/docker/client"
	"github.com/docker/docker/errdefs"
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
	defer s.writeStatus()
	current := s.runners.count()
	target := min(s.config.MaxRunners, s.config.MinRunners+count)
	if target > current && s.config.DockerNetworksPerRunner > 0 {
		available, err := s.availableNetworkRunnerSlots(ctx)
		if err != nil {
			s.logger.Error("runner creation blocked by Docker network headroom inspection", "error", err)
			return current, nil
		}
		// ponytail: double-count current job networks until ownership labels make attribution safe.
		target = min(target, available)
		if target <= current {
			s.logger.Warn("runner creation blocked by Docker network low water")
		}
	}
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
	s.writeStatus()
	s.logger.Info("job started", "runner", job.RunnerName, "jobID", job.JobID)
	return nil
}

func (s *Scaler) HandleJobCompleted(ctx context.Context, job *scaleset.JobCompleted) error {
	id, cleanup, ok := s.runners.markDone(job.RunnerName)
	if !ok {
		return fmt.Errorf("job completed for unknown runner %q", job.RunnerName)
	}
	s.writeStatus()
	s.logger.Info("job completed", "runner", job.RunnerName, "jobID", job.JobID)
	if !cleanup {
		return nil
	}
	return s.logAndRemove(ctx, job.RunnerName, id, true)
}

func (s *Scaler) startRunner(ctx context.Context) (string, error) {
	name := fmt.Sprintf("ci-fleet-%s-%s", s.config.FleetInstance, uuid.NewString()[:8])
	jit, err := s.scalesetClient.GenerateJitRunnerConfig(ctx, &scaleset.RunnerScaleSetJitRunnerSetting{Name: name}, s.scaleSetID)
	if err != nil {
		return "", fmt.Errorf("generate JIT config: %w", err)
	}
	now := time.Now().UTC()
	labels := map[string]string{
		labelPrefix + "managed":    "true",
		labelPrefix + "kind":       "runner",
		labelPrefix + "instance":   s.config.FleetInstance,
		labelPrefix + "scale-set":  s.config.ScaleSetName,
		labelPrefix + "created-at": fmt.Sprint(now.Unix()),
		labelPrefix + "expires-at": fmt.Sprint(now.Add(s.config.RunnerTTL).Unix()),
	}
	created, err := s.dockerClient.ContainerCreate(ctx,
		&container.Config{
			Image: s.config.RunnerImage, User: "runner", Cmd: []string{"/home/runner/run.sh"},
			Env: []string{"ACTIONS_RUNNER_INPUT_JITCONFIG=" + jit.EncodedJITConfig}, Labels: labels,
		},
		&container.HostConfig{
			Binds:       []string{"/var/run/docker.sock:/var/run/docker.sock"},
			GroupAdd:    []string{s.config.DockerGID},
			Resources:   container.Resources{Memory: s.config.RunnerMemory, NanoCPUs: s.config.RunnerCPUs * 1_000_000_000},
			LogConfig:   container.LogConfig{Type: "json-file", Config: map[string]string{"max-size": "10m", "max-file": "3"}},
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
	go s.watchRunner(context.WithoutCancel(ctx), name, created.ID)
	s.logger.Info("runner started", "runner", name, "containerID", created.ID)
	return name, nil
}

func (s *Scaler) watchRunner(ctx context.Context, name, id string) {
	for {
		stopped, errors := s.dockerClient.ContainerWait(ctx, id, container.WaitConditionNotRunning)
		select {
		case err, ok := <-errors:
			if !s.runners.contains(name, id) {
				return
			}
			if !ok || !errdefs.IsNotFound(err) {
				if ok {
					s.logger.Warn("watch runner container", "runner", name, "error", err)
				}
				time.Sleep(time.Minute)
				continue
			}
		case response, ok := <-stopped:
			if !s.runners.contains(name, id) {
				return
			}
			if !ok || response.Error != nil {
				if ok {
					s.logger.Warn("watch runner container", "runner", name, "error", response.Error.Message)
				}
				time.Sleep(time.Minute)
				continue
			}
		}
		// Docker can wake a not-running waiter while the container is restarting.
		current, err := s.dockerClient.ContainerInspect(ctx, id)
		if !errdefs.IsNotFound(err) {
			if err != nil || current.State == nil {
				s.logger.Warn("inspect exited runner", "runner", name, "error", err)
				time.Sleep(time.Minute)
				continue
			}
			if current.State.Running || current.State.Restarting || current.State.Paused {
				continue
			}
		}
		if err := s.logAndRemove(ctx, name, id, false); err != nil {
			s.logger.Warn("retry exited runner cleanup", "runner", name, "error", err)
			time.Sleep(time.Minute)
			continue
		}
		if !s.runners.markExited(name, id) {
			return
		}
		s.writeStatus()
		s.logger.Warn("runner container exited before job completion", "runner", name)
		time.AfterFunc(10*time.Minute, func() { s.runners.forgetExited(name, id) })
		return
	}
}

func (s *Scaler) recoverRunners(ctx context.Context) error {
	f := filters.NewArgs(
		filters.Arg("label", labelPrefix+"managed=true"),
		filters.Arg("label", labelPrefix+"kind=runner"),
		filters.Arg("label", labelPrefix+"instance="+s.config.FleetInstance),
		filters.Arg("label", labelPrefix+"scale-set="+s.config.ScaleSetName),
	)
	containers, err := s.dockerClient.ContainerList(ctx, container.ListOptions{All: true, Filters: f})
	if err != nil {
		return fmt.Errorf("list prior runner containers: %w", err)
	}
	for _, c := range containers {
		current, err := s.dockerClient.ContainerInspect(ctx, c.ID)
		if errdefs.IsNotFound(err) {
			continue
		}
		if err != nil {
			return fmt.Errorf("inspect prior runner: %w", err)
		}
		if current.Config == nil || current.State == nil {
			return fmt.Errorf("prior runner %s has no configuration or state", c.ID)
		}
		labels := current.Config.Labels
		if labels[labelPrefix+"managed"] != "true" || labels[labelPrefix+"kind"] != "runner" ||
			labels[labelPrefix+"instance"] != s.config.FleetInstance || labels[labelPrefix+"scale-set"] != s.config.ScaleSetName {
			continue
		}
		name := strings.TrimPrefix(current.Name, "/")
		if current.State.Running || current.State.Restarting || current.State.Paused {
			if name == "" {
				return fmt.Errorf("active prior runner %s has no name", c.ID)
			}
			// A surviving runner may already have a job whose start event was acknowledged.
			s.runners.addBusy(name, c.ID)
			go s.watchRunner(context.WithoutCancel(ctx), name, c.ID)
			s.logger.Info("recovered active runner", "runner", name, "containerID", c.ID)
			continue
		}
		s.logger.Warn("removing stale runner from prior controller lifetime", "runner", name)
		if err := s.logAndRemove(ctx, name, c.ID, false); err != nil {
			return err
		}
		if name != "" {
			s.runners.addExited(name, c.ID)
			time.AfterFunc(10*time.Minute, func() { s.runners.forgetExited(name, c.ID) })
		}
	}
	return nil
}

func (s *Scaler) logAndRemove(ctx context.Context, name, id string, force bool) error {
	logs, err := s.dockerClient.ContainerLogs(ctx, id, container.LogsOptions{ShowStdout: true, ShowStderr: true, Timestamps: true, Tail: "2000"})
	if err == nil {
		_, _ = fmt.Fprintf(os.Stdout, "--- runner log: %s ---\n", name)
		_, _ = io.Copy(os.Stdout, logs)
		_ = logs.Close()
	} else {
		s.logger.Warn("could not collect runner logs", "runner", name, "error", err)
	}
	if err := s.dockerClient.ContainerRemove(ctx, id, container.RemoveOptions{Force: force, RemoveVolumes: true}); err != nil && !errdefs.IsNotFound(err) {
		return fmt.Errorf("remove runner %s: %w", name, err)
	}
	return nil
}

func (s *Scaler) shutdown(ctx context.Context) {
	defer s.writeStatus()
	for name, id := range s.runners.drain() {
		if err := s.logAndRemove(ctx, name, id, true); err != nil {
			s.logger.Error("runner shutdown failed", slog.String("runner", name), slog.String("error", err.Error()))
		}
	}
}

var _ listener.Scaler = (*Scaler)(nil)
