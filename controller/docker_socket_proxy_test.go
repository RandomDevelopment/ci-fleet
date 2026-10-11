package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

const proxyTestBoot = "11111111-1111-4111-8111-111111111111"

type socketProxyTest struct {
	directory, lockPath, socket string
	client                      *http.Client
	cancel                      context.CancelFunc
	stopped                     chan error
}

func newSocketProxyTest(t *testing.T, handler http.Handler) *socketProxyTest {
	t.Helper()
	directory, err := os.MkdirTemp("", "ci-fleet-proxy-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	upstream := filepath.Join(directory, "backend.sock")
	listener, err := net.Listen("unix", upstream)
	if err != nil {
		t.Fatal(err)
	}
	backend := &http.Server{Handler: handler}
	go backend.Serve(listener)
	t.Cleanup(func() { backend.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	fixture := &socketProxyTest{directory: directory, socket: filepath.Join(directory, "proxy.sock"),
		lockPath: filepath.Join(directory, "docker-maintenance.lock"), cancel: cancel, stopped: make(chan error, 1)}
	go func() {
		fixture.stopped <- serveDockerSocketProxy(ctx, upstream, fixture.socket, fixture.lockPath, proxyTestBoot)
	}()
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", fixture.socket)
	}}
	fixture.client = &http.Client{Transport: transport}
	t.Cleanup(func() {
		cancel()
		transport.CloseIdleConnections()
		select {
		case err := <-fixture.stopped:
			if err != nil {
				t.Errorf("proxy stopped: %v", err)
			}
		case <-time.After(3 * time.Second):
			t.Error("proxy did not shut down")
		}
	})
	deadline := time.Now().Add(2 * time.Second)
	for {
		if info, err := os.Stat(fixture.socket); err == nil && info.Mode()&os.ModeSocket != 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("proxy did not listen")
		}
		time.Sleep(time.Millisecond)
	}
	return fixture
}

func proxyMarkerCount(t *testing.T, fixture *socketProxyTest) int {
	t.Helper()
	markers, err := os.ReadDir(filepath.Join(fixture.directory, "inflight", proxyTestBoot))
	if errors.Is(err, os.ErrNotExist) {
		return 0
	}
	if err != nil {
		t.Fatal(err)
	}
	return len(markers)
}

