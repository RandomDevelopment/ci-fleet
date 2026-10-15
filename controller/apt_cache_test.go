package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	dockerclient "github.com/docker/docker/client"
)

func TestAPTCacheSharedValidationCases(t *testing.T) {
	data, err := os.ReadFile("../scripts/fixtures/apt-cache.json")
	if err != nil {
		// Image builds copy controller sources and the shared fixture into /scripts.
		data, err = os.ReadFile("/scripts/fixtures/apt-cache.json")
	}
	if err != nil {
		t.Fatal(err)
	}
	var cases struct{ Valid, Invalid [][2]string }
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, pair := range cases.Valid {
		if _, err := aptCacheConfig(pair[0], pair[1]); err != nil {
			t.Errorf("valid configuration rejected: %v", err)
		}
	}
	for _, pair := range cases.Invalid {
		if _, err := aptCacheConfig(pair[0], pair[1]); err == nil {
			t.Error("invalid configuration accepted")
		}
	}
}

func TestAPTCacheRunnerLogRedaction(t *testing.T) {
	scaler := &Scaler{config: Config{APTHTTPProxy: "http://cache.example.invalid:3142", NoProxy: "mirror.example.invalid"}}
	var output bytes.Buffer
	input := "normal output\nhttp://cache.example.invalid:3142\ncache.example.invalid:3142\ncache.example.invalid\nmirror.example.invalid\n"
	if err := scaler.copyRunnerLogs(&output, strings.NewReader(input)); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(output.String(), "example.invalid") || !strings.Contains(output.String(), "normal output") {
		t.Error("runner logs exposed cache configuration or lost ordinary output")
	}
	output.Reset()
	if err := scaler.copyRunnerLogs(&output, strings.NewReader(strings.Repeat("x", 1024*1024+1))); err == nil || output.Len() != 0 {
		t.Error("oversized logs did not stop safely")
	}
	scaler.config = Config{}
	if err := scaler.copyRunnerLogs(&output, strings.NewReader(input)); err != nil || output.String() != input {
		t.Error("disabled cache changed runner log output")
	}
}

func TestRunnerAPTConfigurationIsOptionalAndCopyFailurePreventsStart(t *testing.T) {
	for _, test := range []struct {
		name, proxy string
		failCopy    bool
	}{
		{name: "disabled"},
		{name: "enabled", proxy: "http://cache.example.invalid:3142"},
		{name: "copy failure", proxy: "http://cache.example.invalid:3142", failCopy: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var copied, started, removed bool
			release := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/containers/create"):
					var payload struct{ Env []string }
					if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
						t.Error(err)
					}
					environment := strings.Join(payload.Env, "\n")
					if strings.Contains(environment, "CI_FLEET_APT_CONFIG=") != (test.proxy != "") || strings.Contains(environment, "cache.example.invalid") {
						t.Error("runner environment did not preserve optional endpoint-free configuration")
					}
					w.WriteHeader(http.StatusCreated)
					io.WriteString(w, `{"Id":"runner-id"}`)
				case strings.HasSuffix(r.URL.Path, "/archive"):
					copied = true
					if r.Method != http.MethodPut || r.URL.Query().Get("path") != "/etc/apt/apt.conf.d" {
						t.Error("unexpected APT configuration destination")
					}
					archive := tar.NewReader(r.Body)
					header, err := archive.Next()
					if err != nil {
						t.Error(err)
						return
					}
					content, _ := io.ReadAll(archive)
					want, _ := aptCacheConfig(test.proxy, "")
					if header.Name != "99-ci-fleet-cache" || header.Mode != 0644 || header.Uid != 0 || string(content) != want {
						t.Error("incorrect APT configuration archive")
					}
					if _, err := archive.Next(); err != io.EOF {
						t.Error("unexpected extra runner configuration")
					}
					if test.failCopy {
						w.WriteHeader(http.StatusInternalServerError)
						io.WriteString(w, `{"message":"private endpoint"}`)
					}
				case strings.HasSuffix(r.URL.Path, "/start"):
					started = true
					w.WriteHeader(http.StatusNoContent)
				case strings.HasSuffix(r.URL.Path, "/wait"):
					<-release
					w.WriteHeader(http.StatusInternalServerError)
				case r.Method == http.MethodDelete:
					removed = true
					w.WriteHeader(http.StatusNoContent)
				default:
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			defer close(release)
			docker, err := dockerclient.NewClientWithOpts(dockerclient.WithHost(server.URL), dockerclient.WithVersion("1.48"))
			if err != nil {
				t.Fatal(err)
			}
			defer docker.Close()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			scaler := &Scaler{dockerClient: docker, scalesetClient: fixtureJitClient{}, runners: newRunnerState(),
				logger: slog.New(slog.DiscardHandler), config: Config{APTHTTPProxy: test.proxy}}
			_, err = scaler.startRunner(ctx)
			if (err != nil) != test.failCopy || copied != (test.proxy != "") || started == test.failCopy || removed != test.failCopy {
				t.Errorf("runner setup failed: copied=%v started=%v removed=%v error=%v", copied, started, removed, err)
			}
			if err != nil && strings.Contains(err.Error(), "private endpoint") {
				t.Error("Docker error exposed private configuration")
			}
		})
	}
}
