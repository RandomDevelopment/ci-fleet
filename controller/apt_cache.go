package main

import (
	"archive/tar"
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/url"
	"regexp"
	"strings"

	"github.com/docker/docker/api/types/container"
)

const runnerAPTConfig = "/etc/apt/apt.conf.d/99-ci-fleet-cache"

var aptProxyOrigin = regexp.MustCompile(`^http://[A-Za-z0-9.:\[\]-]+/?$`)
var aptHostLabel = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`)

func validAPTHost(host string) bool {
	if net.ParseIP(host) != nil {
		return true
	}
	if len(host) > 253 {
		return false
	}
	for _, label := range strings.Split(strings.TrimSuffix(host, "."), ".") {
		if !aptHostLabel.MatchString(label) {
			return false
		}
	}
	return true
}

func aptCacheConfig(proxy, noProxy string) (string, error) {
	if proxy == "" {
		if noProxy != "" {
			return "", fmt.Errorf("CI_FLEET_NO_PROXY requires CI_FLEET_APT_HTTP_PROXY")
		}
		return "", nil
	}
	invalid := fmt.Errorf("CI_FLEET_APT_HTTP_PROXY must be a credential-free HTTP origin URL")
	parsed, err := url.Parse(proxy)
	if err != nil || len(proxy) > 2048 || !aptProxyOrigin.MatchString(proxy) {
		return "", invalid
	}
	if !validAPTHost(parsed.Hostname()) || parsed.User != nil || strings.HasSuffix(parsed.Host, ":") {
		return "", invalid
	}
	if strings.HasPrefix(parsed.Host, "[") && net.ParseIP(parsed.Hostname()) == nil {
		return "", invalid
	}
	if port := parsed.Port(); port != "" {
		var number int
		if _, err := fmt.Sscanf(port, "%d", &number); err != nil || number < 1 || number > 65535 {
			return "", invalid
		}
	}
	hosts := []string{}
	if noProxy != "" {
		hosts = strings.Split(noProxy, ",")
	}
	if len(hosts) > 64 {
		return "", fmt.Errorf("CI_FLEET_NO_PROXY must contain at most 64 comma-separated DNS names or IPv4 addresses")
	}
	for _, host := range hosts {
		if !validAPTHost(host) || strings.Contains(host, ":") {
			return "", fmt.Errorf("CI_FLEET_NO_PROXY must contain at most 64 comma-separated DNS names or IPv4 addresses")
		}
	}
	content := fmt.Sprintf("Acquire::http::Proxy %q;\nAcquire::https::Proxy \"DIRECT\";\nAcquire::http::Timeout \"10\";\nAcquire::Retries \"0\";\nAPT::Update::Error-Mode \"any\";\n", proxy)
	for _, host := range hosts {
		content += fmt.Sprintf("Acquire::http::Proxy::%s \"DIRECT\";\n", host)
	}
	return content, nil
}

func (s *Scaler) configureRunnerAPT(ctx context.Context, id string) error {
	content, err := aptCacheConfig(s.config.APTHTTPProxy, s.config.NoProxy)
	if err != nil || content == "" {
		return err
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	if err := writer.WriteHeader(&tar.Header{Name: "99-ci-fleet-cache", Mode: 0644, Size: int64(len(content))}); err != nil {
		return fmt.Errorf("prepare runner APT cache configuration")
	}
	if _, err := writer.Write([]byte(content)); err != nil {
		return fmt.Errorf("prepare runner APT cache configuration")
	}
	if err := writer.Close(); err != nil {
		return fmt.Errorf("prepare runner APT cache configuration")
	}
	if err := s.dockerClient.CopyToContainer(ctx, id, "/etc/apt/apt.conf.d", &archive, container.CopyToContainerOptions{}); err != nil {
		return fmt.Errorf("copy runner APT cache configuration")
	}
	return nil
}

func (s *Scaler) copyRunnerLogs(destination io.Writer, source io.Reader) error {
	if s.config.APTHTTPProxy == "" {
		_, err := io.Copy(destination, source)
		return err
	}
	parsed, err := url.Parse(s.config.APTHTTPProxy)
	if err != nil {
		return fmt.Errorf("runner log cache redaction unavailable")
	}
	replacements := []string{s.config.APTHTTPProxy, "[APT cache]", parsed.Host, "[APT cache]", parsed.Hostname(), "[APT cache]"}
	if s.config.NoProxy != "" {
		for _, host := range strings.Split(s.config.NoProxy, ",") {
			replacements = append(replacements, host, "[APT bypass]")
		}
	}
	replacer := strings.NewReplacer(replacements...)
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		if _, err := fmt.Fprintln(destination, replacer.Replace(scanner.Text())); err != nil {
			return err
		}
	}
	return scanner.Err()
}