func TestDockerSocketProxyCancelledCreateRetainsMaintenanceLock(t *testing.T) {
	admitted, finish := make(chan struct{}), make(chan struct{})
	var created atomic.Bool
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		io.Copy(io.Discard, request.Body)
		close(admitted)
		<-finish
		created.Store(true)
		io.WriteString(w, `{"Id":"created"}`)
	}))
	t.Cleanup(func() {
		select {
		case <-finish:
		default:
			close(finish)
		}
	})
	ctx, cancel := context.WithCancel(context.Background())
	request, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://docker/v1.48/containers/create", strings.NewReader(`{"Image":"example"}`))
	responseDone := make(chan error, 1)
	go func() {
		response, err := fixture.client.Do(request)
		if response != nil {
			response.Body.Close()
		}
		responseDone <- err
	}()
	select {
	case <-admitted:
	case <-time.After(2 * time.Second):
		t.Fatal("create was not admitted")
	}
	if proxyMarkerCount(t, fixture) != 1 {
		t.Fatal("mutation dispatched without a durable marker")
	}
	cancel()
	select {
	case err := <-responseDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("frontend cancellation did not complete: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("frontend cancellation blocked")
	}
	cleanupLock, err := openDockerMaintenanceLock(fixture.lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer cleanupLock.Close()
	if err := syscall.Flock(int(cleanupLock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); !errors.Is(err, syscall.EWOULDBLOCK) {
		t.Fatalf("cancelled create released its shared lock early: %v", err)
	}
	close(finish)
	deadline := time.Now().Add(2 * time.Second)
	for {
		err = syscall.Flock(int(cleanupLock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
			t.Fatalf("completed create retained its lock: %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	// Cleanup's final membership check can only run after daemon registration.
	if !created.Load() || proxyMarkerCount(t, fixture) != 0 {
		t.Fatal("cleanup could delete before the cancelled create registered its container")
	}
}

func TestDockerSocketProxyDisconnectedResponseDrainsDaemon(t *testing.T) {
	finish := make(chan struct{})
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		io.Copy(io.Discard, request.Body)
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		<-finish
		io.WriteString(w, `{"Id":"registered-after-disconnect"}`)
	}))
	t.Cleanup(func() {
		select {
		case <-finish:
		default:
			close(finish)
		}
	})
	response, err := fixture.client.Post("http://docker/containers/create", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	lock, err := openDockerMaintenanceLock(fixture.lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); !errors.Is(err, syscall.EWOULDBLOCK) {
		t.Fatalf("downstream response close released the daemon gate: %v", err)
	}
	close(finish)
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
			t.Fatalf("daemon response was not drained: %v", err)
		}
		time.Sleep(time.Millisecond)
	}
	if proxyMarkerCount(t, fixture) != 0 {
		t.Fatal("complete daemon response retained a marker")
	}
}

func TestDockerSocketProxyWaitCancellationDoesNotDispatch(t *testing.T) {
	var dispatched atomic.Int32
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { dispatched.Add(1) }))
	lock, err := openDockerMaintenanceLock(fixture.lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	request, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://docker/containers/create", strings.NewReader(`{}`))
	if response, err := fixture.client.Do(request); err == nil {
		response.Body.Close()
		t.Fatal("create bypassed the cleanup lock")
	}
	if dispatched.Load() != 0 || proxyMarkerCount(t, fixture) != 0 {
		t.Fatal("cancelled waiting request reached the daemon")
	}
}

func TestDockerSocketProxyRewritesNestedSocketMounts(t *testing.T) {
	requestBody := make(chan []byte, 1)
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		requestBody <- body
		io.WriteString(w, `{"Id":"nested"}`)
	}))
	body := `{"Image":"example","HostConfig":{"Binds":["/var/run/docker.sock:/var/run/docker.sock:ro,z","/run/docker.sock:/custom.sock","/workspace:/work:rw"],"Mounts":[{"Type":"bind","Source":"/run/docker.sock","Target":"/var/run/docker.sock","ReadOnly":true,"BindOptions":{"Propagation":"rslave"}},{"Type":"volume","Source":"cache","Target":"/cache"}],"NetworkMode":"default"},"Labels":{"example":"preserved"}}`
	response, err := fixture.client.Post("http://docker/containers/create", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("create failed: %s", response.Status)
	}
	var received struct {
		HostConfig struct {
			Binds       []string
			NetworkMode string
			Mounts      []struct {
				Type, Source, Target string
				ReadOnly             bool
				BindOptions          map[string]string
			}
		}
		Labels map[string]string
	}
	if err := json.Unmarshal(<-requestBody, &received); err != nil {
		t.Fatal(err)
	}
	wantBinds := []string{hostDockerProxySocket + ":/var/run/docker.sock:ro,z", hostDockerProxySocket + ":/custom.sock", "/workspace:/work:rw"}
	for index, want := range wantBinds {
		if received.HostConfig.Binds[index] != want {
			t.Errorf("bind %d got %q want %q", index, received.HostConfig.Binds[index], want)
		}
	}
	mount := received.HostConfig.Mounts[0]
	if mount.Source != hostDockerProxySocket || mount.Target != "/var/run/docker.sock" || !mount.ReadOnly || mount.BindOptions["Propagation"] != "rslave" || received.HostConfig.Mounts[1].Source != "cache" || received.HostConfig.NetworkMode != "default" || received.Labels["example"] != "preserved" {
		t.Fatalf("mount rewrite changed other options: %+v", received)
	}
}

func TestDockerSocketProxyCaseInsensitiveSocketFields(t *testing.T) {
	for _, body := range []string{
		`{"hostconfig":{"binds":["/var/run/docker.sock:/var/run/docker.sock:ro"]}}`,
		`{"HOSTCONFIG":{"MOUNTS":[{"type":"bind","source":"/run/docker.sock","target":"/var/run/docker.sock"}]}}`,
		`{"HostConfig":{"Binds":["/var/run/docker.sock:/first"]},"hostconfig":{"binds":["/run/docker.sock:/second"]}}`,
	} {
		rewritten, err := rewriteDockerSocketBinds([]byte(body))
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(rewritten), `"/var/run/docker.sock:`) || strings.Contains(string(rewritten), `"/run/docker.sock`) || !strings.Contains(string(rewritten), hostDockerProxySocket) {
			t.Fatalf("Docker's case-insensitive decoder bypassed mediation: %s", rewritten)
		}
	}
}

func TestDockerSocketProxyExecUpgradeDoesNotLockCleanup(t *testing.T) {
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		connection, buffered, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer connection.Close()
		buffered.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
		buffered.Flush()
		payload := make([]byte, 4)
		if _, err := io.ReadFull(buffered, payload); err != nil {
			t.Error(err)
			return
		}
		connection.Write(payload)
	}))
	connection, err := net.Dial("unix", fixture.socket)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(2 * time.Second))
	io.WriteString(connection, "POST /v1.48/exec/job/start HTTP/1.1\r\nHost: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n")
	reader := bufio.NewReader(connection)
	status, err := reader.ReadString('\n')
	if err != nil || !strings.Contains(status, "101") {
		t.Fatalf("exec upgrade failed: %q %v", status, err)
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if line == "\r\n" {
			break
		}
	}
	lock, err := openDockerMaintenanceLock(fixture.lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("exec stream starved cleanup: %v", err)
	}
	io.WriteString(connection, "ping")
	payload := make([]byte, 4)
	if _, err := io.ReadFull(reader, payload); err != nil || string(payload) != "ping" {
		t.Fatalf("exec stream was not forwarded: %q %v", payload, err)
	}
}

