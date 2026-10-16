package main

import (
	"bytes"
	"context"
	"google.golang.org/genai"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVisualReferenceReachesModelAndSurvivesSession(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, image.NewRGBA(image.Rect(0, 0, 20, 20))); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "chart.png"), encoded.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	called := false
	provider := providerFunc(func(_ context.Context, h []*genai.Content, _ string, _ []*genai.Tool, emit func(string)) (*genai.Content, error) {
		called = true
		parts := h[len(h)-1].Parts
		if len(parts) < 3 || parts[len(parts)-1].InlineData == nil || !bytes.Equal(parts[len(parts)-1].InlineData.Data, encoded.Bytes()) {
			t.Fatal("image missing from model request")
		}
		emit("Observed chart; estimates require verification.")
		return genai.NewContentFromText("Observed chart; estimates require verification.", genai.RoleModel), nil
	})
	hooks := agentHooks{approve: s.approve, emit: func(string, map[string]any) {}}
	contents, _, err := nativeTurn(context.Background(), provider, s.scope, config{"document_vision": true}, nil, "Analyze @file:chart.png", hooks)
	if err != nil || !called {
		t.Fatal(err)
	}
	s.contents = contents
	if err := s.persist(); err != nil {
		t.Fatal(err)
	}
	snap, err := loadSession(s.opts.root, s.snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	restored, err := sessionContents(snap)
	if err != nil || restored[0].Parts[2].InlineData == nil {
		t.Fatal("visual evidence lost", err)
	}
	if _, err := resolveReferences(context.Background(), s.scope, "@file:chart.png", hooks); err == nil || !strings.Contains(err.Error(), "document_vision") {
		t.Fatal("image transmitted without opt-in")
	}
	var parts []*genai.Part
	if err := appendDocumentImage(&parts, []byte("not image"), "bad"); err == nil {
		t.Fatal("invalid image accepted")
	}
}
