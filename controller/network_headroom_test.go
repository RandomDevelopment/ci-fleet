package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	dockerclient "github.com/docker/docker/client"
)

func TestLowDockerNetworkHeadroomStopsRunnerCreation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/info"):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"DefaultAddressPools": []map[string]any{{"Base": "198.51.100.0/27", "Size": 29}},
			})
		case strings.HasSuffix(r.URL.Path, "/networks"):
			_ = json.NewEncoder(w).Encode([]map[string]any{{
				"Name": "ci-fleet_default",
				"Id":   "controller-network",
				"IPAM": map[string]any{"Config": []map[string]any{{"Subnet": "198.51.100.0/29"}}},
			}})
		default:
			t.Fatalf("runner creation reached unexpected Docker API path %s", r.URL.Path)
		}
	}))
	defer server.Close()

	docker, err := dockerclient.NewClientWithOpts(
		dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
		dockerclient.WithHTTPClient(server.Client()),
		dockerclient.WithVersion("1.48"),
	)
	if err != nil {
		t.Fatal(err)
	}

	scaler := &Scaler{
		runners:      newRunnerState(),
		dockerClient: docker,
		logger:       slog.New(slog.NewTextHandler(os.Stderr, nil)),
		config: Config{
			MaxRunners:                  3,
			StatusFile:                  t.TempDir() + "/status.json",
			DockerNetworksPerRunner:     1,
			DockerNetworkReserveSubnets: 1,
		},
	}
	scaler.runners.addIdle("existing-1", "1")
	scaler.runners.addIdle("existing-2", "2")
	got, err := scaler.HandleDesiredRunnerCount(context.Background(), 3)
	if err != nil {
		t.Fatal(err)
	}
	if got != 2 {
		t.Fatalf("runner count grew to %d without headroom for existing runners", got)
	}
}

func TestOverlappingDockerAddressPoolsAreRejected(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/info"):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"DefaultAddressPools": []map[string]any{
					{"Base": "198.51.100.0/24", "Size": 28},
					{"Base": "198.51.100.0/25", "Size": 28},
				},
			})
		case strings.HasSuffix(r.URL.Path, "/networks"):
			_ = json.NewEncoder(w).Encode([]map[string]any{})
		default:
			t.Fatalf("unexpected Docker API path %s", r.URL.Path)
		}
	}))
	defer server.Close()

	docker, err := dockerclient.NewClientWithOpts(
		dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
		dockerclient.WithHTTPClient(server.Client()),
		dockerclient.WithVersion("1.48"),
	)
	if err != nil {
		t.Fatal(err)
	}
	scaler := &Scaler{dockerClient: docker, config: Config{MaxRunners: 3, DockerNetworksPerRunner: 1}}
	if _, err := scaler.availableNetworkRunnerSlots(context.Background()); err == nil {
		t.Fatal("overlapping Docker address pools must fail closed")
	}
}

func TestMalformedDockerNetworkInventoryStopsRunnerCreation(t *testing.T) {
	cases := map[string]string{
		"null inventory":      `null`,
		"null entry":          `[null]`,
		"empty entry":         `[{}]`,
		"missing IPv4 subnet": `[{"Name":"fixture","Id":"fixture-id","Driver":"bridge","EnableIPv4":true,"IPAM":{"Config":[{}]}}]`,
	}
	for name, payload := range cases {
		t.Run(name, func(t *testing.T) {
			var unexpected atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/info"):
					_ = json.NewEncoder(w).Encode(map[string]any{
						"DefaultAddressPools": []map[string]any{{"Base": "198.51.100.0/24", "Size": 28}},
					})
				case strings.HasSuffix(r.URL.Path, "/networks"):
					_, _ = w.Write([]byte(payload))
				default:
					unexpected.Add(1)
					http.Error(w, "unexpected Docker API call", http.StatusInternalServerError)
				}
			}))
			defer server.Close()

			docker, err := dockerclient.NewClientWithOpts(
				dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
				dockerclient.WithHTTPClient(server.Client()),
				dockerclient.WithVersion("1.48"),
			)
			if err != nil {
				t.Fatal(err)
			}
			scaler := &Scaler{
				runners:      newRunnerState(),
				dockerClient: docker,
				logger:       slog.New(slog.NewTextHandler(os.Stderr, nil)),
				config: Config{
					MaxRunners:                  4,
					StatusFile:                  t.TempDir() + "/status.json",
					DockerNetworksPerRunner:     1,
					DockerNetworkReserveSubnets: 1,
				},
			}
			scaler.runners.addIdle("existing", "1")
			got, err := scaler.HandleDesiredRunnerCount(context.Background(), 4)
			if err != nil {
				t.Fatalf("malformed inventory escaped the inspection-error path: %v", err)
			}
			if got != 1 || unexpected.Load() != 0 {
				t.Fatalf("malformed inventory returned %d runners and made %d creation calls", got, unexpected.Load())
			}
		})
	}
}

