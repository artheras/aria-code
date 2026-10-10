import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aria_code.workspace import WorkspaceFiles, WorkspaceSecurity


class WorkspaceFilesTests(unittest.TestCase):
    def test_utf8_reads_and_search_ignore_legacy_system_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "hello.txt"
            target.write_text("hello 你好\n", encoding="utf-8")
            original = Path.read_text

            def legacy_default(path, *args, **kwargs):
                kwargs.setdefault("encoding", "cp1252")
                return original(path, *args, **kwargs)

            files = WorkspaceFiles(WorkspaceSecurity(cwd=root))
            with patch.object(Path, "read_text", legacy_default):
                self.assertIn("hello 你好", files.read_file(str(target)).content)
                result = files.search_code("你好", str(root), "*.txt")
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["matches"][0]["content"], "hello 你好")

    def test_security_blocks_system_paths(self):
        security = WorkspaceSecurity()
        self.assertFalse(security.is_safe_path("/etc/passwd"))
        self.assertFalse(security.is_safe_path("/dev/null"))

    def test_remote_scope_is_confined_to_assigned_workspace(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as outside:
            security = WorkspaceSecurity(cwd=workspace, allow_home=False)
            self.assertTrue(security.is_safe_path(Path(workspace) / "src" / "app.py"))
            self.assertFalse(security.is_safe_path(Path(outside) / "secret.txt"))
            self.assertFalse(security.is_safe_path(Path.home() / ".ssh" / "id_ed25519"))

    def test_remote_scope_can_add_an_explicit_read_root(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as shared:
            security = WorkspaceSecurity(
                cwd=workspace,
                allowed_roots=[shared],
                allow_home=False,
            )
            self.assertTrue(security.is_safe_path(Path(shared) / "rules" / "AGENTS.md"))

    def test_read_list_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
            (root / "beta.txt").write_text("alpha beta\n", encoding="utf-8")

            files = WorkspaceFiles(WorkspaceSecurity(cwd=root))
            read = files.read_file(str(root / "alpha.py"))
            self.assertIn("def alpha", read.content)
            self.assertEqual(read.lines, 2)

            listing = files.list_files(str(root), "*.py")
            self.assertEqual(listing["count"], 1)
            self.assertEqual(listing["items"][0]["name"], "alpha.py")

            search = files.search_code("return", str(root), "**/*.py")
            self.assertEqual(search["count"], 1)
            self.assertEqual(search["matches"][0]["file"], "alpha.py")

    def test_symlink_to_blocked_root_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "etc_link"
            try:
                os.symlink("/etc", link)
            except OSError:
                return
            files = WorkspaceFiles(WorkspaceSecurity(cwd=tmp))
            with self.assertRaises(PermissionError):
                files.read_file(str(link / "passwd"))


if __name__ == "__main__":
    unittest.main()
