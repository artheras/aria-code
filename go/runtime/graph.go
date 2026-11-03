package main

// The project graph, ported from src/aria_code/runtime/project_graph.py:
// what a change reaches before it is made. Files and the symbols they
// define; imports between files (Python, JS/TS relative specifiers, and Go
// packages through go.mod); references (a file naming a symbol defined in at
// most three files, names of four characters or more); tests; and services
// (a docker-compose build context, or a directory with its own manifest).
//
// Impact follows imports up to three hops, but not through a package
// __init__ or a file importing more than twenty others. The Python version
// parses Python with ast; this one reads imports and definitions with the
// same patterns at line starts, which a file's own syntax rarely defeats.

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"go.yaml.in/yaml/v3"
)

const (
	graphMinName      = 4
	graphMaxDefining  = 3
	graphDepth        = 3
	graphHubFanOut    = 20
	graphMaxFiles     = 20000
	graphMaxFileBytes = 1 << 20
)

var (
	graphJsImport  = regexp.MustCompile(`(?:\bimport\s[^'"]*?\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bexport\s[^'"]*?\bfrom\s*|\bimport\s+)['"](\.{1,2}/[^'"]+)['"]`)
	graphPyFrom    = regexp.MustCompile(`(?m)^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n#]+)`)
	graphPyImport  = regexp.MustCompile(`(?m)^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)`)
	graphGoImport  = regexp.MustCompile(`(?m)^import[ \t]+(?:\w+[ \t]+)?"([^"]+)"|^[ \t]+(?:\w+[ \t]+|_[ \t]+|\.[ \t]+)?"([^"]+)"[ \t]*$`)
	graphIdentRE   = regexp.MustCompile(`[A-Za-z_][A-Za-z0-9_]*`)
	graphPyDef     = regexp.MustCompile(`(?m)^([ \t]*)(?:async[ \t]+)?(def|class)[ \t]+([A-Za-z_]\w*)`)
	graphJsDef     = regexp.MustCompile(`(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?(function\*?|class|interface|type|enum|const|let)[ \t]+([A-Za-z_$][\w$]*)`)
	graphGoFunc    = regexp.MustCompile(`(?m)^func[ \t]+(?:\([ \t]*\w*[ \t]*\*?([A-Za-z_]\w*)[^)]*\)[ \t]*)?([A-Za-z_]\w*)`)
	graphGoType    = regexp.MustCompile(`(?m)^type[ \t]+([A-Za-z_]\w*)`)
	graphRustDef   = regexp.MustCompile(`(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?(fn|struct|enum|trait|mod|type)[ \t]+([A-Za-z_]\w*)`)
	graphGoModLine = regexp.MustCompile(`(?m)^module[ \t]+(\S+)`)
)

var graphSkipDirs = map[string]bool{".git": true, "node_modules": true, "target": true, "dist": true, "build": true, ".venv": true, "venv": true, "__pycache__": true, ".mypy_cache": true, ".next": true, "vendor": true}

var graphPackageMarkers = []string{"pyproject.toml", "setup.py", "package.json", "go.mod", "Cargo.toml"}

type graphSymbol struct{ name, kind, parent string }

type graphFile struct {
	language string
	imports  []string
	symbols  []graphSymbol
}

type projectGraph struct {
	root     string
	files    map[string]*graphFile
	defs     map[string][]string // symbol -> files defining it
	refs     map[string][]string // symbol -> files mentioning it
	services map[string]string   // name -> directory ("" for the root)
}

func graphLanguageOf(p string) string {
	switch strings.ToLower(path.Ext(p)) {
	case ".py":
		return "python"
	case ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte":
		return "javascript"
	case ".go":
		return "go"
	case ".rs":
		return "rust"
	}
	return ""
}

