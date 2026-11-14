package main

import (
	"context"
	"errors"
	"testing"

	"google.golang.org/genai"
)

type dummyRoleModel struct {
	id string
}

func (d *dummyRoleModel) Generate(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
	return &genai.Content{Parts: []*genai.Part{{Text: d.id}}}, nil
}

func TestRoleRouterDefault(t *testing.T) {
	base := &dummyRoleModel{id: "base"}
	rr := newRoleRouter(base, config{})

	if p := rr.ProviderForRole(RoleCoder); p != base {
		t.Fatalf("expected base provider for coder by default, got %v", p)
	}
	if p := rr.ProviderForRole(RoleReviewer); p != base {
		t.Fatalf("expected base provider for reviewer by default, got %v", p)
	}
	if p := rr.ProviderForRole(RolePlanner); p != base {
		t.Fatalf("expected base provider for planner by default, got %v", p)
	}
}

func TestRoleRouterSpecializedRoles(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("OPENAI_API_KEY", "sk-test")
	t.Setenv("ANTHROPIC_API_KEY", "ant-key")

	base := &dummyRoleModel{id: "base"}
	cfg := config{
		"coder_provider":    "openai",
		"coder_model":       "gpt-4o",
		"reviewer_provider": "anthropic",
		"reviewer_model":    "claude-3-7-sonnet",
	}
	rr := newRoleRouter(base, cfg)

	coder := rr.ProviderForRole(RoleCoder)
	if coder == base {
		t.Fatalf("expected specialized coder provider, got base")
	}
	reviewer := rr.ProviderForRole(RoleReviewer)
	if reviewer == base {
		t.Fatalf("expected specialized reviewer provider, got base")
	}
	planner := rr.ProviderForRole(RolePlanner)
	if planner != base {
		t.Fatalf("expected base provider for unset planner, got %v", planner)
	}

	// Verify caching on subsequent calls
	if rr.ProviderForRole(RoleCoder) != coder {
		t.Fatal("expected cached coder provider")
	}
}

type failingRoleModel struct{}

func (f *failingRoleModel) Generate(_ context.Context, _ []*genai.Content, _ string, _ []*genai.Tool, _ func(string)) (*genai.Content, error) {
	return nil, errors.New("planner unavailable")
}

func TestRunPlannerPhase(t *testing.T) {
	var states []string
	emit := func(kind string, fields map[string]any) {
		if kind == "turn.status" {
			if st, ok := fields["state"].(string); ok {
				states = append(states, st)
			}
		}
	}

	plan := runPlannerPhase(context.Background(), &dummyRoleModel{id: "1. Edit agent.go\n2. Run go test"}, "Refactor agent loop", emit)
	if plan == "" {
		t.Fatal("expected non-empty plan from planner")
	}
	if len(states) != 2 || states[0] != "planning" || states[1] != "plan_ready" {
		t.Fatalf("unexpected planner status events: %v", states)
	}

	// Graceful degradation on error
	fallback := runPlannerPhase(context.Background(), &failingRoleModel{}, "Refactor agent loop", nil)
	if fallback != "" {
		t.Fatalf("expected empty plan on error, got %q", fallback)
	}
}
