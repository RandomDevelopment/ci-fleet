package main

import "sync"

type runnerState struct {
	mu     sync.Mutex
	idle   map[string]string
	busy   map[string]string
	exited map[string]string
}

func newRunnerState() runnerState {
	return runnerState{idle: make(map[string]string), busy: make(map[string]string), exited: make(map[string]string)}
}

func (r *runnerState) count() int {
	current, _ := r.counts()
	return current
}

func (r *runnerState) counts() (int, int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.idle) + len(r.busy), len(r.busy)
}

func (r *runnerState) addIdle(name, id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.idle[name] = id
}

func (r *runnerState) markBusy(name string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	id, ok := r.idle[name]
	if !ok {
		return false
	}
	delete(r.idle, name)
	r.busy[name] = id
	return true
}

func (r *runnerState) markExited(name, expectedID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if id, ok := r.busy[name]; ok && id == expectedID {
		delete(r.busy, name)
		r.exited[name] = id
		return true
	}
	if id, ok := r.idle[name]; ok && id == expectedID {
		delete(r.idle, name)
		r.exited[name] = id
		return true
	}
	return false
}

func (r *runnerState) forgetExited(name, expectedID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.exited[name] == expectedID { delete(r.exited, name) }
}

func (r *runnerState) markDone(name string) (string, bool, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if id, ok := r.busy[name]; ok {
		delete(r.busy, name)
		return id, true, true
	}
	if id, ok := r.idle[name]; ok {
		delete(r.idle, name)
		return id, true, true
	}
	if id, ok := r.exited[name]; ok {
		delete(r.exited, name)
		return id, false, true
	}
	return "", false, false
}

func (r *runnerState) drain() map[string]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	all := make(map[string]string, len(r.idle)+len(r.busy)+len(r.exited))
	for name, id := range r.idle { all[name] = id }
	for name, id := range r.busy { all[name] = id }
	for name, id := range r.exited { all[name] = id }
	clear(r.idle)
	clear(r.busy)
	clear(r.exited)
	return all
}
