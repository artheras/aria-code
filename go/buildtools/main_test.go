package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// An actual child executable exercises copying, execution and rename behavior
// on each platform; it does not replace the real-Rust integration test below.
func TestMain(m *testing.M) {
	if helper := os.Getenv("ARIA_BUNDLE_TEST_HELPER"); helper != "" {
		switch {
		case len(os.Args) == 2 && os.Args[1] == "--version":
			fmt.Println("aria-native test")
		case len(os.Args) == 3 && os.Args[1] == "index":
			if helper == "invalid" {
				fmt.Println(`{"imports":null,"fallback":[]}`)
			} else {
				fmt.Println(`{"imports":{},"fallback":[]}`)
			}
		case len(os.Args) > 1 && os.Args[1] == "build":
			// bundle's cargo subprocess: the fixture already wrote its output.
		default:
			os.Exit(3)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func fixture(t *testing.T, internal bool) (string, string) {
	t.Helper()
	onedir := filepath.Join(t.TempDir(), "distribution with spaces")
	if err := os.MkdirAll(onedir, 0o755); err != nil {
		t.Fatal(err)
	}
	if internal {
		if err := os.Mkdir(filepath.Join(onedir, "_internal"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(onedir, executable("aria-code-bin")), []byte("existing worker bytes"), 0o755); err != nil {
		t.Fatal(err)
	}
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return binary, onedir
}

func TestInstallPreservesWorkerAndContentsLayouts(t *testing.T) {
	for _, internal := range []bool{false, true} {
		t.Run(fmt.Sprint(internal), func(t *testing.T) {
			t.Setenv("ARIA_BUNDLE_TEST_HELPER", "valid")
			binary, dir := fixture(t, internal)
			var log bytes.Buffer
			if err := installNative(binary, dir, true, &log); err != nil {
				t.Fatal(err)
			}
			worker, err := os.ReadFile(filepath.Join(dir, executable("aria-code-worker")))
			if err != nil || string(worker) != "existing worker bytes" {
				t.Fatalf("worker was changed: %q %v", worker, err)
			}
			for _, path := range []string{filepath.Join(contentsDir(dir), executable("aria-native")), filepath.Join(dir, executable("aria-code-bin"))} {
				if _, err := probe(path, dir); err != nil {
					t.Fatal(err)
				}
				if info, err := os.Stat(path); err != nil || (runtime.GOOS != "windows" && info.Mode().Perm() != 0o755) {
					t.Fatalf("executable mode: %v %v", info, err)
				}
			}
			if !strings.Contains(log.String(), "native frontend installed") {
				t.Fatal(log.String())
			}
			leftovers, _ := filepath.Glob(filepath.Join(dir, ".aria-bundle-*"))
			if len(leftovers) != 0 {
				t.Fatal(leftovers)
			}
		})
	}
}

func TestFailedProbeLeavesExistingLauncherUntouched(t *testing.T) {
	t.Setenv("ARIA_BUNDLE_TEST_HELPER", "invalid")
	binary, dir := fixture(t, true)
	if err := installNative(binary, dir, true, io.Discard); err == nil {
		t.Fatal("invalid index response accepted")
	}
	launcher, _ := os.ReadFile(filepath.Join(dir, executable("aria-code-bin")))
	if string(launcher) != "existing worker bytes" {
		t.Fatal("launcher replaced on failed probe")
	}
	for _, path := range []string{filepath.Join(dir, executable("aria-code-worker")), filepath.Join(contentsDir(dir), executable("aria-native"))} {
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("failed probe created %s", path)
		}
	}
	leftovers, _ := filepath.Glob(filepath.Join(contentsDir(dir), ".aria-bundle-*"))
	if len(leftovers) != 0 {
		t.Fatal(leftovers)
	}
}

func TestExistingWorkerIsNeverOverwritten(t *testing.T) {
	t.Setenv("ARIA_BUNDLE_TEST_HELPER", "valid")
	binary, dir := fixture(t, true)
	worker := filepath.Join(dir, executable("aria-code-worker"))
	if err := os.WriteFile(worker, []byte("previous release"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := installNative(binary, dir, true, io.Discard); err == nil {
		t.Fatal("existing worker accepted")
	}
	body, _ := os.ReadFile(worker)
	if string(body) != "previous release" {
		t.Fatal("worker overwritten")
	}
}

func TestIndexerOnlyDoesNotRenameLauncher(t *testing.T) {
	t.Setenv("ARIA_BUNDLE_TEST_HELPER", "valid")
	binary, dir := fixture(t, true)
	if err := installNative(binary, dir, false, io.Discard); err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(filepath.Join(dir, executable("aria-code-bin")))
	if string(body) != "existing worker bytes" {
		t.Fatal("index-only bundle changed launcher")
	}
}

func TestRelativeCargoTargetDirectory(t *testing.T) {
	t.Setenv("ARIA_BUNDLE_TEST_HELPER", "valid")
	t.Setenv("CARGO_TARGET_DIR", "custom target")
	binary, dir := fixture(t, true)
	root := t.TempDir()
	bin := filepath.Join(root, "rust", "custom target", "release")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	built, err := copyTemporary(binary, bin)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(built, filepath.Join(bin, executable("aria-native"))); err != nil {
		t.Fatal(err)
	}
	cargoDir := t.TempDir()
	cargo, err := copyTemporary(binary, cargoDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(cargo, filepath.Join(cargoDir, executable("cargo"))); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", cargoDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	if err := bundle(root, dir, true, io.Discard); err != nil {
		t.Fatal(err)
	}
}

func TestRealRustReleaseBundle(t *testing.T) {
	binary := os.Getenv("ARIA_NATIVE_BINARY")
	if binary == "" {
		t.Skip("set ARIA_NATIVE_BINARY to verify the built Rust frontend")
	}
	_, dir := fixture(t, true)
	if err := installNative(binary, dir, true, io.Discard); err != nil {
		t.Fatal(err)
	}
	// The formal launcher forwards index commands to the retained worker;
	// exercise native indexing through the bundled aria-native instead.
	if _, err := probe(filepath.Join(contentsDir(dir), executable("aria-native")), dir); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command(filepath.Join(dir, executable("aria-code-bin")), "--version").CombinedOutput(); err != nil {
		t.Fatalf("formal launcher version: %s %v", out, err)
	}
}
