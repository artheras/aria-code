"""A bare import of an aria_code module is that module, not a second copy."""

import subprocess
import sys
import textwrap


def _run(code: str, cwd) -> str:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_both_roots_give_one_module_with_its_real_spec(tmp_path):
    out = _run("""
        import importlib
        import aria_code
        import runtime.checkpoints as bare
        import aria_code.runtime.checkpoints as packaged
        assert bare is packaged
        assert bare.__spec__.name == "aria_code.runtime.checkpoints"
        bare.MARKER = 1
        assert importlib.reload(bare) is packaged   # reload re-runs the real module
        print("ok")
    """, tmp_path)
    assert out == "ok"


def test_a_users_own_module_of_the_same_name_still_wins(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text("OWNER = 'user'\n")
    out = _run("""
        import sys
        sys.path.insert(0, ".")
        import aria_code
        import tools
        print(getattr(tools, "OWNER", "aria"))
    """, tmp_path)
    assert out == "user"
