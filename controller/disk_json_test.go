package main

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestDiskJSONRejectsAmbiguousMalformedAndTrailingData(t *testing.T) {
	data, err := json.Marshal(healthyDiskMeasurement())
	if err != nil {
		t.Fatal(err)
	}
	if value, err := decodeDiskMeasurement(data); err != nil || !validDiskMeasurement(value) {
		t.Fatalf("valid measurement: %v", err)
	}
	for _, text := range []string{
		"", "{", string(data) + "{}", string(data) + " trailing",
		strings.Replace(string(data), `"schema_version":1`, `"schema_version":1,"schema_version":1`, 1),
		strings.Replace(string(data), `"available_bytes":`, `"available_bytes":0,"available_bytes":`, 1),
		strings.Replace(string(data), `"available_bytes":`, `"Available_Bytes":0,"available_bytes":`, 1),
		strings.Replace(string(data), `"schema_version":1`, `"unexpected":true,"schema_version":1`, 1),
		strings.Replace(string(data), `"schema_version":1`, `"schema_version":1.5`, 1),
		strings.Repeat("[", 18) + "0" + strings.Repeat("]", 18),
	} {
		if _, err := decodeDiskMeasurement([]byte(text)); !errors.Is(err, invalidDiskJSON) {
			t.Errorf("accepted invalid JSON: %q (%v)", text, err)
		}
	}
	d := admittedDisk()
	d.read = func() (diskMeasurement, error) { return decodeDiskMeasurement([]byte("{")) }
	if d.check() || d.snapshot().Reason != "measurement_invalid" {
		t.Fatal("malformed evidence not classified invalid")
	}
}

func TestDiskJSONRejectsNullMalformedRolesAndState(t *testing.T) {
	data, err := json.Marshal(healthyDiskMeasurement())
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"roles", "state", "reason"} {
		for _, value := range []any{nil, true, 1, map[string]any{}, []any{nil}, "invalid"} {
			var raw map[string]any
			if err := json.Unmarshal(data, &raw); err != nil {
				t.Fatal(err)
			}
			fs := raw["filesystems"].([]any)[0].(map[string]any)
			fs[field] = value
			changed, err := json.Marshal(raw)
			if err != nil {
				t.Fatal(err)
			}
			d := admittedDisk()
			d.read = func() (diskMeasurement, error) { return decodeDiskMeasurement(changed) }
			if d.check() || d.snapshot().Reason != "measurement_invalid" {
				t.Errorf("accepted %s=%v", field, value)
			}
		}
	}
}

func TestDiskColdMissingEvidenceAndExactFreshnessBoundary(t *testing.T) {
	d := admittedDisk()
	d.ready = false
	d.read = func() (diskMeasurement, error) { return diskMeasurement{}, errors.New("missing") }
	if d.check() || d.snapshot().Reason != "measurement_unavailable" || d.snapshot().ObservedAt != nil {
		t.Fatal("cold missing sample admitted")
	}
	for _, age := range []int64{75, 76} {
		d.read = func() (diskMeasurement, error) {
			m := healthyDiskMeasurement()
			m.Filesystems[0].ObservedAt = diskInt(1000 - age)
			return m, nil
		}
		if got := d.check(); got != (age == 75) {
			t.Fatalf("age %d admitted=%v", age, got)
		}
	}
}
