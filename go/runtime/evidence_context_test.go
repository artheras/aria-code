package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/genai"
)

func TestPrepareContextKeepsABudgetAndDropsUnsafePaths(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("0123456789"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "b.txt"), []byte("abcdefghij"), 0o644); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "secret.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "link.txt")); err != nil {
		t.Skip(err)
	}
	packet := prepareContext(root, []string{"a.txt", "b.txt", "missing.txt", "link.txt", outside, "a.txt"}, 15)
	if len(packet.Included) != 2 || packet.Included[0].Path != "a.txt" || packet.Included[0].State != "file" || packet.Included[0].Bytes != 10 {
		t.Fatal(packet.Included)
	}
	sum := sha256.Sum256([]byte("0123456789"))
	if packet.Included[0].SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatal(packet.Included[0].SHA256)
	}
	if packet.Included[1].Path != "missing.txt" || packet.Included[1].State != "absent" {
		t.Fatal(packet.Included[1])
	}
	reasons := map[string]string{}
	for _, item := range packet.Omitted {
		reasons[item.Path] = item.Reason
	}
	if reasons["b.txt"] != "over the byte budget" || reasons["link.txt"] != "unavailable" || reasons[outside] != "outside the workspace" {
		t.Fatal(packet.Omitted)
	}
}

func TestPrepareContextStopsAtThePathCap(t *testing.T) {
	root := t.TempDir()
	var paths []string
	for i := 0; i < contextPathCap+1; i++ {
		paths = append(paths, "f"+strings.Repeat("n", i)+".txt")
	}
	packet := prepareContext(root, paths, contextBudget)
	if len(packet.Included) != contextPathCap || len(packet.Omitted) != 1 || packet.Omitted[0].Reason != "over the path cap" {
		t.Fatalf("included=%d omitted=%v", len(packet.Included), packet.Omitted)
	}
}

func TestClaimsNeedARecord(t *testing.T) {
	yes := true
	supported := DeliveryReport{Verified: &yes, Checks: []map[string]any{{"passed": true}}, Changed: []*changedFile{{Path: "app.py"}}}
	if findings := claimFindings("All tests passed.\nI updated app.py.", supported); len(findings) != 0 {
		t.Fatal(findings)
	}
	unsupported := DeliveryReport{}
	findings := claimFindings("All tests passed. I updated app.py.", unsupported)
	if len(findings) != 2 || !strings.Contains(findings[0], "检查已通过") || !strings.Contains(findings[1], "app.py") {
		t.Fatal(findings)
	}
	if findings := claimFindings("Done.", unsupported); len(findings) != 0 {
		t.Fatal(findings)
	}
	if findings := claimFindings("If the suite is green, ship.", unsupported); len(findings) != 0 {
		t.Fatal(findings)
	}
}

func TestAClosingPassClaimIsMarkedWhenNothingRan(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		emit("All tests passed.")
		return genai.NewContentFromText("All tests passed.", genai.RoleModel), nil
	})
	hooks := quietHooks()
	hooks.evidence = newTurnEvidence(root)
	_, answer, err := nativeTurn(context.Background(), provider, scope, config{"acceptance_gate": false}, nil, "Ship it", hooks)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(answer, "All tests passed.") || !strings.Contains(answer, claimNoteHeader) || !strings.Contains(answer, "检查已通过") {
		t.Fatal(answer)
	}
}

func TestAnUnchangedReadIsNotSentAgain(t *testing.T) {
	root := t.TempDir()
	scope, err := newScope(root, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "note.txt"), []byte("same bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	stage := 0
	provider := providerFunc(func(ctx context.Context, contents []*genai.Content, system string, tools []*genai.Tool, emit func(string)) (*genai.Content, error) {
		stage++
		switch stage {
		case 1:
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "note.txt"})}}, nil
		case 2:
			data := contents[len(contents)-1].Parts[0].FunctionResponse.Response["data"].(map[string]any)
			if !strings.Contains(data["content"].(string), "same bytes") {
				t.Fatal(data)
			}
			return &genai.Content{Role: "model", Parts: []*genai.Part{call("read_file", map[string]any{"path": "note.txt"})}}, nil
		default:
			data := contents[len(contents)-1].Parts[0].FunctionResponse.Response["data"].(map[string]any)
			content, _ := data["content"].(string)
			if data["unchanged"] != true || !strings.Contains(content, "unchanged since the earlier read") || strings.Contains(content, "same bytes") {
				t.Fatal(data)
			}
			emit("Read once.")
			return genai.NewContentFromText("Read once.", genai.RoleModel), nil
		}
	})
	hooks := quietHooks()
	hooks.evidence = newTurnEvidence(root)
	_, answer, err := nativeTurn(context.Background(), provider, scope, config{"acceptance_gate": false}, nil, "Read note.txt twice", hooks)
	if err != nil || answer != "Read once." {
		t.Fatal(answer, err)
	}
	packet := hooks.evidence.contextPacket()
	if len(packet.Included) != 1 || packet.Included[0].Path != "note.txt" || packet.Included[0].State != "file" {
		t.Fatal(packet)
	}
}
