"""The managed Cloud Run trigger builds the same image as local Docker Compose."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "container-entrypoint.sh"


class ContainerEntrypointTest(unittest.TestCase):
    def run_entrypoint(self, port: str | None) -> str:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            for name in ("aria-relay", "aria-code"):
                command = fake_bin / name
                command.write_text(f'#!/bin/sh\nprintf "%s" "{name} $*"\n', encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            if port is None:
                env.pop("PORT", None)
            else:
                env["PORT"] = port
            result = subprocess.run(
                ["/bin/sh", str(ENTRYPOINT), "--help"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            return result.stdout

    def test_cloud_run_starts_the_go_relay(self) -> None:
        self.assertEqual(
            self.run_entrypoint("8080"),
            "aria-relay ",
        )

    def test_local_container_preserves_cli(self) -> None:
        self.assertEqual(self.run_entrypoint(None), "aria-code --help")


if __name__ == "__main__":
    unittest.main()
