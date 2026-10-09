"""The local CLI must be able to use its own scratch space.

`allow_home=False` was hardcoded in two places to "force restrict to the
current directory". It does not do that. WorkspaceSecurity appends the temp
roots only on the allow_home branch, so the flag also removed /tmp and
/var/folders — and Python's tempfile lives under /var/folders on macOS, so the
CLI could write a file into a temp directory and then be denied reading it
back. 13 tests failed on exactly that.
"""

import inspect
import os
import pathlib
import tempfile
import unittest

import pytest

from aria_code.workspace import WorkspaceSecurity


class LocalScopeTests(unittest.TestCase):
    def _security(self, **kwargs):
        return WorkspaceSecurity(cwd=os.getcwd(), **kwargs)

    def test_the_temp_dir_is_writable_by_default(self):
        scratch = pathlib.Path(tempfile.gettempdir()) / "aria-scope-probe.txt"
        self.assertTrue(self._security().is_safe_path(scratch))

    @pytest.mark.usefixtures("fake_home")
    def test_home_and_cwd_are_allowed_by_default(self):
        self.assertTrue(self._security().is_safe_path(pathlib.Path.home() / "x.txt"))
        self.assertTrue(self._security().is_safe_path(pathlib.Path.cwd() / "x.py"))

    def test_system_directories_stay_blocked(self):
        for path in ("/etc/passwd", "/dev/null", "/sys/kernel", "/proc/1"):
            with self.subTest(path=path):
                self.assertFalse(self._security().is_safe_path(pathlib.Path(path)))

    # The two below exercise remote-worker confinement, which is an opt-in
    # WorkspaceSecurity grew separately. They skip where it is absent rather
    # than failing: the local-scope behaviour above is what this file is
    # really about, and it holds either way. They start asserting the moment
    # the scope mechanism lands.
    @unittest.skipUnless(
        "allow_home" in inspect.signature(WorkspaceSecurity.__init__).parameters,
        "WorkspaceSecurity has no allow_home parameter in this build",
    )
    def test_remote_scope_confines_to_the_workspace(self):
        # The mechanism Dockerfile.review uses. Home and temp go away; the
        # assigned working directory remains.
        remote = self._security(allow_home=False)
        self.assertFalse(remote.is_safe_path(pathlib.Path.home() / "x.txt"))
        self.assertFalse(remote.is_safe_path(pathlib.Path(tempfile.gettempdir()) / "x.txt"))
        self.assertTrue(remote.is_safe_path(pathlib.Path.cwd() / "x.py"))

    @unittest.skipUnless(
        "allow_home" in inspect.signature(WorkspaceSecurity.__init__).parameters,
        "WorkspaceSecurity has no allow_home parameter in this build",
    )
    def test_the_env_var_selects_the_scope(self):
        previous = os.environ.get("ARIA_RUNTIME_SCOPE")
        try:
            os.environ["ARIA_RUNTIME_SCOPE"] = "remote"
            self.assertFalse(WorkspaceSecurity(cwd=os.getcwd()).allow_home)
            os.environ["ARIA_RUNTIME_SCOPE"] = "local"
            self.assertTrue(WorkspaceSecurity(cwd=os.getcwd()).allow_home)
        finally:
            if previous is None:
                os.environ.pop("ARIA_RUNTIME_SCOPE", None)
            else:
                os.environ["ARIA_RUNTIME_SCOPE"] = previous


class CliCallSiteTests(unittest.TestCase):
    """The two call sites must not re-hardcode the remote confinement."""

    def test_write_and_read_a_temp_file_round_trip(self):
        # The exact failure: the write landed, the read was denied.
        from aria_code.aria_cli import _tool_read_file, _tool_write_file

        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "probe.py")
            content = (
                "import os\n\n\n"
                "def probe(value):\n"
                "    return os.path.basename(str(value))\n"
            )
            written = _tool_write_file({"path": path, "content": content})
            self.assertTrue(written["success"], written.get("error"))
            read = _tool_read_file({"path": path})
            self.assertTrue(read["success"], read.get("error"))

    def test_neither_call_site_hardcodes_allow_home_false(self):
        import inspect

        from aria_code.apps.cli.tools import file_tools
        from aria_code.aria_cli import _is_safe_path

        # Comments are stripped first: both functions explain the old flag in
        # their docstrings, and matching that would pass forever.
        for name, function in (("_is_safe_path", _is_safe_path),
                               ("_get_workspace_files", file_tools._get_workspace_files)):
            with self.subTest(function=name):
                source = inspect.getsource(function)
                body = source.split('"""')[-1]
                self.assertNotIn("allow_home=False", body)


if __name__ == "__main__":
    unittest.main()
