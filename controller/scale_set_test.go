package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

func TestEnsureScaleSet(t *testing.T) {
	for _, test := range []struct {
		name           string
		absent         bool
		defaultGroup   bool
		change         func(*scaleset.RunnerScaleSet)
		lookupError    bool
		updatesEnabled bool
		updateError    bool
		updateResponse func(*scaleset.RunnerScaleSet)
		wantUpdates    int
		wantError      string
	}{
		{name: "idle set left by power cut"},
		{name: "missing set", absent: true},
		{name: "default runner group", defaultGroup: true},
		{name: "different name", change: func(s *scaleset.RunnerScaleSet) { s.Name = "other" }, wantError: "identity does not match"},
		{name: "different group", change: func(s *scaleset.RunnerScaleSet) { s.RunnerGroupID++ }, wantError: "identity does not match"},
		{name: "different labels", change: func(s *scaleset.RunnerScaleSet) { s.Labels[0].Name = "other" }, wantError: "labels do not match"},
		{name: "extra label", change: func(s *scaleset.RunnerScaleSet) { s.Labels = append(s.Labels, scaleset.Label{Name: "other"}) }, wantError: "labels do not match"},
		{name: "missing labels", change: func(s *scaleset.RunnerScaleSet) { s.Labels = nil }, wantError: "labels do not match"},
		{name: "lookup failure", lookupError: true, wantError: "find runner scale set"},
		{name: "correct runner updates in place", updatesEnabled: true, wantUpdates: 1},
		{name: "update failure", updatesEnabled: true, updateError: true, wantUpdates: 1, wantError: "disable runner updates"},
		{name: "update returns a different ID", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { s.ID++ }, wantUpdates: 1, wantError: "identity changed"},
		{name: "update returns a different name", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { s.Name = "other" }, wantUpdates: 1, wantError: "identity does not match"},
		{name: "update returns a different group", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { s.RunnerGroupID++ }, wantUpdates: 1, wantError: "identity does not match"},
		{name: "update returns different labels", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { s.Labels[0].Name = "other" }, wantUpdates: 1, wantError: "labels do not match"},
		{name: "update leaves runner updates enabled", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { s.RunnerSetting.DisableUpdate = false }, wantUpdates: 1, wantError: "did not disable runner updates"},
		{name: "update returns an empty set", updatesEnabled: true, updateResponse: func(s *scaleset.RunnerScaleSet) { *s = scaleset.RunnerScaleSet{} }, wantUpdates: 1, wantError: "identity does not match"},
		{name: "name mismatch with updates enabled", updatesEnabled: true, change: func(s *scaleset.RunnerScaleSet) { s.Name = "other" }, wantError: "identity does not match"},
		{name: "group mismatch with updates enabled", updatesEnabled: true, change: func(s *scaleset.RunnerScaleSet) { s.RunnerGroupID++ }, wantError: "identity does not match"},
		{name: "label mismatch with updates enabled", updatesEnabled: true, change: func(s *scaleset.RunnerScaleSet) { s.Labels[0].Name = "other" }, wantError: "labels do not match"},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg := Config{ScaleSetName: "docker-ci-example", RunnerGroup: "trusted-private-ci", Labels: []string{"docker-ci", "linux"}}
			groupID := 7
			if test.defaultGroup {
				cfg.RunnerGroup = scaleset.DefaultRunnerGroup
				groupID = 1
			}
			existing := &scaleset.RunnerScaleSet{
				ID: 42, Name: cfg.ScaleSetName, RunnerGroupID: groupID,
				RunnerGroupName:    cfg.RunnerGroup,
				Labels:             []scaleset.Label{{Name: "linux", Type: "System"}, {Name: "docker-ci", Type: "System"}},
				RunnerSetting:      scaleset.RunnerSetting{DisableUpdate: !test.updatesEnabled},
				CreatedOn:          time.Date(2026, time.October, 1, 0, 0, 0, 0, time.UTC),
				RunnerJitConfigURL: "https://example.invalid/jit",
				Statistics:         &scaleset.RunnerScaleSetStatistic{},
			}
			if test.change != nil {
				test.change(existing)
			}
			if test.absent {
				existing = nil
			}
			var original *scaleset.RunnerScaleSet
			if existing != nil {
				copy := *existing
				copy.Labels = slices.Clone(existing.Labels)
				original = &copy
			}
			claims, _ := json.Marshal(map[string]int64{"exp": time.Now().Add(time.Hour).Unix()})
			adminToken := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`)) + "." + base64.RawURLEncoding.EncodeToString(claims) + "."
			createCalls, updateCalls, deleteCalls, sessionCalls := 0, 0, 0, 0
			var server *httptest.Server
			server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case r.URL.Path == "/api/v3/orgs/EXAMPLE-ORG/actions/runners/registration-token":
					w.WriteHeader(http.StatusCreated)
					json.NewEncoder(w).Encode(map[string]string{"token": "test-registration"})
				case r.URL.Path == "/api/v3/actions/runner-registration":
					json.NewEncoder(w).Encode(map[string]string{"url": server.URL, "token": adminToken})
				case r.URL.Path == "/_apis/runtime/runnergroups/":
					if test.defaultGroup || r.URL.Query().Get("groupName") != cfg.RunnerGroup {
						t.Errorf("unexpected group lookup: %s", r.URL)
					}
					json.NewEncoder(w).Encode(scaleset.RunnerGroupList{Count: 1, RunnerGroups: []scaleset.RunnerGroup{{ID: groupID, Name: cfg.RunnerGroup}}})
				case r.URL.Path == "/_apis/runtime/runnerscalesets" && r.Method == http.MethodGet:
					if r.URL.Query().Get("runnerGroupId") != strconv.Itoa(groupID) || r.URL.Query().Get("name") != cfg.ScaleSetName {
						t.Errorf("lookup did not use configured name and group: %s", r.URL)
					}
					if test.lookupError {
						w.WriteHeader(http.StatusForbidden)
						return
					}
					sets := []scaleset.RunnerScaleSet{}
					if existing != nil {
						sets = append(sets, *existing)
					}
					json.NewEncoder(w).Encode(map[string]any{"count": len(sets), "value": sets})
				case r.URL.Path == "/_apis/runtime/runnerscalesets" && r.Method == http.MethodPost:
					createCalls++
					if existing != nil {
						w.WriteHeader(http.StatusBadRequest)
						json.NewEncoder(w).Encode(map[string]string{"typeName": "GitHub.Actions.Runtime.WebApi.RunnerScaleSetExistsException", "message": "The runner scale set already exists"})
						return
					}
					var created scaleset.RunnerScaleSet
					if err := json.NewDecoder(r.Body).Decode(&created); err != nil {
						t.Error(err)
					}
					if !created.RunnerSetting.DisableUpdate {
						t.Error("new set must disable runner updates")
					}
					created.ID = 42
					existing = &created
					json.NewEncoder(w).Encode(created)
				case r.URL.Path == "/_apis/runtime/runnerscalesets/42" && r.Method == http.MethodPatch:
					updateCalls++
					var patch scaleset.RunnerScaleSet
					if err := json.NewDecoder(r.Body).Decode(&patch); err != nil {
						t.Error(err)
					}
					if !patch.RunnerSetting.DisableUpdate {
						t.Error("update must disable runner updates")
					}
					patch.RunnerSetting.DisableUpdate = existing.RunnerSetting.DisableUpdate
					if !reflect.DeepEqual(patch, *existing) {
						t.Errorf("update changed other scale-set fields: %+v", patch)
					}
					if test.updateError {
						w.WriteHeader(http.StatusForbidden)
						return
					}
					existing.RunnerSetting.DisableUpdate = true
					response := *existing
					response.Labels = slices.Clone(existing.Labels)
					if test.updateResponse != nil {
						test.updateResponse(&response)
					}
					json.NewEncoder(w).Encode(response)
				case r.URL.Path == "/_apis/runtime/runnerscalesets/42/sessions" && r.Method == http.MethodPost:
					sessionCalls++
					json.NewEncoder(w).Encode(scaleset.RunnerScaleSetSession{RunnerScaleSet: existing})
				case r.Method == http.MethodDelete:
					deleteCalls++
					w.WriteHeader(http.StatusNoContent)
				default:
					t.Errorf("unexpected request: %s %s", r.Method, r.URL)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			client, err := scaleset.NewClientWithPersonalAccessToken(scaleset.NewClientWithPersonalAccessTokenConfig{
				GitHubConfigURL: server.URL + "/EXAMPLE-ORG", PersonalAccessToken: "test-only",
			}, scaleset.WithRetryMax(0))
			if err != nil {
				t.Fatal(err)
			}
			ctx := context.Background()
			if test.name == "idle set left by power cut" {
				_, err := client.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{Name: cfg.ScaleSetName, RunnerGroupID: groupID, Labels: cfg.buildLabels()})
				if err == nil || !strings.Contains(err.Error(), "400") || !strings.Contains(err.Error(), "RunnerScaleSetExistsException") {
					t.Fatalf("create-only startup did not reproduce HTTP 400: %v", err)
				}
				createCalls = 0
			}
			set, err := ensureScaleSet(ctx, cfg, client)
			if test.wantError != "" {
				if err == nil || !strings.Contains(err.Error(), test.wantError) || set != nil {
					t.Fatalf("expected %q, got set=%v err=%v", test.wantError, set, err)
				}
			} else {
				if err != nil || set.ID != 42 || !set.RunnerSetting.DisableUpdate {
					t.Fatalf("startup failed: set=%v err=%v", set, err)
				}
				session, err := client.MessageSessionClient(ctx, set.ID, "example-controller")
				if err != nil {
					t.Fatalf("open listener session: %v", err)
				}
				if _, err := listener.New(session, listener.Config{ScaleSetID: set.ID, MaxRunners: 1, Logger: slog.New(slog.DiscardHandler)}); err != nil {
					t.Fatalf("initialize listener: %v", err)
				}
				if sessionCalls != 1 {
					t.Fatalf("expected one listener session, got %d", sessionCalls)
				}
			}
			wantCreates := 0
			if test.absent {
				wantCreates = 1
			}
			if createCalls != wantCreates || updateCalls != test.wantUpdates || deleteCalls != 0 {
				t.Fatalf("unexpected scale-set mutations: creates=%d updates=%d deletes=%d", createCalls, updateCalls, deleteCalls)
			}
			if original != nil && (existing.ID != original.ID || existing.Name != original.Name || existing.RunnerGroupID != original.RunnerGroupID || !slices.Equal(existing.Labels, original.Labels)) {
				t.Fatalf("setting update changed the original scale set identity or labels: %v", existing)
			}
			if original != nil && (test.wantUpdates == 0 || test.updateError) && existing.RunnerSetting != original.RunnerSetting {
				t.Fatalf("failed or skipped update changed runner settings: %+v", existing.RunnerSetting)
			}
			if test.wantError != "" && sessionCalls != 0 {
				t.Fatalf("unsafe scale set opened a listener session: %d", sessionCalls)
			}
		})
	}
}
