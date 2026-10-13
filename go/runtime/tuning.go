package main

// Tuning Aria's own model on Vertex AI — steps 5 and the registration after
// it in docs/training-protocol.md, which used to be done by hand:
//
//	aria-runtime training tune submit sft/ --name aria-v1 --bucket gs://bucket/aria-sft
//	aria-runtime training tune status sft/tuning-aria-v1.json
//	aria-runtime training tune wait   sft/tuning-aria-v1.json
//
// submit uploads the dataset the builder wrote (train.jsonl, validation.jsonl)
// to Cloud Storage under its content hash — never overwriting — and creates a
// supervised tuning job; the record it writes next to the dataset names the
// job, the base model, and the sha256 of every file the model saw, so a tuned
// model can be traced to exactly what it was trained on. wait polls until the
// job ends and registers the tuned endpoint in tuned_models under the run's
// name. It does not make it the default model: the protocol compares it with
// the base model on the validation set before anything ships.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"google.golang.org/genai"
)

var (
	tuningRunName = regexp.MustCompile(`^[a-z][a-z0-9-]{0,39}$`)
	tuningRegion  = regexp.MustCompile(`^[a-z]+(?:-[a-z]+[0-9]+)?$`)
	gcsLocation   = regexp.MustCompile(`^gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])(/[^\x00-\x1f]*)?$`)
)

// tuningRecord is what submit writes beside the dataset and status updates.
type tuningRecord struct {
	Name           string         `json:"name"`
	Job            string         `json:"job"`
	BaseModel      string         `json:"base_model"`
	Project        string         `json:"project"`
	Region         string         `json:"region"`
	Training       datasetFile    `json:"training"`
	Validation     *datasetFile   `json:"validation"`
	Manifest       string         `json:"manifest_sha256"`
	Examples       map[string]any `json:"examples"`
	Hyperparameter map[string]any `json:"hyperparameters"`
	Submitted      string         `json:"submitted"`
	State          string         `json:"state"`
	Endpoint       string         `json:"endpoint,omitempty"`
	Error          string         `json:"error,omitempty"`
}

type datasetFile struct {
	URI    string `json:"uri"`
	SHA256 string `json:"sha256"`
	Bytes  int    `json:"bytes"`
}

// tuningBackend is where datasets and jobs go: Vertex AI and Cloud Storage
// with the user's Google Cloud identity, or test servers.
type tuningBackend struct {
	client  *genai.Client
	http    *http.Client
	storage string // Cloud Storage JSON API base URL
	project string
	region  string
}

