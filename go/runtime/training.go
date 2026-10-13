package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/artheras/aria-code/go/runtime/training"
)

func trainingCommand(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: aria-runtime training compare|dataset|tune ...")
		return 2
	}
	var value any
	var err error
	switch args[0] {
	case "compare":
		value, err = compareReports(args[1:])
	case "dataset":
		value, err = buildTrainingDataset(args[1:])
	case "tune":
		value, err = tuneCommand(args[1:], newTuningBackend)
	default:
		err = errors.New("unknown training command")
	}
	if err != nil {
		json.NewEncoder(os.Stderr).Encode(map[string]any{"error": err.Error()})
		return 2
	}
	if err := json.NewEncoder(os.Stdout).Encode(value); err != nil {
		return 1
	}
	return 0
}
func buildTrainingDataset(args []string) (training.Manifest, error) {
	if len(args) == 0 {
		return training.Manifest{}, errors.New("usage: aria-runtime training dataset TRAJECTORIES --suites DIR --out DIR")
	}
	options := training.DatasetOptions{Trajectories: args[0]}
	var holdout string
	flags := flag.NewFlagSet("dataset", flag.ContinueOnError)
	flags.StringVar(&options.Suites, "suites", "evals/suites", "evaluation suites")
	flags.StringVar(&options.Out, "out", "", "output directory")
	flags.Float64Var(&options.ValidationShare, "validation-share", 0.2, "validation task share")
	flags.StringVar(&holdout, "holdout", "hard", "comma-separated holdout suites")
	flags.StringVar(&options.System, "system", "", "system instruction")
	if err := flags.Parse(args[1:]); err != nil {
		return training.Manifest{}, err
	}
	if options.Out == "" || len(flags.Args()) != 0 {
		return training.Manifest{}, errors.New("dataset requires --out and valid named options")
	}
	options.Holdout = []string{}
	for _, suite := range strings.Split(holdout, ",") {
		if suite = strings.TrimSpace(suite); suite != "" {
			options.Holdout = append(options.Holdout, suite)
		}
	}
	return training.BuildDataset(options)
}
func compareReports(args []string) (training.Comparison, error) {
	baseline, candidate := []training.Report{}, []training.Report{}
	side := ""
	for _, arg := range args {
		if arg == "--baseline" || arg == "--candidate" {
			side = arg
			continue
		}
		if side == "" {
			return training.Comparison{}, errors.New("report paths require --baseline or --candidate")
		}
		var report training.Report
		if err := readJSON(arg, &report); err != nil {
			return training.Comparison{}, err
		}
		for _, row := range report.Results {
			if row.TaskID == "" || row.Outcome == "" {
				return training.Comparison{}, errors.New("evaluation reports require task_id and outcome for every attempt")
			}
		}
		if side == "--baseline" {
			baseline = append(baseline, report)
		} else {
			candidate = append(candidate, report)
		}
	}
	if len(baseline) == 0 || len(candidate) == 0 {
		return training.Comparison{}, errors.New("at least one baseline and candidate report is required")
	}
	return training.Compare(baseline, candidate), nil
}
