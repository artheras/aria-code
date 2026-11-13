package main

import (
	"strings"
	"testing"
)

func TestValidateProposedSyntax(t *testing.T) {
	// 1. JSON validation
	validJSON := []byte(`{"status": "ok", "count": 42}`)
	if err := validateProposedSyntax("config.json", validJSON); err != nil {
		t.Errorf("expected valid JSON, got: %v", err)
	}

	invalidJSON := []byte(`{"status": "ok", count: 42}`) // missing quotes on key
	if err := validateProposedSyntax("config.json", invalidJSON); err == nil || !strings.Contains(err.Error(), "invalid JSON syntax") {
		t.Errorf("expected invalid JSON error, got: %v", err)
	}

	// 2. Go validation
	validGo := []byte(`package main

func Add(a, b int) int {
	return a + b
}
`)
	if err := validateProposedSyntax("math.go", validGo); err != nil {
		t.Errorf("expected valid Go, got: %v", err)
	}

	invalidGo := []byte(`package main

func Broken( {
	return 1
}
`)
	if err := validateProposedSyntax("broken.go", invalidGo); err == nil || !strings.Contains(err.Error(), "pre-flight syntax check failed") {
		t.Errorf("expected Go syntax error, got: %v", err)
	}

	// 3. Python delimiter validation
	validPy := []byte(`def hello(name):
    # comments with (brackets) are fine
    msg = f"hello {name}!"
    items = [1, 2, 3]
    return {"msg": msg, "items": items}
`)
	if err := validateProposedSyntax("script.py", validPy); err != nil {
		t.Errorf("expected valid Python, got: %v", err)
	}

	invalidPy := []byte(`def broken():
    items = [1, 2, 3
    return items
`)
	if err := validateProposedSyntax("script.py", invalidPy); err == nil || !strings.Contains(err.Error(), "unclosed '['") {
		t.Errorf("expected Python unclosed delimiter error, got: %v", err)
	}

	unexpectedPy := []byte(`def broken():
    return 1 }
`)
	if err := validateProposedSyntax("script.py", unexpectedPy); err == nil || !strings.Contains(err.Error(), "unexpected '}'") {
		t.Errorf("expected Python unexpected delimiter error, got: %v", err)
	}
}

func TestProposePreFlightSyntaxRejection(t *testing.T) {
	tmpDir := t.TempDir()
	sc, err := newScope(tmpDir, nil, nil, true)
	if err != nil {
		t.Fatal(err)
	}

	// Test proposing invalid JSON
	_, err = propose(sc, "write_file", map[string]any{
		"path":    "broken.json",
		"content": "{ broken json",
	})
	if err == nil || !strings.Contains(err.Error(), "invalid JSON syntax") {
		t.Fatalf("expected propose to reject invalid JSON, got: %v", err)
	}

	// Test proposing valid JSON
	change, err := propose(sc, "write_file", map[string]any{
		"path":    "valid.json",
		"content": `{"status": "ok"}`,
	})
	if err != nil {
		t.Fatalf("expected propose to accept valid JSON, got: %v", err)
	}
	if string(change.after) != `{"status": "ok"}` {
		t.Errorf("unexpected change.after: %s", string(change.after))
	}
}
