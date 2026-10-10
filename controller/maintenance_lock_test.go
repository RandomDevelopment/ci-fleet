package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/actions/scaleset"
	dockerclient "github.com/docker/docker/client"
)

func TestRunnerCreationDefersWhileCleanupOwnsMaintenanceLock(t *testing.T) {
	path := filepath.Join(t.TempDir(), "docker-maintenance.lock")
	lock, err := openDockerMaintenanceLock(path)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	scaler := &Scaler{
		runners: newRunnerState(), logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		config:              Config{MaxRunners: 1, StatusFile: filepath.Join(t.TempDir(), "status.json")},
		maintenanceLockPath: path,
	}
	if got, err := scaler.HandleDesiredRunnerCount(context.Background(), 1); err != nil || got != 0 {
		t.Fatalf("cleanup lock returned %d runners and %v, want deferred creation", got, err)
	}
}

func TestMaintenanceLockRejectsMissingAndUntrustedPaths(t *testing.T) {
	directory := t.TempDir()
	for _, name := range []string{"missing/lock", "symlink"} {
		path := filepath.Join(directory, name)
		if name == "symlink" {
			if err := os.Symlink(filepath.Join(directory, "target"), path); err != nil {
				t.Fatal(err)
			}
		}
		if lock, err := openDockerMaintenanceLock(path); err == nil {
			lock.Close()
			t.Fatalf("unsafe path %q was accepted", name)
		}
	}
}

type fixtureJitClient struct{}

func (fixtureJitClient) GenerateJitRunnerConfig(context.Context, *scaleset.RunnerScaleSetJitRunnerSetting, int) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	return &scaleset.RunnerScaleSetJitRunnerConfig{EncodedJITConfig: "fixture"}, nil
}

func TestNewRunnerUsesProxyDirectoryAndAllowsNestedSharedLock(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "docker-maintenance.lock")
	created := false
	release := make(chan struct{})
	removed := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(request.URL.Path, "/containers/create"):
			lock, err := openDockerMaintenanceLock(path)
			if err != nil {
				t.Error(err)
				http.Error(w, "lock", 500)
				return
			}
			defer lock.Close()
			if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); err != nil {
				t.Error(err)
			}
			var payload struct {
				Env        []string
				HostConfig struct{ Binds []string }
			}
			if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			if len(payload.HostConfig.Binds) != 2 || payload.HostConfig.Binds[0] != "/run/lock/ci-fleet:/run/ci-fleet/locks" || payload.HostConfig.Binds[1] != hostDockerProxySocket+":/var/run/docker.sock" {
				t.Errorf("runner Docker mounts bypass proxy: %v", payload.HostConfig.Binds)
			}
			if !strings.Contains(strings.Join(payload.Env, "\n"), "DOCKER_HOST=unix:///run/ci-fleet/locks/docker.sock") {
				t.Errorf("runner Docker endpoint bypasses proxy: %v", payload.Env)
			}
			created = true
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"Id":"runner-id"}`))
		case strings.HasSuffix(request.URL.Path, "/start"):
			w.WriteHeader(http.StatusNoContent)
		case strings.HasSuffix(request.URL.Path, "/wait"):
			<-release
			_, _ = w.Write([]byte(`{"StatusCode":0}`))
		case strings.HasSuffix(request.URL.Path, "/logs"):
			w.Header().Set("Content-Type", "application/octet-stream")
		case request.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
			close(removed)
		default:
			t.Errorf("unexpected Docker request %s", request.URL.Path)
			http.Error(w, "unexpected", 500)
		}
	}))
	defer server.Close()
	docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost("tcp://"+server.Listener.Addr().String()), dockerclient.WithVersion("1.48"))
	if err != nil {
		t.Fatal(err)
	}
	defer docker.Close()
	scaler := &Scaler{
		runners: newRunnerState(), logger: slog.New(slog.NewTextHandler(io.Discard, nil)), dockerClient: docker,
		scalesetClient: fixtureJitClient{}, maintenanceLockPath: path,
		config: Config{MaxRunners: 1, RunnerImage: "fixture", FleetInstance: "fixture", StatusFile: filepath.Join(directory, "status.json")},
	}
	if got, err := scaler.HandleDesiredRunnerCount(context.Background(), 1); err != nil || got != 1 || !created {
		t.Fatalf("runner creation returned %d and %v, created=%v", got, err, created)
	}
	close(release)
	select {
	case <-removed:
	case <-time.After(time.Second):
		t.Fatal("fixture runner watcher did not finish")
	}
}
