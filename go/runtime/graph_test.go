package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func graphFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{
		"app/__init__.py":       "",
		"app/session.py":        "class Session:\n    def refresh(self):\n        pass\n\ndef open_session():\n    return Session()\n",
		"app/api.py":            "from .session import open_session\n\ndef handler():\n    return open_session()\n",
		"app/cli.py":            "from app import api\n\ndef main():\n    api.handler()\n",
		"app/other.py":          "import os\n\ndef unrelated():\n    return os.getcwd()\n",
		"tests/test_session.py": "from app.session import Session\n\ndef test_refresh():\n    Session().refresh()\n",
		"web/src/client.ts":     "import { render } from './view'\nexport function start() { render() }\n",
		"web/src/view.ts":       "export function render() {}\n",
		"web/src/view.test.ts":  "import { render } from './view'\n",
		"web/package.json":      "{}\n",
		"svc/go.mod":            "module example.com/svc\n\ngo 1.22\n",
		"svc/store/store.go":    "package store\n\ntype Store struct{}\n\nfunc (s *Store) Save() {}\n",
		"svc/main.go":           "package main\n\nimport (\n\t\"fmt\"\n\t\"example.com/svc/store\"\n)\n\nfunc main() { fmt.Println(store.Store{}) }\n",
		"docker-compose.yml":    "services:\n  api:\n    build: ./app\n  web:\n    build:\n      context: web\n",
	}
	for name, body := range files {
		path := filepath.Join(root, filepath.FromSlash(name))
		os.MkdirAll(filepath.Dir(path), 0o755)
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestGraphImportsAcrossLanguages(t *testing.T) {
	g, err := buildProjectGraph(context.Background(), graphFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"app/api.py":            "app/session.py",
		"app/cli.py":            "app/api.py",
		"tests/test_session.py": "app/session.py",
		"web/src/client.ts":     "web/src/view.ts",
		"svc/main.go":           "svc/store/store.go",
	}
	for from, to := range cases {
		if !contains(g.files[from].imports, to) {
			t.Errorf("%s should import %s, has %v", from, to, g.files[from].imports)
		}
	}
	if len(g.files["app/other.py"].imports) != 0 {
		t.Error("the standard library is not a repository file", g.files["app/other.py"].imports)
	}
	if g.services["api"] != "app" || g.services["web"] != "web" || g.services["svc"] != "svc" {
		t.Error(g.services)
	}
}

func TestImpactOfAFileAndASymbol(t *testing.T) {
	g, err := buildProjectGraph(context.Background(), graphFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	r := g.impact([]string{"session.py"})
	if !contains(r.direct, "app/api.py") || !contains(r.direct, "tests/test_session.py") || !contains(r.indirect, "app/cli.py") {
		t.Fatal(r.render(20))
	}
	if contains(r.direct, "app/other.py") || contains(r.indirect, "app/other.py") || !contains(r.tests, "tests/test_session.py") || !contains(r.services, "api") {
		t.Fatal(r.render(20))
	}
	s := g.impact([]string{"Session.refresh", "nothing_here"})
	if !contains(s.direct, "tests/test_session.py") || contains(s.direct, "app/api.py") || !contains(s.unresolved, "nothing_here") {
		t.Fatal("a symbol reaches only the files naming it", s.render(20))
	}
	if out := g.impact([]string{"web/src/view.ts"}).render(20); !strings.Contains(out, "web/src/client.ts") || !strings.Contains(out, "view.test.ts") {
		t.Fatal(out)
	}
	if out := g.impact([]string{"app/other.py"}).render(20); !strings.Contains(out, "isolated") || !strings.Contains(out, "Nothing else") {
		t.Fatal(out)
	}
}

func TestGraphCommands(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	s.scope.workspace = graphFixture(t)
	out, err := s.command(context.Background(), "t", "/graph")
	if err != nil || !strings.Contains(out, "Project graph: 11 files") || !strings.Contains(out, "api (app)") {
		t.Fatal(out, err)
	}
	out, err = s.command(context.Background(), "t", "/impact app/session.py")
	if err != nil || !strings.Contains(out, "Impact of app/session.py") || !strings.Contains(out, "app/cli.py") {
		t.Fatal(out, err)
	}
	if _, err := s.command(context.Background(), "t", "/impact"); err == nil {
		t.Fatal("empty /impact accepted")
	}
}

func TestLspListsToolsAndRunsAChecker(t *testing.T) {
	s := approvalServer(t, config{}, appOptions{})
	if out, _ := s.command(context.Background(), "t", "/lsp"); !strings.Contains(out, "gopls") {
		t.Fatal(out)
	}
	path := filepath.Join(s.scope.workspace, "notes.txt")
	os.WriteFile(path, []byte("x"), 0o644)
	if _, err := s.command(context.Background(), "t", "/lsp notes.txt"); err == nil || !strings.Contains(err.Error(), "no checker") {
		t.Fatal(err)
	}
	if _, err := s.command(context.Background(), "t", "/lsp /etc/passwd"); err == nil {
		t.Fatal("checked a file outside the roots")
	}
}
