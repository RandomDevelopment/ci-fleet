package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type zeroBuildContext struct{}

func (zeroBuildContext) Read(buffer []byte) (int, error) {
	clear(buffer)
	return len(buffer), nil
}

func TestDockerSocketProxyRejectsOversizedUploads(t *testing.T) {
	for _, route := range []string{"/v1.48/build", "/v1.48/images/load", "/v1.48/images/create?fromSrc=-&repo=fixture"} {
		t.Run(route, func(t *testing.T) {
			for _, knownLength := range []bool{true, false} {
				t.Run(map[bool]string{true: "known length", false: "streamed"}[knownLength], func(t *testing.T) {
					handler, transport := newDockerSocketProxy("/missing-daemon.sock", filepath.Join(t.TempDir(), "maintenance.lock"), proxyTestBoot)
					defer transport.CloseIdleConnections()
					request := httptest.NewRequest(http.MethodPost, "http://docker"+route, io.LimitReader(zeroBuildContext{}, 1<<30+1))
					if knownLength {
						request.ContentLength = 1<<30 + 1
					}
					response := httptest.NewRecorder()
					handler.ServeHTTP(response, request)
					if response.Code != http.StatusRequestEntityTooLarge {
						t.Fatalf("oversized context status = %d, want 413", response.Code)
					}
				})
			}
		})
	}
}

type blockedBuildContext struct {
	started chan struct{}
	finish  chan struct{}
}

func (body *blockedBuildContext) Read([]byte) (int, error) {
	close(body.started)
	<-body.finish
	return 0, io.ErrUnexpectedEOF
}

func TestDockerSocketProxyBoundsConcurrentUploads(t *testing.T) {
	handler, transport := newDockerSocketProxy("/missing-daemon.sock", filepath.Join(t.TempDir(), "maintenance.lock"), proxyTestBoot)
	defer transport.CloseIdleConnections()
	finished := make(chan struct{}, 2)
	finish := make(chan struct{})
	defer func() {
		close(finish)
		for range 2 {
			select {
			case <-finished:
			case <-time.After(2 * time.Second):
				t.Error("aborted upload did not finish")
			}
		}
	}()
	for _, route := range []string{"/build", "/images/load"} {
		body := &blockedBuildContext{started: make(chan struct{}), finish: finish}
		go func() {
			handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "http://docker"+route, body))
			finished <- struct{}{}
		}()
		select {
		case <-body.started:
		case <-time.After(2 * time.Second):
			t.Fatal("build upload did not start")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	request := httptest.NewRequest(http.MethodPost, "http://docker/images/create?fromSrc=-&repo=fixture", strings.NewReader("context")).WithContext(ctx)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusRequestTimeout || !errors.Is(ctx.Err(), context.DeadlineExceeded) {
		t.Fatalf("third upload bypassed the spool limit: status = %d", response.Code)
	}
}

func TestDockerSocketProxyReleasesSpoolBeforeBuildResponseCompletes(t *testing.T) {
	finish := make(chan struct{})
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		io.Copy(io.Discard, request.Body)
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		<-finish
		io.WriteString(w, `{"stream":"built"}`)
	}))
	defer close(finish)
	spoolDirectory := filepath.Join(fixture.directory, "spools")
	if err := os.Mkdir(spoolDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TMPDIR", spoolDirectory)
	// More builds than upload slots can run once their contexts reach Docker.
	for range 3 {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		request, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://docker/build", strings.NewReader("context"))
		response, err := fixture.client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			t.Fatalf("build failed: %s", response.Status)
		}
	}
	entries, err := os.ReadDir(spoolDirectory)
	if err != nil || len(entries) != 0 {
		t.Fatalf("uploaded contexts retained disk files: %v, %v", entries, err)
	}
	// Unlinked files still consume disk until their descriptors close.
	descriptors, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	for _, descriptor := range descriptors {
		path, _ := os.Readlink(filepath.Join("/proc/self/fd", descriptor.Name()))
		if strings.HasPrefix(path, spoolDirectory+"/") {
			t.Errorf("uploaded context retained an open descriptor: %s", path)
		}
	}
	if proxyMarkerCount(t, fixture) != 3 {
		t.Fatal("releasing upload storage also released unfinished build markers")
	}
}