func TestDockerSocketProxyStreamingAndShutdown(t *testing.T) {
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/_ping" {
			io.WriteString(w, "OK")
			return
		}
		io.WriteString(w, "event\n")
		w.(http.Flusher).Flush()
		<-request.Context().Done()
	}))
	health, err := fixture.client.Get("http://docker" + dockerProxyHealthPath)
	if err != nil {
		t.Fatal(err)
	}
	healthBody, _ := io.ReadAll(health.Body)
	health.Body.Close()
	if string(healthBody) != dockerProxyHealth {
		t.Fatalf("unexpected health response: %s", healthBody)
	}
	response, err := fixture.client.Get("http://docker/v1.48/events")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	event := make([]byte, len("event\n"))
	if _, err := io.ReadFull(response.Body, event); err != nil {
		t.Fatal(err)
	}
	lock, err := openDockerMaintenanceLock(fixture.lockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("readonly stream starved cleanup: %v", err)
	}
	fixture.cancel()
	deadline := time.Now().Add(2 * time.Second)
	for {
		_, err := os.Stat(fixture.socket)
		if errors.Is(err, os.ErrNotExist) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("shutdown retained the socket listener")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestDockerSocketProxyRetainsUncertainMutationMarker(t *testing.T) {
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		io.Copy(io.Discard, request.Body)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		connection.Close()
	}))
	response, err := fixture.client.Post("http://docker/containers/create", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable || proxyMarkerCount(t, fixture) != 1 {
		t.Fatal("uncertain daemon work was marked complete")
	}
}

func TestDockerSocketProxyRefusesUnresolvedRemoval(t *testing.T) {
	for _, boot := range []string{proxyTestBoot, "unknown"} {
		t.Run(boot, func(t *testing.T) {
			var dispatched atomic.Int32
			fixture := newSocketProxyTest(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { dispatched.Add(1) }))
			directory := filepath.Join(fixture.directory, "removals", boot)
			if err := os.MkdirAll(directory, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(directory, "pending.json"), nil, 0o600); err != nil {
				t.Fatal(err)
			}
			response, err := fixture.client.Post("http://docker/networks/job/connect", "application/json", strings.NewReader(`{"Container":"new"}`))
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.StatusCode != http.StatusServiceUnavailable || dispatched.Load() != 0 {
				t.Fatal("new network reference raced an unresolved removal")
			}
			health, err := fixture.client.Get("http://docker" + dockerProxyHealthPath)
			if err != nil {
				t.Fatal(err)
			}
			health.Body.Close()
			if health.StatusCode != http.StatusServiceUnavailable || dispatched.Load() != 0 {
				t.Fatal("health probe hid an unresolved removal")
			}
		})
	}
}

func TestDockerReferenceMutationRoutes(t *testing.T) {
	for _, path := range []string{"/containers/create", "/v1.48/containers/create", "/containers/runner/start", "/containers//runner/start", "/containers/runner/restart", "/containers/runner/unpause", "/networks/job/connect", "/v1.48/build", "/swarm/init", "/v1.48/swarm/join"} {
		if !dockerReferenceMutation(http.MethodPost, path) {
			t.Errorf("reference mutation was not guarded: %s", path)
		}
	}
	for _, path := range []string{"/containers/runner/attach", "/exec/job/start", "/session", "/containers/runner/stop", "/networks/job/disconnect"} {
		if dockerReferenceMutation(http.MethodPost, path) {
			t.Errorf("non-reference operation was guarded: %s", path)
		}
	}
	if dockerReferenceMutation(http.MethodGet, "/v1.48/events") {
		t.Fatal("readonly stream was guarded")
	}
}

func TestDockerSocketProxyBuildContextIsCompleteBeforeDispatch(t *testing.T) {
	contextBody := bytes.Repeat([]byte("tar context"), 4096)
	received := make(chan []byte, 1)
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		received <- body
		io.WriteString(w, `{"stream":"built"}`)
	}))
	response, err := fixture.client.Post("http://docker/v1.48/build", "application/x-tar", bytes.NewReader(contextBody))
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if !bytes.Equal(<-received, contextBody) || proxyMarkerCount(t, fixture) != 0 {
		t.Fatal("build context was truncated or its completed mutation retained a marker")
	}
}
