package main

import (
	"os/exec"
	"strconv"
	"strings"
	"testing"
)

func TestPythonSyntaxErrorMatchesTheReviewContract(t *testing.T) {
	line, evidence, found := pythonSyntaxError("def broken(:\n")
	if !found || line != 1 || evidence != "def broken(:" {
		t.Fatalf("broken def: found=%v line=%d evidence=%q", found, line, evidence)
	}
	file := reviewFile{Path: "broken.py", Content: "def broken(:\n", Language: "Python"}
	findings := scanReviewFile(file)
	if len(findings) == 0 || findings[0].Rule != "python-syntax-error" || findings[0].Severity != "critical" {
		t.Fatalf("findings = %+v", findings)
	}
	if findings[0].Message != "Python 代码无法解析，运行前会失败。" {
		t.Fatalf("message = %q", findings[0].Message)
	}
}

func TestPythonSyntaxAgreesWithCPythonOnStructure(t *testing.T) {
	python, err := exec.LookPath("python3.11")
	if err != nil {
		python, err = exec.LookPath("python3")
		if err != nil {
			t.Skip("python3 is not installed")
		}
	}
	samples := []string{
		"def f():\n    return 1\n",
		"class A:\n    def g(self):\n        return 1\n",
		"try:\n    pass\nexcept Exception:\n    pass\n",
		"x = (\n  1,\n  2\n)\n",
		"s = '''hello\nworld'''\n",
		"@deco\ndef f():\n    pass\n",
		"def broken(:\n",
		"def f(\n    return\n",
		"x = (\n",
		"if True\n    pass\n",
		"def f():\npass\n",
		"a = 'unterminated\n",
		"value = (1, 2]\n",
		"if:\n    pass\n",
		"\tpass\n",
		"async def run():\n    await work()\n",
		"with open(path) as handle:\n    handle.read()\n",
		"text = f\"value {1 + 2}\"\n",
		"# only a comment\n",
		"def f():\n\treturn 1\n",
	}
	script := `import ast,sys
src=sys.stdin.read()
try:
    ast.parse(src)
    print("OK")
except SyntaxError as exc:
    print(exc.lineno or 0)
`
	for _, sample := range samples {
		got := cpythonLine(t, python, script, sample)
		line, _, found := pythonSyntaxError(sample)
		if got == 0 && found {
			t.Errorf("valid Python was rejected: %q line %d", sample, line)
		}
		if got > 0 && !found {
			t.Errorf("CPython line %d was accepted: %q", got, sample)
		}
		if got > 0 && found && line != got {
			t.Errorf("sample %q: CPython line %d, checker line %d", sample, got, line)
		}
	}
}

func cpythonLine(t *testing.T, python, script, sample string) int {
	t.Helper()
	cmd := exec.Command(python, "-c", script)
	cmd.Stdin = strings.NewReader(sample)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("python failed on %q: %v\n%s", sample, err, out)
	}
	text := strings.TrimSpace(string(out))
	if text == "OK" {
		return 0
	}
	line, err := strconv.Atoi(text)
	if err != nil {
		t.Fatalf("python output %q: %v", text, err)
	}
	return line
}
