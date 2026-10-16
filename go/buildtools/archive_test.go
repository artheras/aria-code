package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func releaseFixture(t *testing.T, name string) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), name)
	if err := os.MkdirAll(filepath.Join(root, "_internal", "Versions", "3.13"), 0o755); err != nil {
		t.Fatal(err)
	}
	for file, data := range map[string]string{name: "#!/bin/sh\nexit 0\n", "_internal/Versions/3.13/Python": "library bytes"} {
		if err := os.WriteFile(filepath.Join(root, filepath.FromSlash(file)), []byte(data), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestArchiveRoundTrip(t *testing.T) {
	for _, zipped := range []bool{false, true} {
		t.Run(map[bool]string{false: "tar", true: "zip"}[zipped], func(t *testing.T) {
			root := releaseFixture(t, "aria-code-bin")
			archive, err := packDirectory(root, t.TempDir(), zipped)
			if err != nil {
				t.Fatal(err)
			}
			got, err := unpackArchive(archive, t.TempDir(), false)
			if err != nil {
				t.Fatal(err)
			}
			for _, file := range []string{"aria-code-bin", "_internal/Versions/3.13/Python"} {
				data, err := os.ReadFile(filepath.Join(got, filepath.FromSlash(file)))
				if err != nil || len(data) == 0 {
					t.Fatalf("missing bytes: %v", err)
				}
				info, _ := os.Stat(filepath.Join(got, filepath.FromSlash(file)))
				if runtime.GOOS != "windows" && info.Mode().Perm() != 0o755 {
					t.Fatal(info.Mode())
				}
			}
		})
	}
}

func TestArchiveLinksPreservedAndDereferenced(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows releases contain no symlinks")
	}
	root := releaseFixture(t, "aria-code-bin")
	for name, target := range map[string]string{"_internal/Versions/Current": "3.13", "_internal/Python": "Versions/Current/Python"} {
		if err := os.Symlink(target, filepath.Join(root, name)); err != nil {
			t.Fatal(err)
		}
	}
	archive, err := packDirectory(root, t.TempDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	for _, dereference := range []bool{false, true} {
		got, err := unpackArchive(archive, t.TempDir(), dereference)
		if err != nil {
			t.Fatal(err)
		}
		file := filepath.Join(got, "_internal", "Python")
		data, err := os.ReadFile(file)
		if err != nil || string(data) != "library bytes" {
			t.Fatalf("link lost bytes: %s %v", data, err)
		}
		info, _ := os.Lstat(file)
		if (info.Mode()&os.ModeSymlink == 0) != dereference {
			t.Fatal("unexpected link mode")
		}
	}
	if _, err := packDirectory(root, t.TempDir(), true); err == nil {
		t.Fatal("zip accepted symlinks")
	}
}

func tarFixture(t *testing.T, entries []*tar.Header) string {
	t.Helper()
	name := filepath.Join(t.TempDir(), "fixture.tar.gz")
	f, err := os.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	for _, entry := range entries {
		if err := tw.WriteHeader(entry); err != nil {
			t.Fatal(err)
		}
		if entry.Size > 0 {
			if _, err := io.WriteString(tw, strings.Repeat("x", int(entry.Size))); err != nil {
				t.Fatal(err)
			}
		}
	}
	for _, closer := range []io.Closer{tw, gz, f} {
		if err := closer.Close(); err != nil {
			t.Fatal(err)
		}
	}
	return name
}

func TestUnsafeArchivesPreserveExistingOutput(t *testing.T) {
	cases := map[string][]*tar.Header{
		"parent":         {{Name: "../escape", Mode: 0o644, Size: 1}},
		"absolute":       {{Name: "/escape", Mode: 0o644, Size: 1}},
		"windows":        {{Name: "C:/escape", Mode: 0o644, Size: 1}},
		"duplicate":      {{Name: "app/file", Mode: 0o644, Size: 1}, {Name: "app/file", Mode: 0o644, Size: 1}},
		"multiple-roots": {{Name: "app/file", Mode: 0o644, Size: 1}, {Name: "other/file", Mode: 0o644, Size: 1}},
		"external-link":  {{Name: "app/file", Typeflag: tar.TypeSymlink, Linkname: "../../escape"}},
		"dangling-link":  {{Name: "app/file", Typeflag: tar.TypeSymlink, Linkname: "missing"}},
		"link-parent":    {{Name: "app/dir", Typeflag: tar.TypeSymlink, Linkname: "target"}, {Name: "app/dir/nested", Typeflag: tar.TypeSymlink, Linkname: "../target"}},
		"device":         {{Name: "app/dev", Typeflag: tar.TypeChar}},
		"hardlink-cycle": {{Name: "app/a", Typeflag: tar.TypeLink, Linkname: "app/b"}, {Name: "app/b", Typeflag: tar.TypeLink, Linkname: "app/a"}},
	}
	for name, headers := range cases {
		t.Run(name, func(t *testing.T) {
			archive := tarFixture(t, headers)
			dest := t.TempDir()
			if err := os.Mkdir(filepath.Join(dest, "app"), 0o755); err != nil {
				t.Fatal(err)
			}
			marker := filepath.Join(dest, "app", "keep")
			if err := os.WriteFile(marker, []byte("existing"), 0o644); err != nil {
				t.Fatal(err)
			}
			if _, err := unpackArchive(archive, dest, true); err == nil {
				t.Fatal("unsafe archive accepted")
			}
			data, err := os.ReadFile(marker)
			if err != nil || string(data) != "existing" {
				t.Fatal("existing build changed")
			}
		})
	}
}

func TestTarHardlinksAndZipTraversal(t *testing.T) {
	archive := tarFixture(t, []*tar.Header{{Name: "app/copy", Typeflag: tar.TypeLink, Linkname: "app/data"}, {Name: "app/data", Mode: 0o755, Size: 3}})
	got, err := unpackArchive(archive, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(got, "copy"))
	if err != nil || string(data) != "xxx" {
		t.Fatal("hardlink not materialized")
	}
	name := filepath.Join(t.TempDir(), "bad.zip")
	f, err := os.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	z := zip.NewWriter(f)
	w, err := z.Create("../outside")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = w.Write([]byte("bad"))
	_ = z.Close()
	_ = f.Close()
	if _, err := unpackArchive(name, t.TempDir(), false); err == nil {
		t.Fatal("zip traversal accepted")
	}
}

func TestFailedPackRetainsPreviousArchive(t *testing.T) {
	root := releaseFixture(t, "app")
	dest := t.TempDir()
	archive, err := packDirectory(root, dest, true)
	if err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(archive)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		t.Skip("symlink fixture requires Unix")
	}
	if err := os.Symlink("app", filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	if _, err := packDirectory(root, dest, true); err == nil {
		t.Fatal("failed pack succeeded")
	}
	after, err := os.ReadFile(archive)
	if err != nil || string(after) != string(before) {
		t.Fatal("previous archive was replaced")
	}
}

func TestBadPackageSetPreservesAllPreviousPackages(t *testing.T) {
	cli, err := packDirectory(releaseFixture(t, "aria-code-bin"), t.TempDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	mcp, err := packDirectory(releaseFixture(t, "aria-code-mcp-bin"), t.TempDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	out := t.TempDir()
	if err := buildPlatforms("1.0.0", archivePairs{"linux-x64": cli}, archivePairs{"linux-x64": mcp}, out, io.Discard); err != nil {
		t.Fatal(err)
	}
	manifest := filepath.Join(out, "aria-code-linux-x64", "package.json")
	before, _ := os.ReadFile(manifest)
	if err := buildPlatforms("2.0.0", archivePairs{"linux-x64": cli}, archivePairs{"linux-x64": cli}, out, io.Discard); err == nil {
		t.Fatal("wrong MCP build accepted")
	}
	after, _ := os.ReadFile(manifest)
	if string(before) != string(after) {
		t.Fatal("partial failed build replaced old package")
	}
}
