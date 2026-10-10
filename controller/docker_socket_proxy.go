package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
)

const dockerProxySocket = "/run/ci-fleet/locks/docker.sock"
const dockerProxyHealthPath = "/_ci_fleet/docker-socket-proxy"
const dockerProxyHealth = "ci-fleet-docker-socket-proxy-v1\n"

var dockerAPIVersion = regexp.MustCompile(`^/v[0-9]+\.[0-9]+/`)

func dockerReferenceMutation(method, path string) bool {
	if method != http.MethodPost {
		return false
	}
	path = dockerAPIVersion.ReplaceAllString(path, "/")
	if path == "/containers/create" || path == "/build" || path == "/swarm/init" || path == "/swarm/join" {
		return true
	}
	parts := strings.Split(strings.Trim(path, "/"), "/")
	action := parts[len(parts)-1]
	return len(parts) >= 3 && ((parts[0] == "networks" && action == "connect") ||
		(parts[0] == "containers" && (action == "start" || action == "restart" || action == "unpause")))
}

func rewriteDockerSocketBinds(body []byte) ([]byte, error) {
	var config map[string]json.RawMessage
	if err := json.Unmarshal(body, &config); err != nil {
		return nil, err
	}
	isRawSocket := func(source string) bool {
		source = filepath.Clean(source)
		return source == "/var/run/docker.sock" || source == "/run/docker.sock"
	}
	// Docker's JSON decoder accepts case-insensitive field names.
	for hostKey, raw := range config {
		if !strings.EqualFold(hostKey, "HostConfig") {
			continue
		}
		var host map[string]json.RawMessage
		if err := json.Unmarshal(raw, &host); err != nil {
			return nil, err
		}
		for key, raw := range host {
			if strings.EqualFold(key, "Binds") {
				var binds []string
				if err := json.Unmarshal(raw, &binds); err != nil {
					return nil, err
				}
				for index, bind := range binds {
					parts := strings.SplitN(bind, ":", 2)
					if len(parts) == 2 && isRawSocket(parts[0]) {
						binds[index] = hostDockerProxySocket + ":" + parts[1]
					}
				}
				host[key], _ = json.Marshal(binds)
			}
			if strings.EqualFold(key, "Mounts") {
				var mounts []map[string]json.RawMessage
				if err := json.Unmarshal(raw, &mounts); err != nil {
					return nil, err
				}
				for _, mount := range mounts {
					for sourceKey, raw := range mount {
						if !strings.EqualFold(sourceKey, "Source") {
							continue
						}
						var source string
						if err := json.Unmarshal(raw, &source); err != nil {
							return nil, err
						}
						if isRawSocket(source) {
							mount[sourceKey], _ = json.Marshal(hostDockerProxySocket)
						}
					}
				}
				host[key], _ = json.Marshal(mounts)
			}
		}
		config[hostKey], _ = json.Marshal(host)
	}
	return json.Marshal(config)
}

func dockerProxyBootID() (string, error) {
	data, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return "", err
	}
	id := strings.TrimSpace(string(data))
	parsed, err := uuid.Parse(id)
	if err != nil || parsed.String() != id {
		return "", fmt.Errorf("invalid host boot ID")
	}
	return id, nil
}

func trustedProxyDirectory(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode().Perm()&0o022 != 0 || info.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return fmt.Errorf("Docker proxy directory is not trusted: %s", path)
	}
	return nil
}

func syncProxyDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func proxyRemovalPending(lockPath, bootID string) (bool, error) {
	root := filepath.Join(filepath.Dir(lockPath), "removals")
	if err := trustedProxyDirectory(root); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return true, err
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return true, err
	}
	for _, entry := range entries {
		id, err := uuid.Parse(entry.Name())
		if err != nil || id.String() != entry.Name() || !entry.IsDir() {
			return true, nil
		}
		path := filepath.Join(root, entry.Name())
		if err := trustedProxyDirectory(path); err != nil {
			return true, err
		}
		if entry.Name() == bootID {
			markers, err := os.ReadDir(path)
			if err != nil || len(markers) != 0 {
				return true, err
			}
		}
	}
	return false, nil
}

