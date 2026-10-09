"""Offline checks for the dependency-free Unix installer.

Releases ship a PyInstaller --onedir build as aria-code-<os>-<arch>.tar.gz;
releases before that switch shipped a single file named aria-code-<os>-<arch>.
The installer must handle both, because ARIA_CODE_VERSION can pin either.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
VERSION_SCRIPT = '''#!/bin/sh
case "$1" in
  --version) echo v0.55.0 ;;
  --help)
    [ -z "${ARIA_TEST_FAIL_STARTUP:-}" ] || exit 1
    case "$0" in */releases/*/aria-code-bin/aria-code-bin) ;; *) exit 2 ;; esac
    [ -z "${ARIA_TEST_RELEASES:-}" ] || printf '%s\\n' "$0" >> "$ARIA_TEST_RELEASES"
    echo 'usage: aria-code'
    ;;
  *) exit 2 ;;
esac
'''


class NativeInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="aria-installer-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.release = self.root / "release"
        self.release.mkdir()

    def publish_onedir(self) -> Path:
        build = self.root / "build" / "aria-code-bin"
        (build / "_internal").mkdir(parents=True)
        (build / "_internal" / "base_library.zip").write_bytes(b"zip")
        exe = build / "aria-code-bin"
        exe.write_text(VERSION_SCRIPT, encoding="utf-8")
        exe.chmod(0o755)
        archive = self.release / "aria-code-macos-arm64.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(build, arcname="aria-code-bin")
        return archive

    def publish_single_file(self) -> Path:
        binary = self.release / "aria-code-macos-arm64"
        binary.write_text(VERSION_SCRIPT, encoding="utf-8")
        binary.chmod(0o755)
        return binary

    def run_installer(self, asset: Path, *, valid_checksum: bool = True, fail_startup: bool = False) -> subprocess.CompletedProcess[str]:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest() if valid_checksum else "0" * 64
        (self.release / "SHA256SUMS").write_text(f"{digest}  {asset.name}\n", encoding="utf-8")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "uname").write_text(
            "#!/bin/sh\ncase \"$1\" in -s) echo Darwin ;; -m) echo arm64 ;; esac\n",
            encoding="utf-8",
        )
        (fake_bin / "curl").write_text(
            '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\n'
            'out=$2; shift 2; url=$1\ncp "$ARIA_TEST_RELEASE/${url##*/}" "$out"\n',
            encoding="utf-8",
        )
        for command in ("uname", "curl"):
            (fake_bin / command).chmod(0o755)
        env = os.environ.copy()
        env.update(
            HOME=str(self.root),
            SHELL="/bin/zsh",
            PATH=f"{fake_bin}:{env['PATH']}",
            ARIA_TEST_RELEASE=str(self.release),
            ARIA_CODE_VERSION="v0.55.0",
            ARIA_TEST_FAIL_STARTUP="1" if fail_startup else "",
            ARIA_TEST_RELEASES=str(self.root / "startup-probes.txt"),
        )
        return subprocess.run(["/bin/sh", str(INSTALLER)], env=env, text=True, capture_output=True)

    @property
    def command(self) -> Path:
        return self.root / ".local/bin/aria-code"

    def test_installs_the_onedir_build_and_links_it_onto_path(self) -> None:
        result = self.run_installer(self.publish_onedir())
        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.command.resolve().parent
        self.assertTrue((app / "_internal" / "base_library.zip").is_file(),
                        "the executable's libraries must sit beside it")
        self.assertTrue(self.command.is_symlink())
        self.assertEqual(self.command.resolve(), (app / "aria-code-bin").resolve())
        self.assertEqual((self.root / "startup-probes.txt").read_text().strip(), str(self.command.resolve()))
        run = subprocess.run([str(self.command), "--version"], capture_output=True, text=True)
        self.assertEqual(run.stdout.strip(), "v0.55.0")
        alias = self.command.with_name("aria")
        self.assertTrue(alias.is_file())
        run_alias = subprocess.run([str(alias), "code", "--version"], capture_output=True, text=True)
        self.assertEqual(run_alias.stdout.strip(), "v0.55.0")
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', (self.root / ".zprofile").read_text())

    def test_reinstalling_replaces_the_previous_build(self) -> None:
        archive = self.publish_onedir()
        self.assertEqual(self.run_installer(archive).returncode, 0)
        old_app = self.command.resolve().parent
        stale = old_app / "_internal/stale.so"
        stale.write_bytes(b"old")
        result = self.run_installer(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.command.resolve().parent, old_app)
        self.assertFalse((self.command.resolve().parent / "_internal/stale.so").exists())
        self.assertTrue(stale.exists(), "the previous release should remain available for rollback")

    def test_failed_update_keeps_the_previous_build_runnable(self) -> None:
        archive = self.publish_onedir()
        self.assertEqual(self.run_installer(archive).returncode, 0)
        old_target = self.command.resolve()
        result = self.run_installer(archive, valid_checksum=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.command.resolve(), old_target)
        run = subprocess.run([str(self.command), "--version"], capture_output=True, text=True)
        self.assertEqual(run.stdout.strip(), "v0.55.0")

    def test_replaces_a_single_file_install_from_before_onedir(self) -> None:
        self.command.parent.mkdir(parents=True)
        self.command.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        result = self.run_installer(self.publish_onedir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.command.is_symlink())

    def test_failed_startup_check_keeps_the_previous_cli_and_removes_the_new_build(self) -> None:
        archive = self.publish_onedir()
        self.assertEqual(self.run_installer(archive).returncode, 0)
        old_target = self.command.resolve()
        old_alias = self.command.with_name("aria").read_bytes()
        old_profile = (self.root / ".zprofile").read_bytes()
        releases = self.root / ".local/share/aria-code/releases"
        old_releases = set(releases.iterdir())
        result = self.run_installer(archive, fail_startup=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("startup check", result.stderr)
        self.assertEqual(self.command.resolve(), old_target)
        self.assertEqual(self.command.with_name("aria").read_bytes(), old_alias)
        self.assertEqual((self.root / ".zprofile").read_bytes(), old_profile)
        self.assertEqual(set(releases.iterdir()), old_releases)

    def test_a_release_from_before_onedir_still_installs(self) -> None:
        result = self.run_installer(self.publish_single_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.command.is_file())
        self.assertFalse(self.command.is_symlink())
        alias = self.command.with_name("aria")
        self.assertTrue(alias.is_file())
        run_alias = subprocess.run([str(alias), "code", "--version"], capture_output=True, text=True)
        self.assertEqual(run_alias.stdout.strip(), "v0.55.0")

    def test_rejects_checksum_mismatch_before_installing(self) -> None:
        for publish in (self.publish_onedir, self.publish_single_file):
            with self.subTest(asset=publish.__name__):
                result = self.run_installer(publish(), valid_checksum=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("checksum mismatch", result.stderr)
                self.assertFalse(self.command.exists())
                self.assertFalse(self.command.with_name("aria").exists())
                self.assertFalse((self.root / ".local/share/aria-code").exists())

    def test_a_release_without_this_platform_says_so(self) -> None:
        other = self.release / "aria-code-linux-x64.tar.gz"
        other.write_bytes(b"x")
        result = self.run_installer(other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no build for macos-arm64", result.stderr)


class WindowsInstallerMatchesTheRelease(unittest.TestCase):
    """No PowerShell on the macOS and Linux CI runners, so these are static:
    the names it downloads and the layout it expects must be the ones the
    release workflow produces."""

    def setUp(self) -> None:
        self.script = (INSTALLER.parent / "install.ps1").read_text(encoding="utf-8")

    def test_downloads_the_onedir_zip_and_falls_back_to_the_old_exe(self) -> None:
        self.assertIn("$asset = 'aria-code-windows-x64'", self.script)
        self.assertIn('$file = "$asset.zip"', self.script)
        self.assertIn('$file = "$asset.exe"', self.script)
        self.assertLess(self.script.index('"$asset.zip"'), self.script.index('"$asset.exe"'),
                        "the onedir build must be preferred")

    def test_keeps_the_libraries_beside_the_executable(self) -> None:
        self.assertIn("Join-Path $build 'aria-code-bin.exe'", self.script)
        self.assertIn("Copy-Item -Recurse (Join-Path $build '_internal') (Join-Path $stage '_internal')", self.script)
        self.assertIn("$libraries = Join-Path $installDir '_internal'", self.script)
        self.assertIn("Copy-Item (Join-Path $stage 'aria-code.exe') (Join-Path $stage 'aria.exe')", self.script)

    def test_verifies_before_replacing_anything(self) -> None:
        checked = self.script.index("Checksum mismatch")
        self.assertLess(checked, self.script.index("Move-Item $current (Join-Path $backup $name)"))
        self.assertLess(self.script.index("& $exe --version"),
                        self.script.index("Move-Item $current (Join-Path $backup $name)"))
        self.assertIn("Move-Item $previous $current", self.script)


if __name__ == "__main__":
    unittest.main()