// graphFiles lists the repository's source files, relative with slashes:
// git's view when it is a repository (ignored files stay out), else a walk.
func graphFiles(ctx context.Context, root string) []string {
	var files []string
	if out, err := git(ctx, root, nil, 30*time.Second, "ls-files", "--cached", "--others", "--exclude-standard"); err == nil && out != "" {
		for _, line := range strings.Split(out, "\n") {
			if graphLanguageOf(line) != "" || graphIsManifest(line) {
				files = append(files, line)
			}
		}
	} else {
		filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
			if err != nil || ctx.Err() != nil {
				return filepath.SkipDir
			}
			if d.IsDir() {
				if p != root && (graphSkipDirs[d.Name()] || strings.HasPrefix(d.Name(), ".")) {
					return filepath.SkipDir
				}
				return nil
			}
			rel, _ := filepath.Rel(root, p)
			rel = filepath.ToSlash(rel)
			if graphLanguageOf(rel) != "" || graphIsManifest(rel) {
				files = append(files, rel)
			}
			if len(files) > graphMaxFiles {
				return filepath.SkipAll
			}
			return nil
		})
	}
	if len(files) > graphMaxFiles {
		files = files[:graphMaxFiles]
	}
	return files
}

func graphIsManifest(p string) bool {
	base := path.Base(p)
	for _, marker := range graphPackageMarkers {
		if base == marker {
			return true
		}
	}
	return false
}

func buildProjectGraph(ctx context.Context, root string) (*projectGraph, error) {
	g := &projectGraph{root: root, files: map[string]*graphFile{}, defs: map[string][]string{}, refs: map[string][]string{}}
	all := graphFiles(ctx, root)
	sources := map[string]string{}
	for _, p := range all {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		language := graphLanguageOf(p)
		if language == "" {
			continue
		}
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(p)))
		if err != nil || !info.Mode().IsRegular() || info.Size() > graphMaxFileBytes {
			continue
		}
		body, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(p)))
		if err != nil {
			continue
		}
		text := strings.TrimPrefix(strings.ToValidUTF8(string(body), ""), "\ufeff")
		sources[p] = text
		f := &graphFile{language: language, symbols: graphSymbols(language, text)}
		g.files[p] = f
		for _, s := range f.symbols {
			g.defs[s.name] = appendNew(g.defs[s.name], p)
		}
	}
	modules := graphPythonModules(g.files)
	goModules := graphGoModuleDirs(root, all)
	for p, f := range g.files {
		var imports []string
		switch f.language {
		case "python":
			imports = graphPythonImports(p, sources[p], modules)
		case "javascript":
			imports = graphJsImports(p, sources[p], g.files)
		case "go":
			imports = graphGoImports(p, sources[p], goModules, g.files)
		}
		sort.Strings(imports)
		f.imports = graphRemoveValue(appendNew(nil, imports...), p)
	}
	// References: files naming a symbol defined in a handful of files.
	wanted := map[string]bool{}
	for name, places := range g.defs {
		if len(name) >= graphMinName && len(places) <= graphMaxDefining {
			wanted[name] = true
		}
	}
	for p, text := range sources {
		seen := map[string]bool{}
		for _, token := range graphIdentRE.FindAllString(text, -1) {
			if wanted[token] && !seen[token] {
				seen[token] = true
				g.refs[token] = append(g.refs[token], p)
			}
		}
	}
	for name := range g.refs {
		sort.Strings(g.refs[name])
	}
	g.services = graphServices(root, all)
	return g, nil
}

func graphRemoveValue(items []string, value string) []string {
	out := items[:0]
	for _, item := range items {
		if item != value {
			out = append(out, item)
		}
	}
	return out
}

func graphSymbols(language, text string) []graphSymbol {
	var out []graphSymbol
	switch language {
	case "python":
		type open struct {
			indent int
			name   string
		}
		var classes []open
		for _, m := range graphPyDef.FindAllStringSubmatch(text, -1) {
			indent := len(strings.ReplaceAll(m[1], "\t", "    "))
			for len(classes) > 0 && classes[len(classes)-1].indent >= indent {
				classes = classes[:len(classes)-1]
			}
			parent := ""
			if len(classes) > 0 {
				parent = classes[len(classes)-1].name
			}
			out = append(out, graphSymbol{m[3], m[2], parent})
			if m[2] == "class" {
				classes = append(classes, open{indent, m[3]})
			}
		}
	case "javascript":
		for _, m := range graphJsDef.FindAllStringSubmatch(text, -1) {
			out = append(out, graphSymbol{m[2], strings.TrimSuffix(m[1], "*"), ""})
		}
	case "go":
		for _, m := range graphGoFunc.FindAllStringSubmatch(text, -1) {
			out = append(out, graphSymbol{m[2], "func", m[1]})
		}
		for _, m := range graphGoType.FindAllStringSubmatch(text, -1) {
			out = append(out, graphSymbol{m[1], "type", ""})
		}
	case "rust":
		for _, m := range graphRustDef.FindAllStringSubmatch(text, -1) {
			out = append(out, graphSymbol{m[2], m[1], ""})
		}
	}
	return out
}

