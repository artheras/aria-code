package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

const archiveByteLimit int64 = 8 << 30
const archiveFileLimit = 100000

func packDirectory(source, output string, useZip bool) (string, error) {
	source, err := filepath.Abs(source)
	if err != nil {
		return "", err
	}
	source, err = filepath.EvalSymlinks(source)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(source)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("not a build directory: %s", source)
	}
	if err := os.MkdirAll(output, 0o755); err != nil {
		return "", err
	}
	file, err := os.CreateTemp(output, ".aria-archive-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	var tw *tar.Writer
	var zw *zip.Writer
	var gz *gzip.Writer
	ext := ".tar.gz"
	if useZip {
		ext = ".zip"
		zw = zip.NewWriter(file)
	} else {
		gz = gzip.NewWriter(file)
		tw = tar.NewWriter(gz)
	}
	outputAbs, _ := filepath.EvalSymlinks(output)
	outputAbs, _ = filepath.Abs(outputAbs)
	if within(source, outputAbs) {
		return "", fmt.Errorf("archive output must be outside source")
	}
	err = filepath.WalkDir(source, func(filename string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(filepath.Dir(source), filename)
		name := filepath.ToSlash(rel)
		link := ""
		if info.Mode()&os.ModeSymlink != 0 {
			link, err = os.Readlink(filename)
			if err != nil {
				return err
			}
		}
		if !info.Mode().IsRegular() && !info.IsDir() && link == "" {
			return fmt.Errorf("unsupported archive file: %s", filename)
		}
		var writer io.Writer
		if useZip {
			if link != "" {
				return fmt.Errorf("zip cannot carry symlinks: %s", filename)
			}
			header, err := zip.FileInfoHeader(info)
			if err != nil {
				return err
			}
			header.Name = name
			header.Method = zip.Deflate
			if info.IsDir() {
				header.Name += "/"
			}
			writer, err = zw.CreateHeader(header)
			if err != nil {
				return err
			}
		} else {
			header, err := tar.FileInfoHeader(info, link)
			if err != nil {
				return err
			}
			header.Name = name
			if err := tw.WriteHeader(header); err != nil {
				return err
			}
			writer = tw
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		in, err := os.Open(filename)
		if err != nil {
			return err
		}
		defer in.Close()
		_, err = io.Copy(writer, in)
		return err
	})
	if err != nil {
		return "", err
	}
	if useZip {
		err = zw.Close()
	} else {
		err = tw.Close()
		if err == nil {
			err = gz.Close()
		}
	}
	if err != nil {
		return "", err
	}
	if err := file.Close(); err != nil {
		return "", err
	}
	final := filepath.Join(output, filepath.Base(source)+ext)
	if err := os.Rename(file.Name(), final); err != nil {
		return "", err
	}
	return final, nil
}

func within(root, target string) bool {
	rel, err := filepath.Rel(root, target)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

func archiveName(name string) (string, error) {
	clean := path.Clean(name)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") || strings.HasPrefix(clean, "/") || strings.ContainsAny(name, "\\:\x00") {
		return "", fmt.Errorf("unsafe archive path: %q", name)
	}
	if runtime.GOOS == "windows" && !filepath.IsLocal(filepath.FromSlash(clean)) {
		return "", fmt.Errorf("unsafe Windows path: %q", name)
	}
	return clean, nil
}

type archiveLink struct {
	name, target string
	hard         bool
}
type extraction struct {
	root, top string
	bytes     int64
	count     int
	links     []archiveLink
	seen      map[string]bool
}

func (x *extraction) entry(name string, mode os.FileMode, size int64, kind byte, target string, reader io.Reader) error {
	clean, err := archiveName(name)
	if err != nil {
		return err
	}
	top := strings.SplitN(clean, "/", 2)[0]
	if x.top == "" {
		x.top = top
	} else if top != x.top {
		return fmt.Errorf("archive must contain one directory")
	}
	if x.seen[clean] {
		return fmt.Errorf("duplicate archive entry: %s", clean)
	}
	x.seen[clean] = true
	x.count++
	if x.count > archiveFileLimit || size < 0 || size > archiveByteLimit-x.bytes {
		return fmt.Errorf("archive exceeds extraction limits")
	}
	x.bytes += size
	filename := filepath.Join(x.root, filepath.FromSlash(clean))
	if kind == tar.TypeSymlink || kind == tar.TypeLink {
		if len(x.links) >= 4096 {
			return fmt.Errorf("too many archive links")
		}
		resolved := target
		if kind == tar.TypeSymlink {
			resolved = path.Join(path.Dir(clean), target)
		}
		if strings.HasPrefix(target, "/") || strings.ContainsAny(target, "\\:\x00") {
			return fmt.Errorf("unsafe archive link: %s", clean)
		}
		resolved, err = archiveName(resolved)
		if err != nil || !strings.HasPrefix(resolved, top+"/") {
			return fmt.Errorf("link escapes build: %s", clean)
		}
		x.links = append(x.links, archiveLink{clean, target, kind == tar.TypeLink})
		return nil
	}
	if kind == tar.TypeDir {
		return os.MkdirAll(filename, 0o755)
	}
	if kind != tar.TypeReg && kind != tar.TypeRegA {
		return fmt.Errorf("unsupported archive entry: %s", clean)
	}
	if clean == top {
		return fmt.Errorf("archive root must be a directory")
	}
	if err := os.MkdirAll(filepath.Dir(filename), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(filename, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	_, copyErr := io.CopyN(f, reader, size)
	modeErr := f.Chmod(mode.Perm() & 0o777)
	closeErr := f.Close()
	if copyErr != nil {
		return copyErr
	}
	if modeErr != nil {
		return modeErr
	}
	return closeErr
}

func extractArchive(archive, staging string) (string, error) {
	x := extraction{root: staging, seen: map[string]bool{}}
	if strings.HasSuffix(archive, ".zip") {
		z, err := zip.OpenReader(archive)
		if err != nil {
			return "", err
		}
		defer z.Close()
		for _, member := range z.File {
			if member.UncompressedSize64 > uint64(archiveByteLimit) {
				return "", fmt.Errorf("zip member too large")
			}
			kind := byte(tar.TypeReg)
			if member.FileInfo().IsDir() {
				kind = tar.TypeDir
			} else if !member.Mode().IsRegular() {
				return "", fmt.Errorf("unsupported zip entry: %s", member.Name)
			}
			r, err := member.Open()
			if err != nil {
				return "", err
			}
			err = x.entry(member.Name, member.Mode(), int64(member.UncompressedSize64), kind, "", r)
			// Reading to EOF also verifies the ZIP checksum.
			if err == nil {
				_, err = io.Copy(io.Discard, r)
			}
			closeErr := r.Close()
			if err != nil {
				return "", err
			}
			if closeErr != nil {
				return "", closeErr
			}
		}
	} else {
		f, err := os.Open(archive)
		if err != nil {
			return "", err
		}
		defer f.Close()
		gz, err := gzip.NewReader(f)
		if err != nil {
			return "", err
		}
		defer gz.Close()
		tr := tar.NewReader(gz)
		for {
			h, err := tr.Next()
			if err == io.EOF {
				break
			}
			if err != nil {
				return "", err
			}
			if err := x.entry(h.Name, h.FileInfo().Mode(), h.Size, h.Typeflag, h.Linkname, tr); err != nil {
				return "", err
			}
		}
		n, err := io.Copy(io.Discard, io.LimitReader(gz, 1<<20))
		if err != nil {
			return "", err
		}
		if n == 1<<20 {
			return "", fmt.Errorf("excessive trailing archive data")
		}
	}
	if x.top == "" {
		return "", fmt.Errorf("empty archive")
	}
	root := filepath.Join(staging, x.top)
	linkNames := map[string]bool{}
	for _, link := range x.links {
		linkNames[link.name] = true
	}
	for _, link := range x.links {
		for parent := path.Dir(link.name); parent != "."; parent = path.Dir(parent) {
			if linkNames[parent] {
				return "", fmt.Errorf("link parent is redirected: %s", link.name)
			}
		}
	}
	// Create links last: archive data can never be written through a link.
	for _, link := range x.links {
		if link.hard {
			continue
		}
		file := filepath.Join(staging, filepath.FromSlash(link.name))
		if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
			return "", err
		}
		if err := os.Symlink(filepath.FromSlash(link.target), file); err != nil {
			return "", err
		}
	}
	// tar hardlinks may refer to a later hardlink; resolve in bounded passes.
	pending := append([]archiveLink(nil), x.links...)
	for len(pending) > 0 {
		var next []archiveLink
		progress := false
		for _, link := range pending {
			if !link.hard {
				continue
			}
			target := filepath.Join(staging, filepath.FromSlash(link.target))
			resolved, err := filepath.EvalSymlinks(target)
			if os.IsNotExist(err) {
				next = append(next, link)
				continue
			}
			if err != nil || !within(root, resolved) {
				return "", fmt.Errorf("invalid hardlink %s", link.name)
			}
			info, err := os.Stat(resolved)
			if err != nil || !info.Mode().IsRegular() {
				return "", fmt.Errorf("invalid hardlink target")
			}
			file := filepath.Join(staging, filepath.FromSlash(link.name))
			if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
				return "", err
			}
			// The parent may only be an ordinary directory, not an archive link.
			parent, err := filepath.EvalSymlinks(filepath.Dir(file))
			if err != nil || parent != filepath.Dir(file) {
				return "", fmt.Errorf("hardlink parent is redirected")
			}
			if err := os.Link(resolved, file); err != nil {
				return "", err
			}
			progress = true
		}
		if len(next) > 0 && !progress {
			return "", fmt.Errorf("dangling or cyclic hardlinks")
		}
		pending = next
	}
	for _, link := range x.links {
		resolved, err := filepath.EvalSymlinks(filepath.Join(staging, filepath.FromSlash(link.name)))
		if err != nil || !within(root, resolved) {
			return "", fmt.Errorf("dangling, cyclic or external link: %s", link.name)
		}
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("archive root is not a directory")
	}
	return root, nil
}

func copyDereferenced(root, source, dest string, stack map[string]bool, budget *int64, count *int) error {
	resolved, err := filepath.EvalSymlinks(source)
	if err != nil || !within(root, resolved) {
		return fmt.Errorf("invalid link in %s", source)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return err
	}
	*count++
	if *count > archiveFileLimit {
		return fmt.Errorf("dereferenced tree exceeds file limit")
	}
	if info.IsDir() {
		if stack[resolved] {
			return fmt.Errorf("directory link cycle: %s", source)
		}
		stack[resolved] = true
		defer delete(stack, resolved)
		if err := os.Mkdir(dest, 0o755); err != nil {
			return err
		}
		entries, err := os.ReadDir(resolved)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if err := copyDereferenced(root, filepath.Join(resolved, entry.Name()), filepath.Join(dest, entry.Name()), stack, budget, count); err != nil {
				return err
			}
		}
		return nil
	}
	if !info.Mode().IsRegular() || info.Size() > *budget {
		return fmt.Errorf("invalid or oversized file: %s", source)
	}
	*budget -= info.Size()
	in, err := os.Open(resolved)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dest, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_, err = io.CopyN(out, in, info.Size())
	chmodErr := out.Chmod(info.Mode().Perm())
	closeErr := out.Close()
	if err != nil {
		return err
	}
	if chmodErr != nil {
		return chmodErr
	}
	return closeErr
}

