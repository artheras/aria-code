"""A release must not be able to leave a tag without its commit.

v0.51.0 is a tag pointing at a commit that is not on main. It got there like
this:

  1. #58 merged. The release job checked out, bumped to 0.51.0, committed and
     tagged.
  2. #59 merged 24 seconds later, moving main.
  3. The job pushed `HEAD:main` and `refs/tags/v0.51.0` in one command. git
     pushes each refspec independently, so the branch was rejected as a
     non-fast-forward and **the tag landed anyway**.
  4. The next run computed 0.51.0 again — main's version was still 0.50.0,
     because the bump never landed — and refused, because the tag existed.

The pipeline was then stuck: every subsequent release would compute the same
version and refuse. The step's own comment claimed the two refs went
"together", which is what `--atomic` means and what the command did not do.

These tests run the real step against real repositories, because both the
atomicity and the rebase are behaviours of git rather than of the YAML. A
test that grepped for "--atomic" would pass without either working.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-on-merge.yml"


def _commit_and_tag_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == "Commit and tag":
                return step["run"]
    raise AssertionError("no 'Commit and tag' step in release-on-merge.yml")


def _git(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60)


class _Sandbox(unittest.TestCase):
    """A bare 'origin' and a clone standing in for the runner's checkout."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.other = self.tmp / "other"

        _git("init", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        _git("clone", str(self.origin), str(self.work), cwd=self.tmp)
        for key, val in (("user.name", "t"), ("user.email", "t@e")):
            _git("config", key, val, cwd=self.work)
        (self.work / "pyproject.toml").write_text('version = "0.50.0"\n')
        (self.work / "npm").mkdir()
        (self.work / "npm" / "package.json").write_text("{}\n")
        (self.work / "src" / "aria_code").mkdir(parents=True)
        (self.work / "src" / "aria_code" / "_version.py").write_text("x = 1\n")
        (self.work / "CHANGELOG.md").write_text("# changelog\n")
        for name in ("README.md", "README_CN.md"):
            (self.work / name).write_text("# release badge fixture\n")
        _git("add", "-A", cwd=self.work)
        _git("commit", "-m", "base", cwd=self.work)
        _git("push", "origin", "main", cwd=self.work)

    def run_step(self, version: str = "0.51.0") -> subprocess.CompletedProcess:
        script = _commit_and_tag_script()
        # The step interpolates two GitHub expressions; substitute them the way
        # Actions would, leaving the shell logic untouched.
        script = script.replace("${{ steps.next.outputs.version }}", version)
        script = script.replace("${{ github.ref_name }}", "main")
        # Something has to be staged for the commit to exist.
        (self.work / "pyproject.toml").write_text(f'version = "{version}"\n')
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=self.work, capture_output=True, text=True, timeout=120,
        )

    def move_main(self, message: str = "someone else's merge") -> None:
        """Land an unrelated commit on origin/main, as a merge would."""
        _git("clone", str(self.origin), str(self.other), cwd=self.tmp)
        for key, val in (("user.name", "o"), ("user.email", "o@e")):
            _git("config", key, val, cwd=self.other)
        (self.other / "unrelated.txt").write_text("hello\n")
        _git("add", "-A", cwd=self.other)
        _git("commit", "-m", message, cwd=self.other)
        _git("push", "origin", "main", cwd=self.other)

    def remote_tags(self) -> list[str]:
        out = _git("tag", "--list", cwd=self.origin).stdout
        return out.split()


class TheHappyPathStillWorks(_Sandbox):
    def test_the_commit_and_the_tag_both_land(self) -> None:
        proc = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stderr[-900:])
        self.assertIn("v0.51.0", self.remote_tags())
        head = _git("log", "--format=%s", "-1", "main", cwd=self.origin).stdout
        self.assertIn("release: v0.51.0", head)


class ARaceDoesNotLeaveAnOrphanTag(_Sandbox):
    """The exact v0.51.0 sequence."""

    def test_a_merge_landing_mid_step_does_not_strand_a_tag(self) -> None:
        self.move_main()
        proc = self.run_step()

        # Either the step recovers and lands both, or it fails and lands
        # neither. What it must never do is land the tag alone.
        tags = self.remote_tags()
        if proc.returncode == 0:
            self.assertIn("v0.51.0", tags)
            on_main = _git("merge-base", "--is-ancestor", "v0.51.0", "main",
                           cwd=self.origin)
            self.assertEqual(
                on_main.returncode, 0,
                "the tag landed but its commit is not on main — exactly the "
                "state v0.51.0 is in",
            )
        else:
            self.assertNotIn(
                "v0.51.0", tags,
                "the push failed but the tag landed anyway; the next release "
                "will compute the same version and refuse, which is how the "
                "pipeline got stuck",
            )

    def test_it_recovers_rather_than_giving_up(self) -> None:
        # Recovering is the point: a merge arriving in the window is ordinary,
        # not an error, and losing a release to it would mean losing the
        # change that merge contained from the version count too.
        self.move_main()
        proc = self.run_step()
        self.assertEqual(
            proc.returncode, 0,
            f"a single concurrent merge lost the release:\n{proc.stderr[-900:]}",
        )
        self.assertIn("unrelated.txt", _git("ls-tree", "--name-only", "main",
                                            cwd=self.origin).stdout)

    def test_the_rebased_commit_keeps_the_version(self) -> None:
        # Only a release touches the version files and releases are
        # serialised, so whatever arrived in the window left them alone.
        self.move_main()
        self.assertEqual(self.run_step().returncode, 0)
        content = _git("show", "main:pyproject.toml", cwd=self.origin).stdout
        self.assertIn('version = "0.51.0"', content)


class TheStepAsksGitForAtomicity(unittest.TestCase):
    def test_the_push_is_atomic(self) -> None:
        # The behavioural tests above are the real check; this names the
        # mechanism so a rewrite that drops it is obvious in review.
        self.assertRegex(_commit_and_tag_script(), r"git push\s+--atomic")


if __name__ == "__main__":
    unittest.main()
