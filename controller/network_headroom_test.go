package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

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
