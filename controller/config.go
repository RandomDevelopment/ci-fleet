// Portions of this file follow the actions/scaleset Docker example.
// See NOTICE and THIRD_PARTY_NOTICES.md for attribution.
package main

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/actions/scaleset"
)

type Config struct {
	RegistrationURL string
	ScaleSetName    string
	RunnerGroup     string
	RunnerImage     string
	FleetInstance   string
	GitHubApp       scaleset.GitHubAppAuth
	MaxRunners      int
	MinRunners      int
	RunnerCPUs      int64
	RunnerMemory    int64
	DockerGID       string
	RunnerTTL       time.Duration
}

func configFromEnv() (Config, error) {
	privateKeyPath := getenv("CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE", "/run/secrets/github_app_private_key")
	privateKey, err := os.ReadFile(privateKeyPath)
	if err != nil {
		return Config{}, fmt.Errorf("read GitHub App private key file: %w", err)
	}

	installationID, err := envInt64("CI_FLEET_GITHUB_APP_INSTALLATION_ID", 0)
	if err != nil {
		return Config{}, err
	}
	minRunners, err := envInt("CI_FLEET_MIN_RUNNERS", 0)
	if err != nil {
		return Config{}, err
	}
	maxRunners, err := envInt("CI_FLEET_MAX_RUNNERS", 1)
	if err != nil {
		return Config{}, err
	}
	runnerCPUs, err := envInt64("CI_FLEET_RUNNER_CPUS", 4)
	if err != nil {
		return Config{}, err
	}
	runnerMemoryMiB, err := envInt64("CI_FLEET_RUNNER_MEMORY_MIB", 8192)
	if err != nil {
		return Config{}, err
	}
	runnerTTL, err := time.ParseDuration(getenv("CI_FLEET_RUNNER_TTL", "6h"))
	if err != nil {
		return Config{}, fmt.Errorf("CI_FLEET_RUNNER_TTL: %w", err)
	}

	cfg := Config{
		RegistrationURL: os.Getenv("CI_FLEET_GITHUB_URL"),
		ScaleSetName:    getenv("CI_FLEET_SCALE_SET_NAME", "docker-ci-experimental"),
		RunnerGroup:     getenv("CI_FLEET_RUNNER_GROUP", scaleset.DefaultRunnerGroup),
		RunnerImage:     os.Getenv("CI_FLEET_RUNNER_IMAGE"),
		FleetInstance:   os.Getenv("CI_FLEET_INSTANCE"),
		GitHubApp: scaleset.GitHubAppAuth{
			ClientID:       os.Getenv("CI_FLEET_GITHUB_APP_CLIENT_ID"),
			InstallationID: installationID,
			PrivateKey:     string(privateKey),
		},
		MinRunners:   minRunners,
		MaxRunners:   maxRunners,
		RunnerCPUs:   runnerCPUs,
		RunnerMemory: runnerMemoryMiB * 1024 * 1024,
		DockerGID:    os.Getenv("CI_FLEET_DOCKER_GID"),
		RunnerTTL:    runnerTTL,
	}
	return cfg, cfg.Validate()
}

func (c Config) Validate() error {
	parsed, err := url.ParseRequestURI(c.RegistrationURL)
	if err != nil || parsed.Scheme != "https" || parsed.Host != "github.com" {
		return fmt.Errorf("CI_FLEET_GITHUB_URL must be an https://github.com organization or repository URL")
	}
	if c.ScaleSetName == "" || c.RunnerImage == "" || c.FleetInstance == "" {
		return fmt.Errorf("scale-set name, runner image, and fleet instance are required")
	}
	if err := c.GitHubApp.Validate(); err != nil {
		return fmt.Errorf("GitHub App credentials are incomplete: %w", err)
	}
	if c.MinRunners < 0 || c.MaxRunners < 1 || c.MinRunners > c.MaxRunners {
		return fmt.Errorf("runner bounds must satisfy 0 <= min <= max and max >= 1")
	}
	if c.RunnerCPUs < 1 || c.RunnerMemory < 512*1024*1024 {
		return fmt.Errorf("runner limits must be at least 1 CPU and 512 MiB")
	}
	if c.DockerGID == "" {
		return fmt.Errorf("CI_FLEET_DOCKER_GID is required")
	}
	if c.RunnerTTL < time.Hour {
		return fmt.Errorf("runner TTL must be at least one hour")
	}
	return nil
}

func getenv(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) (int, error) {
	value, err := envInt64(name, int64(fallback))
	return int(value), err
}

func envInt64(name string, fallback int64) (int64, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", name, err)
	}
	return value, nil
}

func (c Config) scaleSetClient() (*scaleset.Client, error) {
	return scaleset.NewClientWithGitHubApp(scaleset.ClientWithGitHubAppConfig{
		GitHubConfigURL: c.RegistrationURL,
		GitHubAppAuth:   c.GitHubApp,
		SystemInfo:      systemInfo(0),
	})
}

func systemInfo(scaleSetID int) scaleset.SystemInfo {
	return scaleset.SystemInfo{
		System: "ci-fleet", Subsystem: "docker-controller", Version: version,
		CommitSHA: commitSHA, ScaleSetID: scaleSetID,
	}
}
