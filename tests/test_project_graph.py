"""The project graph answers: change this, and what else moves?"""

import os
import time
from pathlib import Path

import pytest

from aria_code.runtime import project_graph as pg


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shop"
    write(root, "app/__init__.py", "from .billing import Invoice\n")
    write(root, "app/billing.py", "class Invoice:\n    def total(self):\n        return 1\n\n"
                                  "def refund_policy():\n    return 'none'\n")
    write(root, "app/checkout.py", "from app.billing import Invoice\n\n"
                                   "def checkout():\n    return Invoice().total()\n")
    write(root, "app/api.py", "from . import checkout\n\n"
                              "def handler():\n    return checkout.checkout()\n")
    write(root, "app/reports.py", "import app.billing\n\n"
                                  "def monthly():\n    return app.billing.refund_policy()\n")
    write(root, "tests/test_checkout.py", "from app.checkout import checkout\n\n"
                                          "def test_checkout():\n    assert checkout() == 1\n")
    write(root, "tests/test_api.py", "from app.api import handler\n\n"
                                     "def test_handler():\n    assert handler() == 1\n")
    write(root, "web/src/cart.ts", "import { price } from './price'\nexport const cart = () => price()\n")
    write(root, "web/src/price.ts", "export function price() { return 1 }\n")
    write(root, "web/src/cart.test.ts", "import { cart } from './cart'\ntest('x', () => cart())\n")
    write(root, "web/package.json", "{}\n")
    write(root, "docker-compose.yml", "services:\n  api:\n    build: .\n  worker:\n    build:\n      context: ./worker\n")
    write(root, "worker/jobs.py", "from app.billing import refund_policy\n\n"
                                  "def nightly():\n    return refund_policy()\n")
    return root


@pytest.fixture
def graph(repo):
    pg.clear_cache()
    return pg.load_project_graph(repo)


def test_python_and_typescript_imports_resolve_to_files(graph):
    imports = {path: info["imports"] for path, info in graph.files.items()}
    assert imports["app/checkout.py"] == ["app/billing.py"]
    assert imports["app/api.py"] == ["app/checkout.py"]
    assert imports["app/reports.py"] == ["app/billing.py"]
    assert imports["app/__init__.py"] == ["app/billing.py"]
    assert imports["web/src/cart.ts"] == ["web/src/price.ts"]


def test_changing_a_file_reaches_its_users_their_importers_and_their_tests(graph):
    impact = graph.impact(["app/billing.py"])
    assert {"app/checkout.py", "app/reports.py", "worker/jobs.py"} <= set(impact.direct)
    assert "app/api.py" in impact.indirect
    assert set(impact.tests) == {"tests/test_checkout.py", "tests/test_api.py"}
    assert impact.services == ["api", "worker"]


def test_a_symbol_reaches_only_the_files_that_use_it(graph):
    impact = graph.impact(["refund_policy"])
    assert set(impact.direct) == {"app/reports.py", "worker/jobs.py"}
    assert "tests/test_checkout.py" not in impact.tests

    method = graph.impact(["Invoice.total"])
    assert "app/checkout.py" in method.direct
    assert "app/reports.py" not in method.direct


def test_typescript_tests_and_package_services_are_found(graph):
    impact = graph.impact(["web/src/price.ts"])
    assert impact.direct == ["web/src/cart.ts"]
    assert impact.tests == ["web/src/cart.test.ts"]
    assert impact.services == ["web"]


def test_unknown_targets_are_reported_not_guessed(graph):
    impact = graph.impact(["no_such_thing_anywhere", "app/missing.py"])
    assert impact.unresolved == ["no_such_thing_anywhere", "app/missing.py"]
    assert impact.breadth == "isolated"


def test_the_walk_does_not_pass_through_an_aggregator(tmp_path):
    root = tmp_path / "hub"
    write(root, "core.py", "def compute_value():\n    return 1\n")
    write(root, "user.py", "from core import compute_value\n")
    lines = "".join(f"import mod{i}\n" for i in range(25)) + "import user\n"
    for i in range(25):
        write(root, f"mod{i}.py", f"VALUE_{i} = {i}\n")
    write(root, "main.py", lines)
    write(root, "tests/test_main.py", "import main\n")
    pg.clear_cache()
    impact = pg.load_project_graph(root).impact(["core.py"])
    assert "main.py" in impact.indirect            # reached
    assert "tests/test_main.py" not in impact.tests  # but not walked past


def test_a_saved_graph_is_reused_until_a_file_changes(repo, monkeypatch):
    pg.clear_cache()
    first = pg.load_project_graph(repo)
    pg.clear_cache()

    built = []
    real_build = pg.ProjectGraph.build
    monkeypatch.setattr(pg.ProjectGraph, "build", lambda self, previous=None: built.append(1) or real_build(self, previous))
    reused = pg.load_project_graph(repo)
    assert built == [] and reused.built_at == first.built_at

    target = repo / "app" / "reports.py"
    target.write_text("from app.checkout import checkout\n")
    os.utime(target, (time.time() + 5, time.time() + 5))
    pg.clear_cache()
    rebuilt = pg.load_project_graph(repo)
    assert built == [1]
    assert rebuilt.files["app/reports.py"]["imports"] == ["app/checkout.py"]


def test_the_tool_reports_and_refuses_empty_targets(repo):
    pg.clear_cache()
    result = pg.tool_impact_analysis({"targets": ["refund_policy"], "path": str(repo)})
    assert result["success"]
    assert set(result["data"]["direct_dependents"]) == {"app/reports.py", "worker/jobs.py"}
    assert "Used directly by (2)" in result["data"]["report"]
    assert not pg.tool_impact_analysis({"targets": []})["success"]


def test_the_model_is_offered_the_tool_and_the_user_has_impact(repo, monkeypatch):
    import io
    import sys

    sys.argv = ["aria-code"]
    import aria_code.aria_cli as cli
    from rich.console import Console

    names = {(s.get("function") or s).get("name") for s in cli.LOCAL_TOOL_SCHEMAS}
    assert "impact_analysis" in names and "impact_analysis" in cli.LOCAL_TOOLS

    terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
    terminal.config["_session_workspace_root"] = str(repo)
    out = io.StringIO()
    monkeypatch.setattr(terminal.commands.context, "console", Console(file=out, width=100))
    pg.clear_cache()
    terminal.commands.cmd_impact("app/billing.py")
    assert "Impact of app/billing.py" in out.getvalue()
    assert "tests/test_checkout.py" in out.getvalue()
