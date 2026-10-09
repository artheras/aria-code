from aria_code.workspace.verify import VerificationPlanner


def project(tmp_path, test_source):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(test_source)
    return VerificationPlanner(tmp_path)


def test_standard_library_project_is_verified_without_installing_pytest(tmp_path):
    planner = project(tmp_path, "import unittest\nclass Tests(unittest.TestCase):\n def test_one(self): pass\n")
    assert planner.infer(["app.py"]).commands == ["python3 -m py_compile app.py", "python3 -m unittest discover -s tests -v"]
    assert planner.infer(["README.md"]).commands == ["python3 -m unittest discover -s tests -v"]


def test_explicit_pytest_configuration_takes_precedence(tmp_path):
    planner = project(tmp_path, "from unittest import TestCase\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    assert planner.infer(["app.py"]).commands[-1] == "python3 -m pytest -q"


def test_mixed_suites_do_not_drop_plain_pytest_functions(tmp_path):
    planner = project(tmp_path, "import unittest\ndef test_plain(): pass\n")
    assert planner.infer(["app.py"]).commands[-1] == "python3 -m pytest -q"


def test_root_unittest_files_are_discovered_without_a_tests_directory(tmp_path):
    (tmp_path / "test_app.py").write_text("from unittest import TestCase\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n")
    assert VerificationPlanner(tmp_path).infer(["app.py"]).commands[-1] == "python3 -m unittest discover -s . -v"
