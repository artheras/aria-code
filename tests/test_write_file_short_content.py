"""A short file is not a placeholder: write_file rejects what the content says, not how long it is."""
import pytest

from aria_code.apps.cli.tools.write_tools import tool_write_file


def write(tmp_path, name, content):
    return tool_write_file({"path": str(tmp_path / name), "content": content, "policy": "balanced", "_skip_confirm": True})


@pytest.mark.parametrize("name,content", [
    ("route.txt", "RRDDRRDRRUU\n"),          # an answer file
    ("VERSION", "0.1.0"),                    # a version file
    ("hist.csv", "token,count\na,1\n"),      # a tiny CSV
    ("a.py", "x = 1"),                       # a one-line module
    (".gitignore", "*.pyc\n"),
    ("flag.txt", "yes"),
    ("note.txt", "pass the salt"),           # begins like a placeholder, is not one
    ("todo.md", "- buy milk\n"),
])
def test_real_short_content_is_written(tmp_path, name, content):
    result = write(tmp_path, name, content)
    assert result["success"], result
    assert (tmp_path / name).read_text() == content


@pytest.mark.parametrize("content", [
    "pass", "...", "…", "TODO", "TBD", "# TODO", "# implement", "// TODO: write this", "your code here",
    "Insert code here", "[code]", "placeholder", "Lorem ipsum",
])
def test_placeholder_content_is_still_rejected(tmp_path, content):
    result = write(tmp_path, "stub.py", content)
    assert not result["success"] and "placeholder" in result["error"]
    assert not (tmp_path / "stub.py").exists()


def test_a_long_comment_that_mentions_todo_is_real_content(tmp_path):
    text = "# TODO list for the release\n" + "# - item\n" * 10
    assert write(tmp_path, "notes.py", text)["success"]


def test_empty_content_is_still_a_missing_parameter(tmp_path):
    assert "Missing 'content'" in write(tmp_path, "empty.txt", "")["error"]


def test_whitespace_only_content_is_rejected(tmp_path):
    result = write(tmp_path, "blank.txt", "   \n  ")
    assert not result["success"] and "empty" in result["error"]
    assert not (tmp_path / "blank.txt").exists()
