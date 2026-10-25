package main

import (
	"strings"
	"testing"

	"github.com/docker/docker/api/types/image"
	dockerspec "github.com/moby/docker-image-spec/specs-go/v1"
)

func TestVerifiedRunnerImage(t *testing.T) {
	reference := "sha256:" + strings.Repeat("a", 64)
	revision := strings.Repeat("b", 40)
	valid := image.InspectResponse{ID: reference, Os: "linux", Architecture: "amd64", Config: &dockerspec.DockerOCIImageConfig{}}
	valid.Config.Labels = map[string]string{"org.opencontainers.image.revision": revision}
	if err := verifiedRunnerImage(reference, valid, revision, "amd64"); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*image.InspectResponse){
		func(v *image.InspectResponse) { v.ID = "sha256:" + strings.Repeat("c", 64) },
		func(v *image.InspectResponse) { v.Os = "windows" },
		func(v *image.InspectResponse) { v.Architecture = "arm64" },
		func(v *image.InspectResponse) { v.Config = nil },
		func(v *image.InspectResponse) {
			v.Config = &dockerspec.DockerOCIImageConfig{}
			v.Config.Labels = map[string]string{"org.opencontainers.image.revision": strings.Repeat("c", 40)}
		},
	} {
		candidate := valid
		change(&candidate)
		if err := verifiedRunnerImage(reference, candidate, revision, "amd64"); err == nil {
			t.Fatal("accepted a different runtime, source or platform")
		}
	}
	if err := verifiedRunnerImage("runner:tag", valid, revision, "amd64"); err == nil {
		t.Fatal("accepted mutable tag")
	}
}
