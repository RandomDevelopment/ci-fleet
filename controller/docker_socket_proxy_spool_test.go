package main

import (
	"bytes"
	"crypto/sha256"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
)

func TestDockerBuildSpoolRetainsBytesAcrossWriteAndReadCacheWindows(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "spool-")
	if err != nil {
		t.Fatal(err)
	}
	slots := make(chan struct{}, 1)
	slots <- struct{}{}
	spool := &dockerBuildSpool{file: file, slots: slots}
	defer spool.Close()
	if err := spool.requireDisk(); err != nil {
		t.Fatal(err)
	}
	input := bytes.Repeat([]byte("exact-upload-content\n"), dockerSpoolCacheWindow/10+17)
	want := sha256.Sum256(input)
	if count, err := io.Copy(spool, bytes.NewReader(input)); err != nil || count != int64(len(input)) {
		t.Fatalf("spooling lost bytes: count=%d, error=%v", count, err)
	}
	if err := spool.rewind(); err != nil {
		t.Fatal(err)
	}
	actual := sha256.New()
	if count, err := io.Copy(actual, spool); err != nil || count != int64(len(input)) {
		t.Fatalf("forwarding lost bytes: count=%d, error=%v", count, err)
	}
	if !bytes.Equal(actual.Sum(nil), want[:]) {
		t.Fatal("cache release changed the forwarded payload")
	}
	if err := spool.Close(); err != nil {
		t.Fatal(err)
	}
	if err := spool.Close(); err != nil || len(slots) != 0 {
		t.Fatalf("closing must release exactly one upload slot: %v", err)
	}
}

func TestDockerBuildSpoolRefusesFailedCachePreparation(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "closed-spool-")
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	spool := &dockerBuildSpool{file: file}
	if err := spool.requireDisk(); err == nil {
		t.Fatal("unreadable filesystem identity was accepted")
	}
	if err := spool.rewind(); err == nil {
		t.Fatal("failed file synchronization must not reach forwarding")
	}
	if _, err := spool.Write([]byte("upload")); err == nil {
		t.Fatal("failed spool write was accepted")
	}
	if _, err := spool.Read(make([]byte, 8)); err == nil {
		t.Fatal("failed spool read was accepted")
	}
}

func TestDockerBuildSpoolRefusesMemoryBackedTemporaryFiles(t *testing.T) {
	file, err := os.CreateTemp("/dev/shm", "proxy-memory-spool-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(file.Name())
	defer file.Close()
	spool := &dockerBuildSpool{file: file}
	if err := spool.requireDisk(); err == nil {
		t.Fatal("memory-backed spooling cannot fit a full upload within the proxy memory limit")
	}
}

func TestDockerSocketProxyRefusesMemoryBackedSpoolBeforeDispatch(t *testing.T) {
	directory, err := os.MkdirTemp("/dev/shm", "proxy-spool-refusal-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	dispatched := make(chan struct{}, 1)
	fixture := newSocketProxyTest(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		dispatched <- struct{}{}
		w.WriteHeader(http.StatusOK)
	}))
	t.Setenv("TMPDIR", directory)
	response, err := fixture.client.Post("http://docker/images/load", "application/x-tar", strings.NewReader("fixture"))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil || response.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), "disk-backed") {
		t.Fatalf("memory-backed spool was not refused: %d %s, %v", response.StatusCode, body, err)
	}
	select {
	case <-dispatched:
		t.Fatal("memory-backed spool reached Docker")
	default:
	}
}
