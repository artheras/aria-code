"""@file reference candidates: fuzzy, workspace-wide, ignoring clutter."""
import subprocess

from aria_code.apps.cli import file_references as fr


def make(tmp_path):
    for rel in ("README.md", "src/api/auth.py", "src/api/users.py", "docs/guide notes.md",
                "node_modules/pkg/index.js", "build/out.o"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    fr._cache.clear()
    return tmp_path


def test_bare_at_lists_top_level_folders_first(tmp_path):
    root = make(tmp_path)
    out = fr.reference_candidates("see @", root)
    assert out[0] == "see @docs/" and out[-1] == "see @README.md"
    assert "see @node_modules/" not in out and "see @build/" not in out


def test_fuzzy_finds_nested_file_by_fragment(tmp_path):
    root = make(tmp_path)
    assert fr.reference_candidates("x @auth", root)[0] == "x @src/api/auth.py"
    assert fr.reference_candidates("x @rdme", root)[0] == "x @README.md"


def test_folder_prefix_lists_its_children_and_quotes_spaces(tmp_path):
    root = make(tmp_path)
    assert fr.reference_candidates("@src/api/", root) == ["@src/api/auth.py", "@src/api/users.py"]
    assert fr.reference_candidates("@guide", root)[0] == '@"docs/guide notes.md"'


def test_no_token_no_candidates(tmp_path):
    root = make(tmp_path)
    assert fr.reference_candidates("mail me@x", root) == []
    assert fr.reference_candidates("plain text", root) == []


def test_git_workspace_respects_gitignore(tmp_path):
    root = make(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text("secret.txt\n")
    (root / "secret.txt").write_text("x")
    fr._cache.clear()
    assert "secret.txt" not in fr.workspace_files(root)
    assert "src/api/auth.py" in fr.workspace_files(root)