// graphPythonModules maps every dotted suffix of a Python file's path to it:
// src/pkg/agent.py is agent, pkg.agent and src.pkg.agent.
func graphPythonModules(files map[string]*graphFile) map[string][]string {
	index := map[string][]string{}
	for p, f := range files {
		if f.language != "python" {
			continue
		}
		parts := strings.Split(strings.TrimSuffix(p, ".py"), "/")
		if parts[len(parts)-1] == "__init__" {
			parts = parts[:len(parts)-1]
		}
		for start := range parts {
			name := strings.Join(parts[start:], ".")
			index[name] = append(index[name], p)
		}
	}
	return index
}

// graphPickModule chooses among files answering to one module name: the only
// one, or the one sharing most of the importer's directory.
func graphPickModule(candidates []string, importer string) string {
	if len(candidates) == 0 {
		return ""
	}
	if len(candidates) == 1 {
		return candidates[0]
	}
	shared := func(p string) int {
		a, b := strings.Split(p, "/"), strings.Split(importer, "/")
		n := 0
		for n < len(a) && n < len(b) && a[n] == b[n] {
			n++
		}
		return n
	}
	ranked := append([]string(nil), candidates...)
	sort.SliceStable(ranked, func(i, j int) bool { return shared(ranked[i]) > shared(ranked[j]) })
	if shared(ranked[0]) > shared(ranked[1]) {
		return ranked[0]
	}
	return ""
}

func graphPythonImports(p, text string, modules map[string][]string) []string {
	var found []string
	resolve := func(name string) string { return graphPickModule(modules[name], p) }
	for _, m := range graphPyImport.FindAllStringSubmatch(text, -1) {
		for _, item := range strings.Split(m[1], ",") {
			name := strings.Fields(strings.TrimSpace(item))
			if len(name) > 0 {
				if hit := resolve(name[0]); hit != "" {
					found = append(found, hit)
				}
			}
		}
	}
	pkg := strings.Split(path.Dir(p), "/")
	if path.Dir(p) == "." {
		pkg = nil
	}
	for _, m := range graphPyFrom.FindAllStringSubmatch(text, -1) {
		spec := m[1]
		level := len(spec) - len(strings.TrimLeft(spec, "."))
		module := strings.TrimLeft(spec, ".")
		base := module
		if level > 0 {
			parts := pkg
			if level > 1 {
				parts = pkg[:max(0, len(pkg)-(level-1))]
			}
			if module != "" {
				parts = append(append([]string(nil), parts...), module)
			}
			base = strings.Join(parts, ".")
		}
		names := strings.NewReplacer("(", " ", ")", " ", "\n", " ", "\\", " ").Replace(m[2])
		for _, item := range strings.Split(names, ",") {
			fields := strings.Fields(item)
			if len(fields) == 0 || fields[0] == "*" {
				if base != "" {
					if hit := resolve(base); hit != "" {
						found = append(found, hit)
					}
				}
				continue
			}
			full := fields[0]
			if base != "" {
				full = base + "." + fields[0]
			}
			hit := resolve(full)
			if hit == "" && base != "" {
				hit = resolve(base)
			}
			if hit != "" {
				found = append(found, hit)
			}
		}
	}
	return found
}

func graphJsImports(p, text string, files map[string]*graphFile) []string {
	var found []string
	folder := path.Dir(p)
	for _, m := range graphJsImport.FindAllStringSubmatch(text, -1) {
		base := path.Clean(path.Join(folder, m[1]))
		candidates := []string{base}
		for _, ext := range []string{".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"} {
			candidates = append(candidates, base+ext)
		}
		for _, ext := range []string{".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"} {
			candidates = append(candidates, path.Join(base, "index"+ext))
		}
		for _, c := range candidates {
			if _, ok := files[c]; ok {
				found = append(found, c)
				break
			}
		}
	}
	return found
}

// graphGoModuleDirs maps each go.mod's module path to its directory.
func graphGoModuleDirs(root string, files []string) map[string]string {
	out := map[string]string{}
	for _, p := range files {
		if path.Base(p) != "go.mod" {
			continue
		}
		body, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(p)))
		if err != nil {
			continue
		}
		if m := graphGoModLine.FindStringSubmatch(string(body)); m != nil {
			dir := path.Dir(p)
			if dir == "." {
				dir = ""
			}
			out[m[1]] = dir
		}
	}
	return out
}

