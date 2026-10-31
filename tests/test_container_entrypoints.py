"""Container entry points must name modules that exist.

The package moved under src/aria_code/ and the container definitions were not
updated with it. Dockerfile.relay still did `COPY aria_relay_server.py .` for a
root-level file that has not existed since, so
`docker compose -f docker-compose.prod.yml build relay-server` failed on the
COPY — and docker-compose.yml still ran `python3 aria_daemon.py` and
`python3 aria_relay_client.py` the same way.

Nothing catches this in CI, because building images is not part of the test
run. These checks are cheap and would have.
"""

import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Commands supplied by a dependency rather than by this package.
_DEPENDENCY_COMMANDS = {"uvicorn", "gunicorn", "hypercorn", "celery"}


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class ComposeCommandTests(unittest.TestCase):
    def test_every_compose_command_names_a_real_module(self):
        for compose in ("docker-compose.yml", "docker-compose.prod.yml"):
            path = ROOT / compose
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for module in re.findall(r"python3? -m ([\w.]+)", text):
                with self.subTest(compose=compose, module=module):
                    self.assertTrue(_importable(module), f"{module} is not importable")

    def test_no_compose_command_runs_a_bare_root_script(self):
        # `python3 aria_daemon.py` only worked before the src/ move.
        for compose in ("docker-compose.yml", "docker-compose.prod.yml"):
            path = ROOT / compose
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            stale = re.findall(r"command:\s*python3? ([\w./]+\.py)", text)
            with self.subTest(compose=compose):
                self.assertEqual(stale, [], f"{compose} runs root-level scripts: {stale}")


class DockerfileTests(unittest.TestCase):
    def _dockerfiles(self):
        return [p for p in ROOT.glob("Dockerfile*") if p.is_file()]

    def test_no_dockerfile_copies_a_file_that_does_not_exist(self):
        for path in self._dockerfiles():
            text = path.read_text(encoding="utf-8")
            for source in re.findall(r"^COPY\s+([^\s]+)\s", text, re.M):
                if any(ch in source for ch in "*?[") or source.startswith("--"):
                    continue
                with self.subTest(dockerfile=path.name, source=source):
                    self.assertTrue(
                        (ROOT / source).exists(),
                        f"{path.name} copies {source}, which is not in the repo",
                    )

    def test_every_dockerfile_cmd_is_runnable(self):
        for path in self._dockerfiles():
            text = path.read_text(encoding="utf-8")
            match = re.search(r'^CMD\s+\[(.+?)\]', text, re.M | re.S)
            if not match:
                continue
            argv = [a.strip().strip('"').strip("'") for a in match.group(1).split(",")]
            with self.subTest(dockerfile=path.name, cmd=argv):
                if "-m" in argv:
                    module = argv[argv.index("-m") + 1]
                    self.assertTrue(_importable(module), f"{module} is not importable")
                elif argv[0] in {"sh", "bash"}:
                    self.assertGreater(len(argv), 1, f"{path.name} has no shell script")
                    script = ROOT / argv[1]
                    self.assertTrue(script.is_file(), f"{path.name} runs missing script {argv[1]}")
                elif argv[0].endswith(".py"):
                    self.fail(f"{path.name} runs a bare script: {argv[0]}")
                elif argv[0] in _DEPENDENCY_COMMANDS:
                    # A command a dependency provides (uvicorn, gunicorn). What
                    # matters is that the app it points at can be imported.
                    target = next((a for a in argv[1:] if ":" in a), "")
                    if target:
                        module = target.split(":")[0]
                        self.assertTrue(_importable(module), f"{module} is not importable")
                else:
                    # Our own console script: it must be declared in pyproject.
                    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
                    self.assertIn(
                        f"{argv[0]} =", pyproject,
                        f"{argv[0]} is not a declared console script",
                    )

    def _extras(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10
            try:
                import tomli as tomllib
            except ImportError:
                self.skipTest("no TOML parser on this Python")
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        return tomllib.loads(text)["project"]["optional-dependencies"]

    def test_every_installed_extra_is_declared(self):
        # Dockerfile.review did `pip install ".[service]"` while pyproject had
        # no `service` extra: pip only warns, so the image built without
        # fastapi/uvicorn and its CMD could not start.
        extras = set(self._extras())
        for path in self._dockerfiles():
            text = path.read_text(encoding="utf-8")
            for group in re.findall(r"pip install[^\n]*?\.\[([\w,\s-]+)\]", text):
                for extra in (e.strip() for e in group.split(",")):
                    with self.subTest(dockerfile=path.name, extra=extra):
                        self.assertIn(extra, extras, f"{path.name} installs undeclared extra [{extra}]")

    def test_review_service_extra_carries_the_server_stack(self):
        extras = self._extras()
        names = {re.split(r"[<>=\[ ]", dep, maxsplit=1)[0].lower() for dep in extras["service"]}
        self.assertLessEqual({"fastapi", "uvicorn", "python-multipart"}, names)


if __name__ == "__main__":
    unittest.main()
