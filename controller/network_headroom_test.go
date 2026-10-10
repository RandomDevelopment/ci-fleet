package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	dockerclient "github.com/docker/docker/client"
)

func TestCleanupRestoresHeadroomFromEmptyUnlabeledJobNetworks(t *testing.T) {
	inventory := map[string]map[string]any{}
	addNetwork := func(id, name, subnet string) {
		inventory[id] = map[string]any{
			"Id": id, "Name": name, "Driver": "bridge", "Containers": map[string]any{},
			"Labels": map[string]string{}, "IPAM": map[string]any{"Config": []map[string]string{{"Subnet": subnet}}},
		}
	}
	for index := 0; index < 30; index++ {
		addNetwork(fmt.Sprintf("job-%02d", index), fmt.Sprintf("empty-compose-%02d", index), fmt.Sprintf("198.51.100.%d/29", index*8))
	}
	addNetwork("controller", "ci-fleet_default", "192.0.2.0/24")
	addNetwork("bridge", "bridge", "203.0.113.0/27")
	addNetwork("stopped-network", "stopped-job", "198.51.100.240/29")
	addNetwork("created-network", "created-job", "198.51.100.248/29")
	var mutex sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mutex.Lock()
		defer mutex.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/info"):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"DefaultAddressPools": []map[string]any{{"Base": "198.51.100.0/24", "Size": 29}},
			})
		case strings.HasSuffix(r.URL.Path, "/networks"):
			items := make([]map[string]any, 0, len(inventory))
			for _, item := range inventory {
				items = append(items, item)
			}
			_ = json.NewEncoder(w).Encode(items)
		case strings.HasSuffix(r.URL.Path, "/containers/json"):
			var filters map[string][]string
			if r.URL.Query().Get("all") != "true" || json.Unmarshal([]byte(r.URL.Query().Get("filters")), &filters) != nil {
				http.Error(w, "all-container filter required", http.StatusBadRequest)
				return
			}
			items := []map[string]string{}
			stopped, created := false, false
			for _, value := range filters["network"] {
				stopped = stopped || value == "stopped-network" || value == "stopped-job"
				created = created || value == "created-job"
			}
			if stopped {
				items = append(items, map[string]string{"Id": "stopped-container"})
			}
			if created {
				items = append(items, map[string]string{"Id": "created-container"})
			}
			_ = json.NewEncoder(w).Encode(items)
		case strings.Contains(r.URL.Path, "/networks/"):
			id := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
			item, exists := inventory[id]
			if !exists {
				http.Error(w, "network disappeared", http.StatusNotFound)
				return
			}
			if r.Method == http.MethodDelete {
				if len(item["Containers"].(map[string]any)) != 0 {
					http.Error(w, "network has active endpoints", http.StatusConflict)
					return
				}
				delete(inventory, id)
				_, _ = w.Write([]byte(`{}`))
				return
			}
			_ = json.NewEncoder(w).Encode(item)
		default:
			http.Error(w, "unexpected Docker API call", http.StatusNotFound)
		}
	}))
	defer server.Close()
	docker, err := dockerclient.NewClientWithOpts(
		dockerclient.WithHost("tcp://"+server.Listener.Addr().String()),
		dockerclient.WithHTTPClient(server.Client()), dockerclient.WithVersion("1.48"),
	)
	if err != nil {
		t.Fatal(err)
	}
	scaler := &Scaler{dockerClient: docker, config: Config{MaxRunners: 6, DockerNetworksPerRunner: 1, DockerNetworkReserveSubnets: 2}}
	if slots, err := scaler.availableNetworkRunnerSlots(context.Background()); err != nil || slots != 0 {
		t.Fatalf("filled pool returned %d runner slots and error %v, want zero", slots, err)
	}
	bin := t.TempDir()
	fakeDocker := `#!/usr/bin/env python3
import json, os, sys, urllib.parse, urllib.request
args = sys.argv[1:]
def request(path, method="GET"):
    with urllib.request.urlopen(urllib.request.Request(os.environ["CI_FLEET_CLEANUP_TEST_URL"] + path, method=method)) as response:
        return json.load(response)
if args[0] == "info":
    print(json.dumps(request("/info")))
elif args[0] == "ps":
    filters = {}
    for index, arg in enumerate(args):
        if arg == "--filter":
            key, value = args[index + 1].split("=", 1)
            filters.setdefault(key, []).append(value)
    if not filters or set(filters) - {"label", "network"}:
        sys.exit("unexpected container filters")
    query = urllib.parse.urlencode({"all": "true", "filters": json.dumps(filters)})
    for item in request("/containers/json?" + query):
        print(item["Id"])
elif args[:2] == ["volume", "ls"]:
    pass
elif args[:2] == ["network", "ls"]:
    for item in request("/networks"):
        print(item["Id"])
elif args[:2] == ["network", "inspect"]:
    print(json.dumps([request("/networks/" + args[-1])]))
elif args[:2] == ["network", "rm"]:
    request("/networks/" + args[-1], "DELETE")
else:
    sys.exit("unexpected fake Docker command: " + repr(args))
`
	if err := os.WriteFile(filepath.Join(bin, "docker"), []byte(fakeDocker), 0o755); err != nil {
		t.Fatal(err)
	}
	command := exec.Command("bash", "../scripts/cleanup.sh", "--apply")
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_") && !strings.HasPrefix(value, "PATH=") {
			command.Env = append(command.Env, value)
		}
	}
	command.Env = append(command.Env,
		"PATH="+bin+string(os.PathListSeparator)+os.Getenv("PATH"),
		"CI_FLEET_CLEANUP_TEST_URL="+server.URL,
		"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=1",
		"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_BASE=198.51.100.0/24",
		"CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_0_SIZE=29",
	)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("cleanup failed: %v\n%s", err, output)
	}
	if slots, err := scaler.availableNetworkRunnerSlots(context.Background()); err != nil || slots != 6 {
		t.Fatalf("cleaned pool returned %d runner slots and error %v, want 6", slots, err)
	}
	mutex.Lock()
	defer mutex.Unlock()
	if len(inventory) != 4 || inventory["controller"] == nil || inventory["bridge"] == nil || inventory["stopped-network"] == nil || inventory["created-network"] == nil {
		t.Fatalf("cleanup removed infrastructure or left leaked networks: %v", inventory)
	}
}

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
		"null inventory":       `null`,
		"null entry":           `[null]`,
		"empty entry":          `[{}]`,
		"missing IPv4 subnet":  `[{"Name":"fixture","Id":"fixture-id","Driver":"bridge","EnableIPv4":true,"IPAM":{"Config":[{}]}}]`,
		"non-canonical subnet": `[{"Name":"fixture","Id":"fixture-id","Driver":"bridge","EnableIPv4":true,"IPAM":{"Config":[{"Subnet":"10.64.0.1/28"}]}}]`,
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
