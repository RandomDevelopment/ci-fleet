package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const dockerMaintenanceLock = "/run/ci-fleet/locks/docker-maintenance.lock"
const hostDockerProxySocket = "/run/lock/ci-fleet/docker.sock"

func openDockerMaintenanceLock(path string) (*os.File, error) {
	directory, err := os.Lstat(filepath.Dir(path))
	if err != nil {
		return nil, fmt.Errorf("Docker maintenance lock directory: %w", err)
	}
	if !directory.IsDir() || directory.Mode().Perm()&0o022 != 0 || directory.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return nil, fmt.Errorf("Docker maintenance lock directory is not trusted")
	}
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open Docker maintenance lock: %w", err)
	}
	file := os.NewFile(uintptr(fd), path)
	info, err := file.Stat()
	if err == nil && (!info.Mode().IsRegular() || info.Mode().Perm()&0o022 != 0 || info.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid())) {
		err = fmt.Errorf("Docker maintenance lock is not trusted")
	}
	if err != nil {
		file.Close()
		return nil, fmt.Errorf("Docker maintenance lock: %w", err)
	}
	return file, nil
}

func (s *Scaler) lockRunnerCreation() (*os.File, error) {
	path := s.maintenanceLockPath
	if path == "" {
		path = dockerMaintenanceLock
	}
	file, err := openDockerMaintenanceLock(path)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); err != nil {
		file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, nil
		}
		return nil, fmt.Errorf("lock Docker runner creation: %w", err)
	}
	return file, nil
}