// graphGoImports resolves an import of a package in one of the repository's
// modules to the package's non-test files.
func graphGoImports(p, text string, modules map[string]string, files map[string]*graphFile) []string {
	block := text
	if i := strings.Index(text, "\nfunc "); i >= 0 {
		block = text[:i]
	}
	var found []string
	for _, m := range graphGoImport.FindAllStringSubmatch(block, -1) {
		spec := m[1] + m[2]
		for module, dir := range modules {
			if spec != module && !strings.HasPrefix(spec, module+"/") {
				continue
			}
			pkg := strings.TrimPrefix(strings.TrimPrefix(spec, module), "/")
			pkgDir := path.Join(dir, pkg)
			for candidate, f := range files {
				if f.language == "go" && path.Dir(candidate) == pkgDir && !strings.HasSuffix(candidate, "_test.go") {
					found = append(found, candidate)
				}
			}
		}
	}
	return found
}

func graphServices(root string, files []string) map[string]string {
	services := map[string]string{}
	for _, name := range []string{"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} {
		body, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			continue
		}
		var compose struct {
			Services map[string]struct {
				Build any `yaml:"build"`
			} `yaml:"services"`
		}
		if yaml.Unmarshal(body, &compose) != nil {
			continue
		}
		for service, spec := range compose.Services {
			context := ""
			switch b := spec.Build.(type) {
			case string:
				context = b
			case map[string]any:
				context, _ = b["context"].(string)
			default:
				continue
			}
			dir := path.Clean(filepath.ToSlash(context))
			if dir == "." {
				dir = ""
			}
			services[service] = dir
		}
	}
	used := map[string]bool{}
	for _, dir := range services {
		used[dir] = true
	}
	dirs := []string{}
	for _, p := range files {
		if graphIsManifest(p) && path.Dir(p) != "." {
			dirs = appendNew(dirs, path.Dir(p))
		}
	}
	sort.Strings(dirs)
	for _, dir := range dirs {
		if used[dir] {
			continue
		}
		if _, taken := services[path.Base(dir)]; !taken {
			services[path.Base(dir)] = dir
		}
	}
	return services
}

func (g *projectGraph) importers() map[string][]string {
	reverse := map[string][]string{}
	for p, f := range g.files {
		for _, target := range f.imports {
			reverse[target] = append(reverse[target], p)
		}
	}
	return reverse
}

func (g *projectGraph) serviceOf(p string) string {
	best, bestLen := "", -1
	for name, dir := range g.services {
		if dir == "" || p == dir || strings.HasPrefix(p, strings.TrimSuffix(dir, "/")+"/") {
			if len(dir) > bestLen {
				best, bestLen = name, len(dir)
			}
		}
	}
	return best
}

func (g *projectGraph) isHub(p string) bool {
	return path.Base(p) == "__init__.py" || len(g.files[p].imports) > graphHubFanOut
}

func graphSetOf(items []string) map[string]bool {
	out := map[string]bool{}
	for _, item := range items {
		out[item] = true
	}
	return out
}

// symbolUsers are (defining files, mentioning files) for name or
// Class.method.
func (g *projectGraph) symbolUsers(dotted string) (map[string]bool, map[string]bool) {
	parts := []string{}
	for _, part := range strings.Split(dotted, ".") {
		if part != "" {
			parts = append(parts, part)
		}
	}
	if len(parts) == 0 {
		return nil, nil
	}
	last := parts[len(parts)-1]
	defining, users := graphSetOf(g.defs[last]), graphSetOf(g.refs[last])
	for _, outer := range parts[:len(parts)-1] {
		outerRefs := graphSetOf(g.refs[outer])
		for u := range users {
			if !outerRefs[u] {
				delete(users, u)
			}
		}
		if outerDefs := graphSetOf(g.defs[outer]); len(outerDefs) > 0 {
			for d := range defining {
				if !outerDefs[d] {
					delete(defining, d)
				}
			}
		}
	}
	return defining, users
}

