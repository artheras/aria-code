"""Which project rule files reach the model.

Only ARIA.md and CLAUDE.md were read, so a repository set up for Codex and
other agents through AGENTS.md gave Aria none of its rules.
"""

from __future__ import annotations

import pathlib

import pytest

from aria_code.apps.cli.helpers import _load_project_context


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "code" / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(root)
    return root


def test_agents_md_is_read(repo):
    (repo / "AGENTS.md").write_text("Run tests with make test.")
    assert "Run tests with make test." in _load_project_context()


def test_agents_and_aria_in_one_directory_are_both_read(repo):
    (repo / "AGENTS.md").write_text("shared rule")
    (repo / "ARIA.md").write_text("aria rule")
    text = _load_project_context()
    assert "shared rule" in text and "aria rule" in text


def test_the_override_stands_in_for_agents_md(repo):
    (repo / "AGENTS.md").write_text("normal rule")
    (repo / "AGENTS.override.md").write_text("override rule")
    text = _load_project_context()
    assert "override rule" in text and "normal rule" not in text


def test_claude_md_only_when_nothing_else_is_there(repo):
    (repo / "CLAUDE.md").write_text("claude rule")
    assert "claude rule" in _load_project_context()
    (repo / "AGENTS.md").write_text("agents rule")
    text = _load_project_context()
    assert "agents rule" in text and "claude rule" not in text


def test_the_same_text_twice_is_read_once(repo):
    (repo / "AGENTS.md").write_text("same rule")
    (repo / "ARIA.md").write_text("same rule\n")
    assert _load_project_context().count("same rule") == 1


def test_the_walk_stops_at_the_repository_root(repo):
    (repo.parent / "AGENTS.md").write_text("another project's rule")
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("package rule")
    (repo / "AGENTS.md").write_text("repo rule")
    import os
    os.chdir(sub)
    text = _load_project_context()
    assert "another project's rule" not in text
    assert text.index("package rule") < text.index("repo rule")


def test_the_project_gets_the_budget_before_the_global_profile(repo):
    from aria_code.account_session import profile_path
    profile = profile_path()
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("g" * 20000)
    (repo / "AGENTS.md").write_text("project rule")
    text = _load_project_context()
    assert "project rule" in text
    assert text.index("project rule") < text.index("ggg")