func newTuningBackend(ctx context.Context, region string) (*tuningBackend, error) {
	root, err := stateRoot()
	if err != nil {
		return nil, err
	}
	cfg, err := loadConfig(root)
	if err != nil {
		return nil, err
	}
	cfg["use_vertexai"] = true
	options, err := googleHTTPOptions(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if options.Backend != genai.BackendVertexAI || options.Project == "" {
		return nil, errors.New("tuning runs on Vertex AI: sign in with gcloud and set a Google Cloud project")
	}
	options.Location = region
	client, err := genai.NewClient(ctx, options)
	if err != nil {
		return nil, err
	}
	return &tuningBackend{client: client, http: options.HTTPClient, storage: "https://storage.googleapis.com", project: options.Project, region: region}, nil
}

// upload stores body at gs://bucket/object unless an object is already there:
// the name carries the content hash, so one that exists holds these bytes.
func (b *tuningBackend) upload(ctx context.Context, bucket, object string, body []byte) error {
	endpoint := fmt.Sprintf("%s/upload/storage/v1/b/%s/o?uploadType=media&ifGenerationMatch=0&name=%s",
		b.storage, url.PathEscape(bucket), url.QueryEscape(object))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	response, err := b.http.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	io.Copy(io.Discard, io.LimitReader(response.Body, 65536))
	switch response.StatusCode {
	case http.StatusOK, http.StatusPreconditionFailed:
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("Cloud Storage refused the upload to gs://%s (HTTP %d): the signed-in identity needs write access to the bucket", bucket, response.StatusCode)
	}
	return fmt.Errorf("Cloud Storage upload to gs://%s returned HTTP %d", bucket, response.StatusCode)
}

type tuneOptions struct {
	dir, name, bucket, base, region string
	epochs                          int
	learningRateMultiplier          float64
	adapterSize                     int
}

var adapterSizes = map[int]genai.AdapterSize{1: genai.AdapterSizeOne, 2: genai.AdapterSizeTwo, 4: genai.AdapterSizeFour,
	8: genai.AdapterSizeEight, 16: genai.AdapterSizeSixteen, 32: genai.AdapterSizeThirtyTwo}

func (o tuneOptions) validate() error {
	switch {
	case !tuningRunName.MatchString(o.name):
		return errors.New("--name must be lowercase letters, digits and hyphens, starting with a letter")
	case !gcsLocation.MatchString(o.bucket):
		return errors.New("--bucket must be gs://BUCKET or gs://BUCKET/PREFIX")
	case !tuningRegion.MatchString(o.region):
		return errors.New("--region must be a Vertex AI region such as us-central1")
	case o.epochs < 0 || o.learningRateMultiplier < 0:
		return errors.New("--epochs and --learning-rate-multiplier must not be negative")
	case o.adapterSize != 0 && adapterSizes[o.adapterSize] == "":
		return errors.New("--adapter-size must be 1, 2, 4, 8, 16 or 32")
	}
	return nil
}

func sha256Hex(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

func countLines(body []byte) int {
	return len(pySplitLines(string(body)))
}

// submitTuning uploads the dataset and creates the tuning job.
func submitTuning(ctx context.Context, b *tuningBackend, o tuneOptions) (*tuningRecord, error) {
	if err := o.validate(); err != nil {
		return nil, err
	}
	manifest, err := os.ReadFile(filepath.Join(o.dir, "manifest.json"))
	if err != nil {
		return nil, errors.New("the dataset has no manifest.json: build it with `aria-runtime training dataset` so the tuned model can be traced to what it saw")
	}
	var manifestData map[string]any
	if json.Unmarshal(manifest, &manifestData) != nil {
		return nil, errors.New("manifest.json is not valid JSON")
	}
	train, err := os.ReadFile(filepath.Join(o.dir, "train.jsonl"))
	if err != nil || countLines(train) == 0 {
		return nil, errors.New("train.jsonl is missing or empty")
	}
	validation, _ := os.ReadFile(filepath.Join(o.dir, "validation.jsonl"))
	m := gcsLocation.FindStringSubmatch(o.bucket)
	bucket, prefix := m[1], strings.Trim(m[2], "/")
	folder := strings.Trim(prefix+"/"+o.name+"/"+sha256Hex(append(append([]byte{}, train...), validation...))[:16], "/")
	record := &tuningRecord{Name: o.name, BaseModel: o.base, Project: b.project, Region: b.region,
		Manifest: sha256Hex(manifest), State: string(genai.JobStatePending), Submitted: time.Now().UTC().Format(time.RFC3339)}
	if examples, ok := manifestData["examples"].(map[string]any); ok {
		record.Examples = examples
	}
	trainObject := folder + "/train.jsonl"
	if err := b.upload(ctx, bucket, trainObject, train); err != nil {
		return nil, err
	}
	record.Training = datasetFile{URI: "gs://" + bucket + "/" + trainObject, SHA256: sha256Hex(train), Bytes: len(train)}
	config := &genai.CreateTuningJobConfig{TunedModelDisplayName: o.name, Labels: map[string]string{"aria-run": o.name}}
	if countLines(validation) > 0 {
		validationObject := folder + "/validation.jsonl"
		if err := b.upload(ctx, bucket, validationObject, validation); err != nil {
			return nil, err
		}
		record.Validation = &datasetFile{URI: "gs://" + bucket + "/" + validationObject, SHA256: sha256Hex(validation), Bytes: len(validation)}
		config.ValidationDataset = &genai.TuningValidationDataset{GCSURI: record.Validation.URI}
	}
	record.Hyperparameter = map[string]any{}
	if o.epochs > 0 {
		epochs := int32(o.epochs)
		config.EpochCount = &epochs
		record.Hyperparameter["epochs"] = o.epochs
	}
	if o.learningRateMultiplier > 0 {
		multiplier := float32(o.learningRateMultiplier)
		config.LearningRateMultiplier = &multiplier
		record.Hyperparameter["learning_rate_multiplier"] = o.learningRateMultiplier
	}
	if o.adapterSize > 0 {
		config.AdapterSize = adapterSizes[o.adapterSize]
		record.Hyperparameter["adapter_size"] = o.adapterSize
	}
	job, err := b.client.Tunings.Tune(ctx, o.base, &genai.TuningDataset{GCSURI: record.Training.URI}, config)
	if err != nil {
		return nil, fmt.Errorf("Vertex AI did not create the tuning job: %w", err)
	}
	record.Job, record.State = job.Name, string(job.State)
	return record, writeTuningRecord(o.dir, record)
}

func tuningRecordPath(dir, name string) string { return filepath.Join(dir, "tuning-"+name+".json") }

func writeTuningRecord(dir string, record *tuningRecord) error {
	return atomicJSON(tuningRecordPath(dir, record.Name), record)
}

func readTuningRecord(path string) (*tuningRecord, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var record tuningRecord
	if err := json.Unmarshal(body, &record); err != nil || record.Job == "" || record.Region == "" {
		return nil, errors.New(path + " is not a tuning record")
	}
	return &record, nil
}

func terminal(state genai.JobState) bool {
	return state == genai.JobStateSucceeded || state == genai.JobStateFailed || state == genai.JobStateCancelled
}

// refreshTuning reads the job's state into the record.
func refreshTuning(ctx context.Context, b *tuningBackend, record *tuningRecord) error {
	job, err := b.client.Tunings.Get(ctx, record.Job, nil)
	if err != nil {
		return fmt.Errorf("Vertex AI did not return the tuning job: %w", err)
	}
	record.State = string(job.State)
	if job.TunedModel != nil {
		record.Endpoint = job.TunedModel.Endpoint
	}
	if job.Error != nil {
		record.Error = job.Error.Message
	}
	return nil
}

// waitTuning polls until the job ends; a succeeded job's endpoint is
// registered in tuned_models under the run's name.
func waitTuning(ctx context.Context, b *tuningBackend, dir string, record *tuningRecord, interval time.Duration, register bool) error {
	for {
		if err := refreshTuning(ctx, b, record); err != nil {
			return err
		}
		if err := writeTuningRecord(dir, record); err != nil {
			return err
		}
		if terminal(genai.JobState(record.State)) {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
	}
	if record.State != string(genai.JobStateSucceeded) {
		return fmt.Errorf("tuning job %s ended %s: %s", record.Job, record.State, record.Error)
	}
	if !tunedEndpointName.MatchString(record.Endpoint) {
		return fmt.Errorf("tuning job %s succeeded without a usable endpoint (%q)", record.Job, record.Endpoint)
	}
	if register {
		return registerTunedModel(record.Name, record.Endpoint)
	}
	return nil
}

// registerTunedModel adds name → endpoint to tuned_models in the user's
// configuration, keeping every other setting and the current model.
func registerTunedModel(name, endpoint string) error {
	root, err := stateRoot()
	if err != nil {
		return err
	}
	cfg, err := loadConfig(root)
	if err != nil {
		return err
	}
	models, _ := asMapping(cfg["tuned_models"])
	if models == nil {
		models = map[string]any{}
	}
	models[name] = endpoint
	cfg["tuned_models"] = models
	if err := os.MkdirAll(root, 0o700); err != nil {
		return err
	}
	return atomicJSON(filepath.Join(root, "config.json"), cfg)
}

// tuneCommand is `aria-runtime training tune submit|status|wait`.
func tuneCommand(args []string, backend func(ctx context.Context, region string) (*tuningBackend, error)) (any, error) {
	if len(args) < 2 {
		return nil, errors.New("usage: aria-runtime training tune submit DATASET_DIR --name NAME --bucket gs://BUCKET[/PREFIX] | status RECORD | wait RECORD")
	}
	ctx := context.Background()
	switch args[0] {
	case "submit":
		o := tuneOptions{dir: args[1]}
		flags := flag.NewFlagSet("tune submit", flag.ContinueOnError)
		flags.StringVar(&o.name, "name", "", "short name of the tuned model, e.g. aria-v1")
		flags.StringVar(&o.bucket, "bucket", "", "gs://BUCKET[/PREFIX] for the dataset")
		flags.StringVar(&o.base, "base", "gemini-3.5-flash", "base model")
		flags.StringVar(&o.region, "region", "us-central1", "Vertex AI region of the job and the tuned endpoint")
		flags.IntVar(&o.epochs, "epochs", 0, "epochs (default: Vertex AI's)")
		flags.Float64Var(&o.learningRateMultiplier, "learning-rate-multiplier", 0, "learning rate multiplier (default: Vertex AI's)")
		flags.IntVar(&o.adapterSize, "adapter-size", 0, "adapter size 1-32 (default: Vertex AI's)")
		if err := flags.Parse(args[2:]); err != nil {
			return nil, err
		}
		if err := o.validate(); err != nil {
			return nil, err
		}
		b, err := backend(ctx, o.region)
		if err != nil {
			return nil, err
		}
		return submitTuning(ctx, b, o)
	case "status", "wait":
		record, err := readTuningRecord(args[1])
		if err != nil {
			return nil, err
		}
		flags := flag.NewFlagSet("tune "+args[0], flag.ContinueOnError)
		interval := flags.Duration("interval", time.Minute, "polling interval")
		timeout := flags.Duration("timeout", 48*time.Hour, "longest wait")
		noRegister := flags.Bool("no-register", false, "do not add the tuned endpoint to tuned_models")
		if err := flags.Parse(args[2:]); err != nil {
			return nil, err
		}
		b, err := backend(ctx, record.Region)
		if err != nil {
			return nil, err
		}
		dir := filepath.Dir(args[1])
		if args[0] == "status" {
			if err := refreshTuning(ctx, b, record); err != nil {
				return nil, err
			}
			return record, writeTuningRecord(dir, record)
		}
		ctx, cancel := context.WithTimeout(ctx, *timeout)
		defer cancel()
		return record, waitTuning(ctx, b, dir, record, *interval, !*noRegister)
	}
	return nil, errors.New("unknown tune command; use submit, status or wait")
}
