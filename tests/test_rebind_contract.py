"""守卫：被重绑的函数，它引用的每个全局名都必须在重绑后的命名空间里存在。

aria_cli 把五组函数的 ``__globals__`` 换成自己的命名空间，好让它们用裸名读
CLI 的会话状态：

    _rebind_module_function_globals(tool_executor, __all__)
    _rebind_module_function_globals(provider_endpoints, ["_test_api_key"])
    _rebind_module_function_globals(broker_render, __all__)
    stream_ollama = FunctionType(code, <merged globals>, ...)

这套机制有两种静默失效，都不会在导入期报错，只在那行代码真正执行时炸：

  1. aria_cli 里某个名字被删掉或改名——函数还引用着它
  2. 反过来，源模块自己的模块级名字在重绑后**丢失**，因为 globals 被整个换掉了
     （pdf_export_cmds.py 的注释记着这一点）

40f23c8 删掉 mixin 注入那次正是第一种，58 个裸名当场全成 NameError，而且没有
任何东西发现——直到一个多月后。这条守卫做的就是那件没人做的事：真的导入
aria_cli（重绑随之发生），然后逐个函数核对。

不核对**属性**访问，只核对全局名读取；lambda 与嵌套函数的参数一律算作已绑定，
宁可漏报也不产生噪音。
"""

from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "aria_code"
_BUILTINS = set(dir(builtins))

# 源文件 -> 被重绑的名字（None 表示该模块 __all__ 里的全部）
REBOUND = {
    "apps/cli/tool_executor.py": None,
    "apps/cli/provider_endpoints.py": ["_test_api_key"],
    "apps/cli/broker_render.py": None,
    "apps/cli/providers/llm/ollama_stream.py": ["stream_ollama"],
}


def _bound_everywhere(node: ast.AST) -> set[str]:
    """函数体内所有被绑定的名字，含 lambda 与嵌套函数的参数。

    对嵌套作用域是过度绑定的——某个内层参数恰好和外层全局同名时会漏报。守卫
    宁可漏一个，也不要因为 lambda 的 `x` 这种噪音而被忽视。
    """
    bound: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.arg):
            bound.add(child.arg)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            bound.add(child.id)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name.split(".")[0]) for a in child.names)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, ast.Global) or isinstance(child, ast.Nonlocal):
            bound.update(child.names)
    return bound


def _global_reads(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _free_names(fn_node: ast.AST) -> set[str]:
    return _global_reads(fn_node) - _bound_everywhere(fn_node) - _BUILTINS


def _cases():
    out = []
    for rel, only in REBOUND.items():
        path = PACKAGE_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {
            n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        names = only if only is not None else sorted(funcs)
        for name in names:
            if name in funcs:
                out.append(pytest.param(rel, name, funcs[name], id=f"{pathlib.Path(rel).stem}::{name}"))
    return out


@pytest.mark.parametrize("rel, name, node", _cases())
def test_rebound_function_can_resolve_every_name_it_reads(rel, name, node):
    aria_cli = pytest.importorskip("aria_cli")  # 导入即触发全部重绑

    fn = getattr(aria_cli, name, None)
    assert fn is not None, (
        f"{rel} 的 {name} 应当被重绑进 aria_cli，但 aria_cli 里没有这个名字。"
        " 要么重绑调用被删了，要么该名字已不在源模块的 __all__ 里。"
    )

    namespace = getattr(fn, "__globals__", {})
    unresolved = sorted(n for n in _free_names(node) if n not in namespace)
    assert not unresolved, (
        f"{rel}::{name} 读取了这些全局名，但它们不在重绑后的命名空间里：\n  "
        + ", ".join(unresolved)
        + "\n\n每一个都是那行代码执行时的 NameError，导入期不会有任何征兆。"
          " 要么把名字加回 aria_cli，要么让该函数自己导入它。"
    )


def test_the_rebinds_actually_ran():
    """别让这条守卫因为全部 importorskip 而空转。"""
    aria_cli = pytest.importorskip("aria_cli")
    # 重绑后的副本，其 __globals__ 必须是 aria_cli 的命名空间而不是源模块的。
    fn = getattr(aria_cli, "_tool_read_file", None)
    assert fn is not None, "_tool_read_file 没有被重绑进 aria_cli"
    assert fn.__globals__ is aria_cli.__dict__, (
        "_tool_read_file 的 globals 不是 aria_cli 的命名空间——重绑没有生效"
    )
