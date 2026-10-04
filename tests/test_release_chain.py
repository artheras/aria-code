"""A tag must lead to a publish, in the same run.

release-on-merge created v0.1.0 and v0.2.0 and published neither. Nothing failed:
publish.yml triggers on `push: tags`, and a tag pushed with GITHUB_TOKEN does not
start a new workflow run — GitHub suppresses that to prevent recursion. The
release looked green, the version climbed, and PyPI and npm stayed on 4.4.2.

The fix is to call the publishing workflows from the release run instead of
hoping a tag event arrives. These tests pin the wiring, because it is the kind
that fails by doing nothing.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # YAML 1.1 reads a bare `on:` as the boolean True.
    return doc.get("on") or doc.get(True) or {}


class ReleaseRunPublishes(unittest.TestCase):
    def setUp(self):
        self.release = _load("release-on-merge")
        self.jobs = self.release["jobs"]

    def test_the_release_run_calls_the_publishers(self):
        """Not "a tag exists and something will notice"."""
        called = {j.get("uses") for j in self.jobs.values() if j.get("uses")}
        self.assertIn("./.github/workflows/publish.yml", called)
        self.assertIn("./.github/workflows/build-native-binaries.yml", called)

    def test_publishing_waits_for_the_tag(self):
        for name, job in self.jobs.items():
            if not job.get("uses"):
                continue
            with self.subTest(job=name):
                needs = job.get("needs") or []
                needs = [needs] if isinstance(needs, str) else needs
                self.assertIn("tag-next-patch", needs,
                              "would publish before the tag it publishes exists")

    def test_the_tag_is_passed_explicitly(self):
        """Called workflows see the caller's ref, not the tag, so it must be an
        input — otherwise the publish would read "main" as the version."""
        for name, job in self.jobs.items():
            if not job.get("uses"):
                continue
            with self.subTest(job=name):
                self.assertIn("tag", job.get("with") or {})

    def test_the_binaries_are_published_before_the_dispatcher(self):
        """npm skips an optionalDependency it cannot resolve, silently.

        This used to assert `needs: binaries` on the whole publish job, which
        enforced the ordering by holding *every* registry behind the npm
        binaries. v0.48.0 showed the cost: all five binaries built, `npm
        publish` failed on credentials, and the Python package — which
        contains no npm artifact — was never published either.

        The requirement is npm's alone, so it is enforced inside publish-npm,
        which waits for this version's platform packages to appear before
        publishing the dispatcher.
        """
        self.assertNotIn(
            "binaries", self.jobs["publish"].get("needs") or [],
            "publishing waits on the npm binaries again, which couples PyPI to "
            "an npm failure for no reason",
        )
        publish_yml = yaml.safe_load(
            (WORKFLOWS / "publish.yml").read_text(encoding="utf-8"))
        steps = publish_yml["jobs"]["publish-npm"].get("steps") or []
        names = [str(s.get("name", "")) for s in steps]
        self.assertTrue(
            any("Wait for this version's platform packages" in n for n in names),
            f"nothing orders the dispatcher after the platform packages: {names}",
        )

    def test_secrets_reach_the_called_workflows(self):
        for name, job in self.jobs.items():
            if job.get("uses"):
                with self.subTest(job=name):
                    self.assertEqual(job.get("secrets"), "inherit",
                                     "without this the publish steps skip on an "
                                     "empty token, which is how they skip silently")


class CalledWorkflowsAcceptATag(unittest.TestCase):
    def test_both_are_callable_and_take_a_tag(self):
        for name in ("publish", "build-native-binaries"):
            with self.subTest(workflow=name):
                call = _triggers(_load(name)).get("workflow_call")
                self.assertIsNotNone(call, f"{name}.yml cannot be called")
                self.assertIn("tag", call.get("inputs") or {})

    def test_they_still_work_from_a_pushed_tag(self):
        """A human pushing a tag must keep working — that is how every release
        before this one happened."""
        for name in ("publish", "build-native-binaries"):
            with self.subTest(workflow=name):
                push = _triggers(_load(name)).get("push") or {}
                self.assertIn("v*", push.get("tags") or [])

    def test_nothing_reads_the_ref_without_the_input(self):
        """github.ref_name is the caller's branch when called. Any bare use of it
        would make a call publish "main"."""
        for name in ("publish", "build-native-binaries"):
            text = (WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8")
            bare = [line.strip() for line in text.splitlines()
                    if "github.ref_name" in line and "inputs.tag" not in line
                    # Comments explaining the hazard name it; only live
                    # expressions can cause it.
                    and not line.lstrip().startswith("#")]
            with self.subTest(workflow=name):
                self.assertEqual(bare, [], f"{name}.yml reads ref_name unguarded")

    def test_every_checkout_pins_the_tag(self):
        """The same hazard, in the form the test above cannot see.

        `actions/checkout` with no `ref:` takes the ref that triggered the run.
        On the workflow_call path that is the caller's branch — main at the
        merge commit, one before release-on-merge's version bump — so the jobs
        run against the previous version's tree while claiming to publish the
        new tag.

        v0.50.0 failed there: head be60f8c had pyproject 0.49.0, tag v0.50.0
        pointed at cff7dec with 0.50.0, verify-version compared them and
        failed, and npm, PyPI and the GitHub release were all skipped.

        The assertion above greps for an explicit `github.ref_name`. This one
        is about its *absence*, which leaves nothing to grep — that is why a
        test written for this exact hazard did not catch it.
        """
        for name in ("publish", "build-native-binaries"):
            doc = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))
            for job_id, job in (doc.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    if not str(step.get("uses", "")).startswith("actions/checkout"):
                        continue
                    with self.subTest(workflow=name, job=job_id):
                        ref = (step.get("with") or {}).get("ref")
                        self.assertIsNotNone(
                            ref,
                            f"{name}.yml:{job_id} checks out the triggering ref, "
                            f"which on a call is the caller's branch, not the tag",
                        )
                        self.assertIn("inputs.tag", str(ref))


class NpmDispatcherGate(unittest.TestCase):
    """Exercise the actual release step without contacting npm."""

    def _run_wait(self, npm_available: bool):
        steps = _load("publish")["jobs"]["publish-npm"]["steps"]
        script = next(step["run"] for step in steps
                      if step.get("name") == "Wait for this version's platform packages")
        # Keep the real shell logic, but expire its wait immediately in the
        # missing-package case instead of making the test sleep an hour.
        self.assertIn("+ 3600", script)
        script = script.replace("+ 3600", "+ 0", 1)
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp)
            node = bin_dir / "node"
            node.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -p ]; then echo 0.52.0; "
                "else echo @artheras/aria-code-linux-x64@0.52.0; fi\n"
            )
            npm = bin_dir / "npm"
            npm.write_text(f"#!/bin/sh\nexit {0 if npm_available else 1}\n")
            node.chmod(0o755)
            npm.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}",
                       INPUT_TAG="v0.52.0")
            return subprocess.run(["bash", "-c", script], env=env,
                                  capture_output=True, text=True, timeout=10)

    def test_dispatcher_waits_for_its_platform_packages(self):
        present = self._run_wait(True)
        self.assertEqual(present.returncode, 0, present.stderr)
        missing = self._run_wait(False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Refusing to publish a dispatcher", missing.stdout)


if __name__ == "__main__":
    unittest.main()
