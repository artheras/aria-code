"""The per-platform npm packages must be installable and runnable as published.

npm runs no code from these packages — there is no postinstall any more — so
everything that has to be right is in the metadata and the file modes. Each of
these has a concrete failure: a missing +x installs fine and fails with EACCES
on first run; a wrong `os`/`cpu` makes npm download a binary it cannot execute;
a filename without .exe is simply not found on Windows.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import tempfile
import unittest
import shutil

from native_packaging_support import fixture_archive, run_tool

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS_MODULE = ROOT / "npm" / "lib" / "platform.js"


GO_SOURCE = ROOT / "go" / "buildtools" / "packages.go"


class KeysAgreeWithTheDispatcher(unittest.TestCase):
    def test_go_and_javascript_list_the_same_platforms(self):
        """Two lists, one truth. A key in one and not the other ships a package
        the dispatcher never looks for, or looks for one nobody built."""
        js = JS_MODULE.read_text(encoding="utf-8")
        block = re.search(r"PLATFORM_KEYS = Object\.freeze\(\[(.*?)\]\)", js, re.S)
        self.assertIsNotNone(block, "could not find PLATFORM_KEYS in platform.js")
        from_js = tuple(re.findall(r'"([^"]+)"', block.group(1)))
        go = re.search(r'platformKeys = \[\]string\{([^}]*)\}', GO_SOURCE.read_text())
        self.assertIsNotNone(go)
        self.assertEqual(from_js, tuple(re.findall(r'"([^"]+)"', go.group(1))))


def _onedir_archive(tmp: pathlib.Path, name: str, *, windows: bool = False,
                    extra_link: tuple[str, str] | None = None) -> pathlib.Path:
    """A miniature PyInstaller --onedir build, archived the way CI archives it.

    Shaped like the real macOS output: an executable beside _internal/, with a
    framework whose Versions/Current is a directory symlink and a library that
    is a file symlink.
    """
    root = tmp / "src" / name
    if root.parent.exists():
        import shutil
        shutil.rmtree(root.parent)
    internal = root / "_internal"
    exe = root / (f"{name}.exe" if windows else name)
    (internal / "Python.framework" / "Versions" / "3.13").mkdir(parents=True)
    (internal / "Python.framework" / "Versions" / "3.13" / "Python").write_bytes(b"dylib")
    (internal / "base_library.zip").write_bytes(b"zip")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    if not windows:
        os.symlink("3.13", internal / "Python.framework" / "Versions" / "Current")
        os.symlink("Python.framework/Versions/Current/Python", internal / "Python")
        if extra_link:
            os.symlink(extra_link[1], internal / extra_link[0])
    return fixture_archive(root, tmp / "archives" / name, use_zip=windows)


class GeneratedPackages(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "platforms"

    def _build(self, key, mcp=False, **archive):
        name = "aria-code-mcp-bin" if mcp else "aria-code-bin"
        source = _onedir_archive(self.tmp, name, windows=key.startswith("win32"), **archive)
        other = "aria-code-bin" if mcp else "aria-code-mcp-bin"
        peer = _onedir_archive(self.tmp, other, windows=key.startswith("win32"))
        cli, server = (peer, source) if mcp else (source, peer)
        run_tool("packages", "--version", "9.9.9", "--out", self.out,
                 "--binary", f"{key}={cli}", "--mcp", f"{key}={server}")
        return self.out / f"aria-code-{'mcp-' if mcp else ''}{key}"

    def test_windows_binaries_get_exe_and_nothing_else_does(self):
        win = self._build("win32-x64")
        self.assertTrue((win / "bin" / "aria-code-bin" / "aria-code-bin.exe").is_file())
        for key in ("darwin-arm64", "linux-x64"):
            pkg = self._build(key)
            self.assertTrue((pkg / "bin" / "aria-code-bin" / "aria-code-bin").is_file())
            self.assertFalse((pkg / "bin" / "aria-code-bin" / "aria-code-bin.exe").exists())

    def test_the_binary_sits_beside_its_libraries(self):
        """--onedir: the executable finds _internal/ next to itself."""
        pkg = self._build("darwin-arm64")
        app = pkg / "bin" / "aria-code-bin"
        self.assertTrue((app / "_internal" / "base_library.zip").is_file())

    def test_the_binary_is_executable(self):
        pkg = self._build("linux-x64")
        mode = (pkg / "bin" / "aria-code-bin" / "aria-code-bin").stat().st_mode
        for bit in (stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH):
            self.assertTrue(mode & bit, "binary would install without +x")

    def test_no_symlink_reaches_the_npm_package(self):
        """npm tarballs cannot be relied on to carry symlinks; the real macOS
        build has 22. Each must arrive as a real file or directory."""
        pkg = self._build("darwin-arm64")
        internal = pkg / "bin" / "aria-code-bin" / "_internal"
        self.assertEqual([p for p in pkg.rglob("*") if p.is_symlink()], [])
        self.assertEqual((internal / "Python").read_bytes(), b"dylib")
        self.assertTrue((internal / "Python.framework" / "Versions" / "Current").is_dir())

    def test_a_symlink_out_of_the_build_is_refused(self):
        for target in ("/etc/passwd", "../../../../etc/passwd"):
            with self.subTest(target=target), self.assertRaises(SystemExit):
                self._build("linux-x64", extra_link=("libssl.so", target))

    def test_os_and_cpu_let_npm_refuse_the_wrong_machine(self):
        for key, want_os, want_cpu in (
            ("darwin-arm64", "darwin", "arm64"),
            ("linux-x64", "linux", "x64"),
            ("win32-x64", "win32", "x64"),
        ):
            with self.subTest(key=key):
                meta = json.loads((self._build(key) / "package.json").read_text())
                self.assertEqual(meta["os"], [want_os])
                self.assertEqual(meta["cpu"], [want_cpu])

    def test_package_name_matches_what_the_dispatcher_requires(self):
        meta = json.loads((self._build("darwin-arm64") / "package.json").read_text())
        self.assertEqual(meta["name"], "@artheras/aria-code-darwin-arm64")
        self.assertEqual(meta["version"], "9.9.9")
        self.assertEqual(meta["files"], ["bin/"])

    def test_the_layout_matches_the_dispatchers_require_path(self):
        js = JS_MODULE.read_text(encoding="utf-8")
        self.assertIn("/bin/${name}/${binaryName(platform, name)}", js)

    def test_the_mcp_binary_has_its_own_package(self):
        cli = self._build("darwin-arm64")
        mcp = self._build("darwin-arm64", mcp=True)
        self.assertFalse((cli / "bin" / "aria-code-mcp-bin").exists())
        self.assertTrue((mcp / "bin" / "aria-code-mcp-bin" / "aria-code-mcp-bin").is_file())
        self.assertEqual(json.loads((mcp / "package.json").read_text())["name"],
                         "@artheras/aria-code-mcp-darwin-arm64")

    def test_an_archive_of_the_wrong_build_is_refused(self):
        source = _onedir_archive(self.tmp, "aria-code-mcp-bin")
        with self.assertRaises(SystemExit):
            run_tool("packages", "--version", "9.9.9", "--out", self.out,
                     "--binary", f"linux-x64={source}", "--mcp", f"linux-x64={source}")

    def test_rebuilding_replaces_rather_than_accumulates(self):
        pkg = self._build("linux-x64")
        stale = pkg / "bin" / "aria-code-mcp-bin"
        stale.write_bytes(b"stale")
        pkg = self._build("linux-x64")
        self.assertFalse(stale.exists(),
                         "a stale binary from a previous build would be published")


class ArgumentHandling(unittest.TestCase):
    def test_an_unknown_platform_key_is_refused(self):
        with self.assertRaises(SystemExit):
            run_tool("packages", "--binary", "sunos-x64=/dev/null")

    def test_a_missing_file_is_refused_rather_than_packaged_empty(self):
        with self.assertRaises(SystemExit):
            run_tool("packages", "--binary", "linux-x64=/nonexistent/aria-code-bin")

    def test_a_pair_without_an_equals_sign_is_refused(self):
        with self.assertRaises(SystemExit):
            run_tool("packages", "--binary", "linux-x64")


if __name__ == "__main__":
    unittest.main()