func (g *projectGraph) fileUsers(p string, importers map[string][]string) map[string]bool {
	users := graphSetOf(importers[p])
	for _, s := range g.files[p].symbols {
		if s.parent != "" {
			continue // a method is reached through its type
		}
		if contains(g.defs[s.name], p) {
			for _, u := range g.refs[s.name] {
				users[u] = true
			}
		}
	}
	delete(users, p)
	return users
}

func (g *projectGraph) resolveTarget(target string) string {
	raw := strings.TrimSpace(target)
	if filepath.IsAbs(raw) {
		rel, err := filepath.Rel(g.root, raw)
		if err != nil || strings.HasPrefix(rel, "..") {
			return ""
		}
		raw = rel
	}
	raw = strings.TrimPrefix(filepath.ToSlash(raw), "./")
	if _, ok := g.files[raw]; ok {
		return raw
	}
	var matches []string
	for p := range g.files {
		if strings.HasSuffix(p, "/"+raw) {
			matches = append(matches, p)
		}
	}
	if len(matches) == 1 {
		return matches[0]
	}
	return ""
}

type graphImpact struct {
	targets, unresolved, direct, indirect, tests, services []string
}

func (g *projectGraph) impact(targets []string) graphImpact {
	result := graphImpact{targets: targets}
	origin, changed, direct := map[string]bool{}, map[string]bool{}, map[string]bool{}
	importers := g.importers()
	for _, target := range targets {
		if p := g.resolveTarget(target); p != "" {
			origin[p], changed[p] = true, true
			for u := range g.fileUsers(p, importers) {
				direct[u] = true
			}
			continue
		}
		// Importing the file that defines a symbol is not using it: only
		// files naming it are affected, and the walk starts from them.
		defining, users := g.symbolUsers(target)
		if len(defining) == 0 && len(users) == 0 {
			result.unresolved = append(result.unresolved, target)
			continue
		}
		for d := range defining {
			origin[d] = true
		}
		for u := range users {
			direct[u] = true
		}
	}
	for o := range origin {
		delete(direct, o)
	}
	type step struct {
		path  string
		depth int
	}
	var frontier []step
	visited := map[string]bool{}
	for p := range direct {
		frontier, visited[p] = append(frontier, step{p, 1}), true
	}
	for p := range changed {
		frontier = append(frontier, step{p, 1})
	}
	for p := range origin {
		visited[p] = true
	}
	indirect := map[string]bool{}
	for len(frontier) > 0 {
		s := frontier[0]
		frontier = frontier[1:]
		if s.depth > graphDepth || (!changed[s.path] && g.isHub(s.path)) {
			continue
		}
		for _, parent := range importers[s.path] {
			if !visited[parent] {
				visited[parent], indirect[parent] = true, true
				frontier = append(frontier, step{parent, s.depth + 1})
			}
		}
	}
	sorted := func(m map[string]bool) []string {
		out := make([]string, 0, len(m))
		for k := range m {
			out = append(out, k)
		}
		sort.Strings(out)
		return out
	}
	for p := range direct {
		delete(indirect, p)
	}
	result.direct, result.indirect = sorted(direct), sorted(indirect)
	services := map[string]bool{}
	for _, set := range []map[string]bool{origin, direct, indirect} {
		for p := range set {
			if isTestPath(p) && !origin[p] {
				result.tests = appendNew(result.tests, p)
			}
			if s := g.serviceOf(p); s != "" {
				services[s] = true
			}
		}
	}
	sort.Strings(result.tests)
	result.services = sorted(services)
	return result
}

func (r graphImpact) breadth() string {
	switch reach := len(r.direct) + len(r.indirect); {
	case reach == 0:
		return "isolated"
	case reach <= 5:
		return "narrow"
	case reach <= 25:
		return "moderate"
	}
	return "wide"
}

func (r graphImpact) render(limit int) string {
	targets := strings.Join(r.targets, ", ")
	if targets == "" {
		targets = "(nothing)"
	}
	lines := []string{fmt.Sprintf("Impact of %s: %s", targets, r.breadth())}
	if len(r.unresolved) > 0 {
		lines = append(lines, "  not found: "+strings.Join(r.unresolved, ", "))
	}
	block := func(title string, items []string) {
		if len(items) == 0 {
			return
		}
		lines = append(lines, fmt.Sprintf("  %s (%d)", title, len(items)))
		for i, item := range items {
			if i == limit {
				lines = append(lines, fmt.Sprintf("    … and %d more", len(items)-limit))
				break
			}
			lines = append(lines, "    "+item)
		}
	}
	block("Used directly by", r.direct)
	block("Reached through imports", r.indirect)
	block("Tests", r.tests)
	if len(r.services) > 0 {
		lines = append(lines, "  Services  "+strings.Join(r.services, ", "))
	}
	if len(r.direct)+len(r.indirect)+len(r.tests) == 0 {
		lines = append(lines, "  Nothing else in the repository depends on it.")
	}
	return strings.Join(lines, "\n")
}

