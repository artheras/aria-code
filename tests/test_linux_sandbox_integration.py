"""Execute actual scripts, not command-text patterns, inside bubblewrap."""
import os
import shlex
import sys
from pathlib import Path

import pytest

from aria_code.safety import sandbox
from aria_code.apps.cli.tools.system_tools import tool_run_command

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not sandbox.available(),
                                reason="needs Linux bubblewrap")


def test_unlisted_script_cannot_write_outside_workspace_or_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    outside = Path.home() / f".aria-sandbox-probe-{os.getpid()}"
    script = tmp_path / "probe.py"
    script.write_text(
        "import socket\nfrom pathlib import Path\n"
        "Path('inside.txt').write_text('ok')\n"
        f"try: Path({str(outside)!r}).write_text('escaped')\n"
        "except OSError: print('write denied')\n"
        "try: socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
        "except OSError: print('network denied')\n"
    )
    result = tool_run_command({"command": f"{shlex.quote(sys.executable)} probe.py", "policy": "full",
                               "cwd": str(tmp_path), "_workspace": str(tmp_path),
                               "permission_mode": "workspace-write", "network_enabled": False}, has_rich=False)
    try:
        assert result["data"]["exit_code"] == 0, result
        assert (tmp_path / "inside.txt").read_text() == "ok"
        assert not outside.exists()
        assert "write denied" in result["data"]["stdout"]
        assert "network denied" in result["data"]["stdout"]
    finally:
        outside.unlink(missing_ok=True)