func createProxyMarker(lockPath, bootID string) (string, error) {
	root := filepath.Join(filepath.Dir(lockPath), "inflight")
	directory := filepath.Join(root, bootID)
	for _, path := range []string{root, directory} {
		if err := os.Mkdir(path, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
			return "", err
		}
		if err := trustedProxyDirectory(path); err != nil {
			return "", err
		}
		if err := syncProxyDirectory(filepath.Dir(path)); err != nil {
			return "", err
		}
	}
	path := filepath.Join(directory, uuid.NewString()+".json")
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_EXCL|syscall.O_WRONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return "", err
	}
	marker := os.NewFile(uintptr(fd), path)
	err = marker.Sync()
	if closeErr := marker.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = syncProxyDirectory(directory)
	}
	return path, err
}

type dockerProxyTransport struct {
	backend  *http.Transport
	lockPath string
	bootID   string
}

func (t *dockerProxyTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if !dockerReferenceMutation(request.Method, request.URL.Path) {
		return t.backend.RoundTrip(request)
	}
	lock, err := openDockerMaintenanceLock(t.lockPath)
	if err != nil {
		return nil, err
	}
	for {
		err = syscall.Flock(int(lock.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) {
			lock.Close()
			return nil, err
		}
		select {
		case <-request.Context().Done():
			lock.Close()
			return nil, request.Context().Err()
		case <-time.After(25 * time.Millisecond):
		}
	}
	if pending, err := proxyRemovalPending(t.lockPath, t.bootID); pending || err != nil {
		lock.Close()
		return nil, fmt.Errorf("Docker network removal has not completed: %v", err)
	}
	if err := request.Context().Err(); err != nil {
		lock.Close()
		return nil, err
	}
	marker, err := createProxyMarker(t.lockPath, t.bootID)
	if err != nil {
		lock.Close()
		return nil, err
	}
	// The daemon may finish registering a container after its caller disappears.
	forwardContext, cancel := context.WithCancel(context.WithoutCancel(request.Context()))
	request = request.Clone(forwardContext)
	response, err := t.backend.RoundTrip(request)
	if err != nil {
		cancel()
		lock.Close() // Keep the marker because daemon completion is uncertain.
		return nil, err
	}
	response.Body = &dockerProxyBody{ReadCloser: response.Body, lock: lock, marker: marker, cancel: cancel}
	return response, nil
}

type dockerProxyBody struct {
	io.ReadCloser
	lock     *os.File
	marker   string
	complete bool
	cancel   context.CancelFunc
}

func (body *dockerProxyBody) Read(buffer []byte) (int, error) {
	n, err := body.ReadCloser.Read(buffer)
	if err == io.EOF {
		body.complete = true
	}
	return n, err
}

func (body *dockerProxyBody) Close() error {
	defer body.cancel()
	// ReverseProxy stops copying on a downstream disconnect; finish upstream work.
	if !body.complete {
		if _, err := io.Copy(io.Discard, body.ReadCloser); err == nil {
			body.complete = true
		}
	}
	err := body.ReadCloser.Close()
	if body.complete {
		if removeErr := os.Remove(body.marker); removeErr != nil {
			err = errors.Join(err, removeErr)
		} else {
			err = errors.Join(err, syncProxyDirectory(filepath.Dir(body.marker)))
		}
	}
	return errors.Join(err, body.lock.Close())
}

func newDockerSocketProxy(upstream, lockPath, bootID string) (http.Handler, *http.Transport) {
	backend := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", upstream)
	}}
	proxy := httputil.NewSingleHostReverseProxy(&url.URL{Scheme: "http", Host: "docker"})
	proxy.Transport = &dockerProxyTransport{backend: backend, lockPath: lockPath, bootID: bootID}
	proxy.FlushInterval = -1
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodGet && request.URL.Path == dockerProxyHealthPath {
			if pending, err := proxyRemovalPending(lockPath, bootID); pending || err != nil {
				http.Error(w, "Docker network removal has not completed", http.StatusServiceUnavailable)
				return
			}
			ctx, cancel := context.WithTimeout(request.Context(), 5*time.Second)
			defer cancel()
			ping, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/_ping", nil)
			response, err := backend.RoundTrip(ping)
			if err != nil {
				http.Error(w, "Docker daemon is unavailable", http.StatusServiceUnavailable)
				return
			}
			defer response.Body.Close()
			if response.StatusCode != http.StatusOK {
				http.Error(w, "Docker daemon is unavailable", http.StatusServiceUnavailable)
				return
			}
			io.WriteString(w, dockerProxyHealth)
			return
		}
		if dockerReferenceMutation(request.Method, request.URL.Path) && request.Body != nil {
			if dockerAPIVersion.ReplaceAllString(request.URL.Path, "/") == "/build" {
				spool, err := os.CreateTemp("", "ci-fleet-docker-build-*")
				if err != nil {
					http.Error(w, err.Error(), http.StatusServiceUnavailable)
					return
				}
				defer os.Remove(spool.Name())
				defer spool.Close()
				if _, err := io.Copy(spool, request.Body); err != nil {
					http.Error(w, err.Error(), http.StatusBadRequest)
					return
				}
				length, err := spool.Seek(0, io.SeekStart)
				if err != nil {
					http.Error(w, err.Error(), http.StatusServiceUnavailable)
					return
				}
				info, err := spool.Stat()
				if err != nil || length != 0 {
					http.Error(w, "cannot rewind build context", http.StatusServiceUnavailable)
					return
				}
				request.Body = spool
				request.ContentLength = info.Size()
			} else {
				body, err := io.ReadAll(request.Body)
				if err == nil && dockerAPIVersion.ReplaceAllString(request.URL.Path, "/") == "/containers/create" {
					body, err = rewriteDockerSocketBinds(body)
				}
				if err != nil {
					http.Error(w, err.Error(), http.StatusBadRequest)
					return
				}
				request.Body = io.NopCloser(bytes.NewReader(body))
				request.ContentLength = int64(len(body))
			}
		}
		proxy.ServeHTTP(w, request)
	})
	return handler, backend
}

