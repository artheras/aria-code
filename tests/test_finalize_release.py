"""The release must stay draft until every install channel is ready."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    "aria-code-macos-arm64.tar.gz", "aria-code-macos-x64.tar.gz",
    "aria-code-linux-arm64.tar.gz", "aria-code-linux-x64.tar.gz",
    "aria-code-windows-x64.zip", "aria-code-mcp-macos-arm64.tar.gz",
    "aria-code-mcp-macos-x64.tar.gz", "aria-code-mcp-linux-arm64.tar.gz",
    "aria-code-mcp-linux-x64.tar.gz", "aria-code-mcp-windows-x64.zip",
)


class FinalizeReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aria-finalize-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        fake_bin = self.tmp / "bin"
        fake_bin.mkdir()
        (fake_bin / "gh").write_text(
            "#!/bin/sh\n"
            'if [ "$1 $2" = "release download" ]; then\n'
            '  while [ "$1" != "--dir" ]; do shift; done\n'
            '  cp "$ARIA_TEST_MANIFEST" "$2/SHA256SUMS"\n'
            'elif [ "$1 $2" = "release view" ]; then\n'
            '  case " $* " in\n'
            '    *" .assets[].name "*) cat "$ARIA_TEST_ASSETS" ;;\n'
            '    *) echo true ;;\n'
            '  esac\n'
            'elif [ "$1 $2" = "release edit" ]; then\n'
            '  echo published >> "$ARIA_TEST_LOG"\n'
            'else exit 1; fi\n',
            encoding="utf-8",
        )
        for name in ("npm", "curl"):
            (fake_bin / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "node").write_text(
            '#!/bin/sh\ncat "$ARIA_TEST_PACKAGES"\n', encoding="utf-8"
        )
        for file in fake_bin.iterdir():
            file.chmod(0o755)
        (self.tmp / "manifest").write_text(
            "".join(f"{'a' * 64}  {asset}\n" for asset in ASSETS), encoding="utf-8"
        )
        package = json.loads((ROOT / "npm/package.json").read_text(encoding="utf-8"))
        (self.tmp / "packages").write_text(
            "".join(f"{name}@{version}\n" for name, version in package["optionalDependencies"].items()),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{fake_bin}:{self.env['PATH']}",
            ARIA_TEST_MANIFEST=str(self.tmp / "manifest"),
            ARIA_TEST_ASSETS=str(self.tmp / "assets"),
            ARIA_TEST_LOG=str(self.tmp / "log"),
            ARIA_TEST_PACKAGES=str(self.tmp / "packages"),
        )

    def run_finalizer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "scripts/finalize_release.sh", "v0.67.0"], cwd=ROOT,
            env=self.env, capture_output=True, text=True,
        )

    def test_missing_archive_does_not_publish(self) -> None:
        (self.tmp / "assets").write_text("\n".join(ASSETS[:-1]), encoding="utf-8")
        result = self.run_finalizer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is missing", result.stderr)
        self.assertFalse((self.tmp / "log").exists())

    def test_complete_release_is_published(self) -> None:
        (self.tmp / "assets").write_text("\n".join(ASSETS), encoding="utf-8")
        result = self.run_finalizer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.tmp / "log").read_text(), "published\n")


if __name__ == "__main__":
    unittest.main()