func (g *projectGraph) summary() string {
	edges, tests := 0, 0
	for p, f := range g.files {
		edges += len(f.imports)
		if isTestPath(p) {
			tests++
		}
	}
	text := fmt.Sprintf("Project graph: %d files · %d symbols · %d imports · %d test files", len(g.files), len(g.defs), edges, tests)
	if len(g.services) > 0 {
		names := make([]string, 0, len(g.services))
		for name, dir := range g.services {
			if dir == "" {
				dir = "."
			}
			names = append(names, name+" ("+dir+")")
		}
		sort.Strings(names)
		text += "\nServices: " + strings.Join(names, ", ")
	}
	return text + "\n\n/impact <path|symbol> … shows what a change reaches"
}

// graphRoot is the repository the workspace is in, or the workspace.
func (s *server) graphRoot(ctx context.Context) string {
	if top, err := git(ctx, s.scope.workspace, nil, 5*time.Second, "rev-parse", "--show-toplevel"); err == nil && top != "" {
		if canonicalTop, err := canonical(top); err == nil {
			return canonicalTop
		}
		return top
	}
	return s.scope.workspace
}

func (s *server) graphCommand(ctx context.Context, args []string) (string, error) {
	g, err := buildProjectGraph(ctx, s.graphRoot(ctx))
	if err != nil {
		return "", err
	}
	if len(args) == 0 {
		return g.summary(), nil
	}
	return g.impact(args).render(12), nil
}

func (s *server) impactCommand(ctx context.Context, args []string) (string, error) {
	if len(args) == 0 {
		return "", errors.New("usage: /impact <path|symbol> … — e.g. /impact go/runtime/server.go turnHooks")
	}
	return s.graphCommand(ctx, args)
}

// ── /lsp: diagnostics through the language's own graphChecker ─────────────────

type graphChecker struct {
	language, binary string
	command          func(file string) string
}

var graphCheckers = []graphChecker{
	{"python", "ruff", func(f string) string { return "ruff check " + argQuote(f) }},
	{"python", "pyright", func(f string) string { return "pyright " + argQuote(f) }},
	{"python", "python3", func(f string) string { return "python3 -m py_compile " + argQuote(f) }},
	{"go", "go", func(f string) string { return "go vet ./" + filepath.ToSlash(filepath.Dir(f)) }},
	{"javascript", "npx", func(string) string { return "npx --no-install tsc --noEmit" }},
	{"rust", "cargo", func(string) string { return "cargo check --quiet" }},
}

var graphLanguageServers = []string{"gopls", "pyright-langserver", "pylsp", "typescript-language-server", "rust-analyzer", "ruff"}

func (s *server) lspCommand(ctx context.Context, turn string, args []string) (string, error) {
	if len(args) == 0 {
		var b strings.Builder
		b.WriteString("Language tools on PATH")
		for _, name := range graphLanguageServers {
			if p, err := exec.LookPath(name); err == nil {
				fmt.Fprintf(&b, "\n  ● %-28s %s", name, p)
			} else {
				fmt.Fprintf(&b, "\n  ○ %-28s not found", name)
			}
		}
		return b.String() + "\n\n/lsp <file> runs the file's graphChecker and shows its diagnostics", nil
	}
	if len(args) != 1 {
		return "", errors.New("usage: /lsp [file]")
	}
	file := args[0]
	if _, err := s.readTool(ctx, "read_file", map[string]any{"path": file, "limit": float64(1)}); err != nil {
		return "", err
	}
	language := graphLanguageOf(file)
	for _, c := range graphCheckers {
		if c.language != language {
			continue
		}
		if _, err := exec.LookPath(c.binary); err != nil {
			continue
		}
		return s.userCommand(ctx, turn, c.command(file))
	}
	if language == "" {
		return "", fmt.Errorf("no checker for %s", file)
	}
	return "", fmt.Errorf("no %s checker found on PATH", language)
}
