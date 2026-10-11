package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
)

func TestDockerSocketProxyDefiniteDialFailureDoesNotLeaveMarker(t *testing.T) {
	for _, failure := range []string{"missing socket", "refused socket"} {
		t.Run(failure, func(t *testing.T) {
			directory, err := os.MkdirTemp("", "ci-fleet-dispatch-test-")
			if err != nil {
				t.Fatal(err)
			}
			defer os.RemoveAll(directory)
			upstream := filepath.Join(directory, "daemon.sock")
			if failure == "refused socket" {
				listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: upstream, Net: "unix"})
				if err != nil {
					t.Fatal(err)
				}
				listener.SetUnlinkOnClose(false)
				if err := listener.Close(); err != nil {
					t.Fatal(err)
				}
			}
			handler, transport := newDockerSocketProxy(upstream, filepath.Join(directory, "maintenance.lock"), proxyTestBoot)
			defer transport.CloseIdleConnections()
			frontend := httptest.NewServer(handler)
			defer frontend.Close()
			post := func() int {
				t.Helper()
				response, err := frontend.Client().Post(frontend.URL+"/containers/create", "application/json", strings.NewReader(`{}`))
				if err != nil {
					t.Fatal(err)
				}
				io.Copy(io.Discard, response.Body)
				response.Body.Close()
				return response.StatusCode
			}
			if got := post(); got != http.StatusServiceUnavailable {
				t.Fatalf("unavailable daemon status = %d", got)
			}
			fixture := &socketProxyTest{directory: directory}
			if count := proxyMarkerCount(t, fixture); count != 0 {
				t.Errorf("definite pre-dispatch failure left %d markers and permanently blocked cleanup", count)
			}
			if err := os.Remove(upstream); err != nil && !os.IsNotExist(err) {
				t.Fatal(err)
			}
			listener, err := net.Listen("unix", upstream)
			if err != nil {
				t.Fatal(err)
			}
			backend := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
				io.Copy(io.Discard, request.Body)
				io.WriteString(w, `{"Id":"recovered"}`)
			})}
			go backend.Serve(listener)
			defer backend.Close()
			if got := post(); got != http.StatusOK {
				t.Fatalf("recovered daemon status = %d", got)
			}
			if count := proxyMarkerCount(t, fixture); count != 0 {
				t.Errorf("daemon recovery retained %d markers from requests it never received", count)
			}
		})
	}
}

func TestDockerSocketProxyDialFailurePreservesDispatchedMarker(t *testing.T) {
	directory, err := os.MkdirTemp("", "ci-fleet-dispatch-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	upstream := filepath.Join(directory, "daemon.sock")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: upstream, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	listener.SetUnlinkOnClose(false)
	backend := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		io.Copy(io.Discard, request.Body)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		connection.Close()
	})}
	go backend.Serve(listener)
	defer backend.Close()
	handler, transport := newDockerSocketProxy(upstream, filepath.Join(directory, "maintenance.lock"), proxyTestBoot)
	defer transport.CloseIdleConnections()
	frontend := httptest.NewServer(handler)
	defer frontend.Close()
	post := func() int {
		t.Helper()
		response, err := frontend.Client().Post(frontend.URL+"/containers/create", "application/json", strings.NewReader(`{}`))
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		return response.StatusCode
	}
	fixture := &socketProxyTest{directory: directory}
	if got := post(); got != http.StatusServiceUnavailable || proxyMarkerCount(t, fixture) != 1 {
		t.Fatalf("uncertain dispatched operation lost its marker: status=%d markers=%d", got, proxyMarkerCount(t, fixture))
	}
	if err := backend.Close(); err != nil {
		t.Fatal(err)
	}
	if got := post(); got != http.StatusServiceUnavailable {
		t.Fatalf("refused daemon status = %d", got)
	}
	if count := proxyMarkerCount(t, fixture); count != 1 {
		t.Errorf("definite dial failure should preserve only the earlier dispatched operation, got %d markers", count)
	}
}

func TestDockerSocketProxyRetryDialFailureRetainsAdmittedMarker(t *testing.T) {
	directory, err := os.MkdirTemp("", "ci-fleet-dispatch-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	upstream := filepath.Join(directory, "daemon.sock")
	listener, err := net.Listen("unix", upstream)
	if err != nil {
		t.Fatal(err)
	}
	var admitted atomic.Int32
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/_ping" {
			io.WriteString(w, "OK")
			return
		}
		io.Copy(io.Discard, request.Body)
		admitted.Add(1)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		connection.Close()
	})}
	go server.Serve(listener)
	defer server.Close()
	var dials atomic.Int32
	backend := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		if dials.Add(1) > 1 {
			return nil, syscall.ENOENT
		}
		return (&net.Dialer{}).DialContext(ctx, "unix", upstream)
	}}
	defer backend.CloseIdleConnections()
	ping, _ := http.NewRequest(http.MethodGet, "http://docker/_ping", nil)
	response, err := backend.RoundTrip(ping)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	proxy := &dockerProxyTransport{backend: backend, lockPath: filepath.Join(directory, "maintenance.lock"), bootID: proxyTestBoot}
	request, _ := http.NewRequest(http.MethodPost, "http://docker/containers/create", strings.NewReader(`{}`))
	request.Header.Set("Idempotency-Key", "test-retry")
	if _, err := proxy.RoundTrip(request); !errors.Is(err, syscall.ENOENT) {
		t.Fatalf("expected failed retry dial after an admitted request, got %v", err)
	}
	if dials.Load() != 2 || admitted.Load() != 1 {
		t.Fatalf("retry fixture did not first dispatch: dials=%d admitted=%d", dials.Load(), admitted.Load())
	}
	if count := proxyMarkerCount(t, &socketProxyTest{directory: directory}); count != 1 {
		t.Fatalf("retry's failed dial erased earlier admitted work: markers=%d", count)
	}
}
