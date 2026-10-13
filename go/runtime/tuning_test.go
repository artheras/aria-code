package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"cloud.google.com/go/auth"
	"google.golang.org/genai"
)

// fakeCloud stands in for Cloud Storage and Vertex AI tuning.
type fakeCloud struct {
	mu      sync.Mutex
	objects map[string][]byte
	created map[string]any
	polls   int
	states  []string
}

func (f *fakeCloud) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case strings.HasPrefix(r.URL.Path, "/upload/storage/v1/b/"):
		bucket := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/upload/storage/v1/b/"), "/o")
		name := bucket + "/" + r.URL.Query().Get("name")
		if r.URL.Query().Get("ifGenerationMatch") != "0" {
			http.Error(w, "must not overwrite", http.StatusBadRequest)
			return
		}
		if _, exists := f.objects[name]; exists {
			w.WriteHeader(http.StatusPreconditionFailed)
			return
		}
		body, _ := io.ReadAll(r.Body)
		f.objects[name] = body
		io.WriteString(w, `{}`)
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/projects/p/locations/us-central1/tuningJobs"):
		json.NewDecoder(r.Body).Decode(&f.created)
		io.WriteString(w, `{"name":"projects/p/locations/us-central1/tuningJobs/123","state":"JOB_STATE_PENDING"}`)
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/projects/p/locations/us-central1/tuningJobs/123"):
		state := f.states[min(f.polls, len(f.states)-1)]
		f.polls++
		job := map[string]any{"name": "projects/p/locations/us-central1/tuningJobs/123", "state": state}
		if state == "JOB_STATE_SUCCEEDED" {
			job["tunedModel"] = map[string]any{"model": "projects/p/locations/us-central1/models/9@1", "endpoint": "projects/p/locations/us-central1/endpoints/456"}
		}
		if state == "JOB_STATE_FAILED" {
			job["error"] = map[string]any{"code": 3, "message": "training data is not valid"}
		}
		json.NewEncoder(w).Encode(job)
	default:
		http.Error(w, r.Method+" "+r.URL.Path, http.StatusNotFound)
	}
}

func fakeTuningBackend(t *testing.T, cloud *fakeCloud) func(context.Context, string) (*tuningBackend, error) {
	server := httptest.NewServer(cloud)
	t.Cleanup(server.Close)
	return func(ctx context.Context, region string) (*tuningBackend, error) {
		client, err := genai.NewClient(ctx, &genai.ClientConfig{Backend: genai.BackendVertexAI, Project: "p", Location: region,
			Credentials: auth.NewCredentials(&auth.CredentialsOptions{TokenProvider: staticToken{}}), HTTPOptions: genai.HTTPOptions{BaseURL: server.URL}})
		if err != nil {
			return nil, err
		}
		return &tuningBackend{client: client, http: server.Client(), storage: server.URL, project: "p", region: region}, nil
	}
}

func sftFixture(t *testing.T, validation string) string {
	t.Helper()
	dir := t.TempDir()
	files := map[string]string{"train.jsonl": "{\"contents\":[1]}\n{\"contents\":[2]}\n", "validation.jsonl": validation,
		"manifest.json": `{"examples":{"train":2,"validation":1},"rules":{"holdout_suites":["hard"]}}`}
	for name, text := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestTuningSubmitsAContentAddressedDatasetAndRecordsIt(t *testing.T) {
	cloud := &fakeCloud{objects: map[string][]byte{}}
	backend := fakeTuningBackend(t, cloud)
	dir := sftFixture(t, "{\"contents\":[3]}\n")
	value, err := tuneCommand([]string{"submit", dir, "--name", "aria-v1", "--bucket", "gs://aria-sft/runs", "--epochs", "3", "--adapter-size", "4"}, backend)
	if err != nil {
		t.Fatal(err)
	}
	record := value.(*tuningRecord)
	if record.Job != "projects/p/locations/us-central1/tuningJobs/123" || record.State != "JOB_STATE_PENDING" {
		t.Fatal(record)
	}
	train, _ := os.ReadFile(filepath.Join(dir, "train.jsonl"))
	if record.Training.SHA256 != sha256Hex(train) || !strings.HasPrefix(record.Training.URI, "gs://aria-sft/runs/aria-v1/") || record.Validation == nil {
		t.Fatal(record.Training, record.Validation)
	}
	if string(cloud.objects["aria-sft/"+strings.TrimPrefix(record.Training.URI, "gs://aria-sft/")]) != string(train) {
		t.Fatal("the uploaded training file is the dataset", cloud.objects)
	}
	spec, _ := cloud.created["supervisedTuningSpec"].(map[string]any)
	hyper, _ := spec["hyperParameters"].(map[string]any)
	if cloud.created["baseModel"] != "gemini-3.5-flash" || spec["trainingDatasetUri"] != record.Training.URI ||
		spec["validationDatasetUri"] != record.Validation.URI || fmt.Sprint(hyper["epochCount"]) != "3" || hyper["adapterSize"] != "ADAPTER_SIZE_FOUR" {
		t.Fatalf("%v", cloud.created)
	}
	if cloud.created["tunedModelDisplayName"] != "aria-v1" {
		t.Fatal(cloud.created)
	}
	saved, err := readTuningRecord(tuningRecordPath(dir, "aria-v1"))
	if err != nil || saved.Manifest == "" || saved.Examples["train"] != 2.0 {
		t.Fatal("the record traces the model to its dataset", saved, err)
	}
	// The same dataset again reuses the objects instead of overwriting them.
	if _, err := tuneCommand([]string{"submit", dir, "--name", "aria-v1", "--bucket", "gs://aria-sft/runs"}, backend); err != nil {
		t.Fatal(err)
	}
}

func TestTuningRefusesWhatCannotBeTraced(t *testing.T) {
	backend := fakeTuningBackend(t, &fakeCloud{objects: map[string][]byte{}})
	dir := sftFixture(t, "")
	os.Remove(filepath.Join(dir, "manifest.json"))
	for args, want := range map[string]string{
		"submit " + dir + " --name aria-v1 --bucket gs://aria-b":            "no manifest.json",
		"submit " + dir + " --name Aria --bucket gs://aria-b":               "--name",
		"submit " + dir + " --name aria-v1 --bucket s3://b":                 "--bucket",
		"submit " + dir + " --name a --bucket gs://aria-b --region ..":      "--region",
		"submit " + dir + " --name a --bucket gs://aria-b --adapter-size 3": "--adapter-size",
	} {
		if _, err := tuneCommand(strings.Fields(args), backend); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v", args, err)
		}
	}
}