// Replacement uses a recoverable rename; validation happens before this call.
func promoteDirectory(prepared, final string) error {
	var backup string
	if _, err := os.Lstat(final); err == nil {
		backup, err = os.MkdirTemp(filepath.Dir(final), ".aria-previous-*")
		if err != nil {
			return err
		}
		if err := os.Remove(backup); err != nil {
			return err
		}
		if err := os.Rename(final, backup); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.Rename(prepared, final); err != nil {
		if backup != "" {
			if rollback := os.Rename(backup, final); rollback != nil {
				return fmt.Errorf("promotion: %v; previous tree remains at %s: %w", err, backup, rollback)
			}
		}
		return err
	}
	if backup != "" {
		return os.RemoveAll(backup)
	}
	return nil
}

func unpackArchive(archive, dest string, dereference bool) (string, error) {
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return "", err
	}
	staging, err := os.MkdirTemp(dest, ".aria-unpack-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(staging)
	// macOS /var is a symlink; canonicalize the staging root before link checks.
	staging, err = filepath.EvalSymlinks(staging)
	if err != nil {
		return "", err
	}
	root, err := extractArchive(archive, staging)
	if err != nil {
		return "", err
	}
	name := filepath.Base(root)
	if dereference {
		prepared := filepath.Join(staging, ".dereferenced")
		budget, count := archiveByteLimit, 0
		if err := copyDereferenced(root, root, prepared, map[string]bool{}, &budget, &count); err != nil {
			return "", err
		}
		root = prepared
	}
	final := filepath.Join(dest, name)
	if err := promoteDirectory(root, final); err != nil {
		return "", err
	}
	return final, nil
}

// Stable ordering is also used by the platform builder and its manifest tests.
func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