func TestAddresslessAndIPv6OnlyDockerNetworksAreAccepted(t *testing.T) {
	cases := map[string]string{
		"host and none": `[{"Name":"host","Id":"host-id","Driver":"host","IPAM":{"Driver":"default","Config":[]}},{"Name":"none","Id":"none-id","Driver":"null","IPAM":{"Driver":"default","Config":[]}}]`,
		"IPv6 only":     `[{"Name":"ipv6","Id":"ipv6-id","Driver":"bridge","EnableIPv6":true,"IPAM":{"Driver":"default","Config":[{"Subnet":"2001:db8::/64"}]}}]`,
	}
	for name, payload := range cases {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/info"):
					_ = json.NewEncoder(w).Encode(map[string]any{
						"DefaultAddressPools": []map[string]any{{"Base": "198.51.100.0/24", "Size": 28}},
					})
				case strings.HasSuffix(r.URL.Path, "/networks"):
					_, _ = w.Write([]byte(payload))
				default:
					t.Fatalf("unexpected Docker API path %s", r.URL.Path)
				}
			}))
			defer server.Close()

			docker, err := dockerclient.NewClientWithOpts(
				dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
				dockerclient.WithHTTPClient(server.Client()),
				dockerclient.WithVersion("1.48"),
			)
			if err != nil {
				t.Fatal(err)
			}
			scaler := &Scaler{dockerClient: docker, config: Config{MaxRunners: 100, DockerNetworksPerRunner: 1, DockerNetworkReserveSubnets: 1}}
			slots, err := scaler.availableNetworkRunnerSlots(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			if slots != 15 {
				t.Fatalf("available runner slots = %d, want 15", slots)
			}
		})
	}
}

func TestDockerNetworkInspectionHasBoundedDeadlineAndRecovers(t *testing.T) {
	previousTimeout := dockerNetworkInspectionTimeout
	dockerNetworkInspectionTimeout = 50 * time.Millisecond
	defer func() { dockerNetworkInspectionTimeout = previousTimeout }()

	for _, stalledCall := range []string{"info", "networks"} {
		t.Run(stalledCall, func(t *testing.T) {
			var stalled atomic.Bool
			var unexpected atomic.Int32
			stalled.Store(true)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case strings.HasSuffix(r.URL.Path, "/info"):
					if stalledCall == "info" && stalled.Load() {
						<-r.Context().Done()
						return
					}
					_ = json.NewEncoder(w).Encode(map[string]any{
						"DefaultAddressPools": []map[string]any{{"Base": "198.51.100.0/24", "Size": 28}},
					})
				case strings.HasSuffix(r.URL.Path, "/networks"):
					if stalledCall == "networks" && stalled.Load() {
						<-r.Context().Done()
						return
					}
					_, _ = w.Write([]byte(`[{"Name":"host","Id":"host-id","Driver":"host","IPAM":{"Driver":"default","Config":[]}}]`))
				default:
					unexpected.Add(1)
					http.Error(w, "unexpected Docker API call", http.StatusInternalServerError)
				}
			}))
			defer server.Close()

			docker, err := dockerclient.NewClientWithOpts(
				dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
				dockerclient.WithHTTPClient(server.Client()),
				dockerclient.WithVersion("1.48"),
			)
			if err != nil {
				t.Fatal(err)
			}
			scaler := &Scaler{
				runners:      newRunnerState(),
				dockerClient: docker,
				logger:       slog.New(slog.NewTextHandler(os.Stderr, nil)),
				config: Config{
					MaxRunners:                  2,
					StatusFile:                  t.TempDir() + "/status.json",
					DockerNetworksPerRunner:     1,
					DockerNetworkReserveSubnets: 1,
				},
			}
			scaler.runners.addIdle("existing", "1")
			started := time.Now()
			got, err := scaler.HandleDesiredRunnerCount(context.WithoutCancel(context.Background()), 2)
			if err != nil {
				t.Fatal(err)
			}
			if got != 1 || unexpected.Load() != 0 {
				t.Fatalf("stalled %s inspection returned %d runners and made %d creation calls", stalledCall, got, unexpected.Load())
			}
			if elapsed := time.Since(started); elapsed > time.Second {
				t.Fatalf("stalled %s inspection took %s", stalledCall, elapsed)
			}

			stalled.Store(false)
			slots, err := scaler.availableNetworkRunnerSlots(context.WithoutCancel(context.Background()))
			if err != nil {
				t.Fatalf("healthy inspection after stalled %s call failed: %v", stalledCall, err)
			}
			if slots != 2 {
				t.Fatalf("healthy inspection after stalled %s call returned %d slots, want 2", stalledCall, slots)
			}
		})
	}
}