func TestWaitRegistersTheTunedEndpointWithoutSwitchingTheModel(t *testing.T) {
	home := t.TempDir()
	t.Setenv("ARIA_HOME", home)
	if err := atomicJSON(filepath.Join(home, "config.json"), config{"model": "gemini-3.5-flash", "ui_lang": "zh"}); err != nil {
		t.Fatal(err)
	}
	cloud := &fakeCloud{objects: map[string][]byte{}, states: []string{"JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"}}
	backend := fakeTuningBackend(t, cloud)
	dir := sftFixture(t, "")
	if _, err := tuneCommand([]string{"submit", dir, "--name", "aria-v1", "--bucket", "gs://aria-b"}, backend); err != nil {
		t.Fatal(err)
	}
	value, err := tuneCommand([]string{"wait", tuningRecordPath(dir, "aria-v1"), "--interval", "1ms"}, backend)
	if err != nil {
		t.Fatal(err)
	}
	if record := value.(*tuningRecord); record.State != "JOB_STATE_SUCCEEDED" || record.Endpoint != "projects/p/locations/us-central1/endpoints/456" || cloud.polls != 2 {
		t.Fatal(record, cloud.polls)
	}
	cfg, _ := loadConfig(home)
	models, _ := asMapping(cfg["tuned_models"])
	if models["aria-v1"] != "projects/p/locations/us-central1/endpoints/456" || cfg["model"] != "gemini-3.5-flash" || cfg["ui_lang"] != "zh" {
		t.Fatal("registered under its name, the default model and other settings unchanged:", cfg)
	}
	if tuned, err := resolveTunedModel(config{"model": "aria-v1", "tuned_models": cfg["tuned_models"]}); err != nil || tuned.location != "us-central1" {
		t.Fatal(tuned, err)
	}
}

func TestAFailedTuningJobIsReportedAndNotRegistered(t *testing.T) {
	home := t.TempDir()
	t.Setenv("ARIA_HOME", home)
	cloud := &fakeCloud{objects: map[string][]byte{}, states: []string{"JOB_STATE_FAILED"}}
	backend := fakeTuningBackend(t, cloud)
	dir := sftFixture(t, "")
	tuneCommand([]string{"submit", dir, "--name", "aria-v2", "--bucket", "gs://aria-b"}, backend)
	_, err := tuneCommand([]string{"wait", tuningRecordPath(dir, "aria-v2"), "--interval", "1ms"}, backend)
	if err == nil || !strings.Contains(err.Error(), "training data is not valid") {
		t.Fatal(err)
	}
	if record, _ := readTuningRecord(tuningRecordPath(dir, "aria-v2")); record.State != "JOB_STATE_FAILED" {
		t.Fatal("the record keeps the outcome", record)
	}
	if _, err := os.Stat(filepath.Join(home, "config.json")); !os.IsNotExist(err) {
		t.Fatal("a failed job registers nothing")
	}
}