func serveDockerSocketProxy(ctx context.Context, upstream, socket, lockPath, bootID string) error {
	guard, err := openDockerMaintenanceLock(socket + ".lock")
	if err != nil {
		return err
	}
	defer guard.Close()
	if err := syscall.Flock(int(guard.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return fmt.Errorf("Docker socket proxy is already running: %w", err)
	}
	upstreamInfo, err := os.Stat(upstream)
	if err != nil || upstreamInfo.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("Docker upstream socket is unavailable: %v", err)
	}
	if info, err := os.Lstat(socket); err == nil {
		if info.Mode()&os.ModeSocket == 0 || info.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
			return fmt.Errorf("Docker proxy socket is not trusted")
		}
		if connection, err := net.DialTimeout("unix", socket, time.Second); err == nil {
			connection.Close()
			return fmt.Errorf("Docker proxy socket is already serving")
		}
		if err := os.Remove(socket); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	owner := upstreamInfo.Sys().(*syscall.Stat_t)
	if err := os.Chown(socket, int(owner.Uid), int(owner.Gid)); err != nil {
		return err
	}
	if err := os.Chmod(socket, upstreamInfo.Mode().Perm()); err != nil {
		return err
	}
	handler, backend := newDockerSocketProxy(upstream, lockPath, bootID)
	defer backend.CloseIdleConnections()
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second, BaseContext: func(net.Listener) context.Context { return ctx }}
	stopped := make(chan error, 1)
	go func() { stopped <- server.Serve(listener) }()
	select {
	case err := <-stopped:
		return err
	case <-ctx.Done():
		// Accepted mutations must reach a daemon response before their locks end.
		if err := server.Shutdown(context.Background()); err != nil {
			return err
		}
		if err := <-stopped; !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	}
}

func runDockerSocketProxy(ctx context.Context) error {
	bootID, err := dockerProxyBootID()
	if err != nil {
		return err
	}
	return serveDockerSocketProxy(ctx, "/var/run/docker.sock", dockerProxySocket, dockerMaintenanceLock, bootID)
}

func checkDockerSocketProxy(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", dockerProxySocket)
	}}
	defer transport.CloseIdleConnections()
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker"+dockerProxyHealthPath, nil)
	response, err := transport.RoundTrip(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 128))
	if err != nil || response.StatusCode != http.StatusOK || string(body) != dockerProxyHealth {
		return fmt.Errorf("Docker socket proxy is unavailable")
	}
	return nil
}
