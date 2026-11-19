"""依赖图守卫：模块级导入保持无环，对 aria_cli 的反向耦合只减不增。

为什么需要这条守卫
------------------
对照 openai/codex：它有 127 个 crate、196 万行 Rust——**比 aria-code 大一个数
量级**，文件平均还更长（395 行 vs 241 行）。它读起来更清楚，靠的不是文件小，
而是 Cargo 从根上不允许 crate 之间成环，以及扇入最高的 crate（codex-protocol，
被 81 个 crate 依赖）是**纯类型**：481 个 struct/enum，没有运行时状态。

aria-code 的形状相反。aria_cli.py 是 7042 行的**活状态**——console、HAS_RICH、
LOCAL_TOOLS、响应缓存、审批策略、工具注册表——而 29 个模块靠函数内 `from
aria_cli import ...` 反向伸回来取这些东西。Python 允许这样（延迟到调用时才
解析，所以导入期不成环），代价是这份耦合完全不可见，也就无从阻止它变大。

这不是假想的代价。2026-09-29 一次性修掉的这些，全是同一个结构的产物：

  - _rebind_mixin_globals 这个函数存在,就是为了绕开这层耦合;40f23c8 把它的
    25 处调用删了却没做完转换,58 个裸名当场变成 NameError,/apply、/code
    --save、/plan、/run、券商选择器、/backtest 的大部分输出全部一跑就崩
  - 为了让裸名导入可用,包在两个根下都可导入,于是 `X` 与 `aria_code.X` 是两个
    不同的模块对象;24 个测试 patch 打在没人调用的那一份上,隔离静默失效——
    test_subagent 跑在开发者真实的任务账本上,test_broker_chat_confirm_gate
    往真实的 brokers.json 里写,两个测试真的联网抓了行情

Cargo 的强制力 Python 给不了,但一条门禁能给同样的效果——就像
test_root_module_budget.py 用基线管住了根目录模块数那样。

怎么让这两个数字降下去
----------------------
不是去拆小文件（aria-code 的文件已经比 codex 的小了）,是把 aria_cli 持有的
状态显式传下去。40f23c8 已经对 console/HAS_RICH 做了一半——迁到 AriaContext——
把那一半做完,LOCAL_TOOLS、缓存、审批策略同样处理,反向导入就没有存在的理由了。
纯类型可以沉到一个无依赖的 contracts 层,那是 codex-protocol 扮演的角色。
"""

from __future__ import annotations

import ast
import pathlib

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "aria_code"

# 2026-09-29 冻结。只减不增。
_BASELINE_ARIA_CLI_IMPORTERS = 19
# 这个刻度数的是 import 语句行数，不是调用点。一行 `from aria_cli import a, b, c`
# 只算 1。所以它会低估进展：把 73 处 _print_error 调用改成 context 适配器后，
# 模块数只掉了 1、行数只掉了 14。它仍然是对的方向指示，但别拿它当工作量。
_BASELINE_ARIA_CLI_REFERENCES = 137


def _module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(PACKAGE_ROOT).parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)


def _modules() -> dict[str, pathlib.Path]:
    return {_module_name(p): p for p in PACKAGE_ROOT.rglob("*.py")}


def _resolve(target: str | None, known: set[str]) -> str | None:
    """把一条 import 的目标映射回本包内的模块名，两个导入根都认。"""
    if not target:
        return None
    name = target[len("aria_code."):] if target.startswith("aria_code.") else target
    if name in known:
        return name
    parent = name.rsplit(".", 1)[0] if "." in name else None
    return parent if parent in known else None


def _import_edges() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 (模块级导入边, 函数内导入边)。

    只有模块级的那一组会在导入期求值，因此只有它能造成导入期成环；函数内的
    那一组是这个仓库用来绕开耦合的逃生舱，单独统计。
    """
    modules = _modules()
    known = set(modules)
    top: dict[str, set[str]] = {}
    deferred: dict[str, set[str]] = {}

    for name, path in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        inside_function: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inside_function.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                targets = [node.module]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                continue
            bucket = deferred if node.lineno in inside_function else top
            for target in targets:
                resolved = _resolve(target, known)
                if resolved and resolved != name:
                    bucket.setdefault(name, set()).add(resolved)

    return top, deferred


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan，返回成员多于一个的分量，即真正的环。迭代实现，避免深图爆栈。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    counter = 0

    for root in list(graph):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = sorted(graph.get(node, ()))
            if child_index < len(children):
                work[-1] = (node, child_index + 1)
                child = children[child_index]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        found.append(component)
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
    return found


def test_module_level_imports_stay_acyclic():
    """导入期必须无环——Cargo 是编译期拒绝，这里是 CI 拒绝。

    模块级的环在 Python 里不会立刻炸，它取决于谁先被导入，于是表现为「换个
    入口就 ImportError」这类最难查的故障。目前是干净的，要守住。
    """
    top, _ = _import_edges()
    cycles = _strongly_connected(top)
    assert not cycles, (
        "模块级导入出现环，导入顺序一变就会 ImportError：\n  "
        + "\n  ".join(" → ".join(sorted(component)) for component in cycles)
        + "\n把共享的类型下沉到无依赖的模块，或把导入挪进函数体（后者只是权宜，"
        "会让下面那条守卫的数字变大）。"
    )


def test_reverse_coupling_to_aria_cli_does_not_grow():
    """反向伸回 aria_cli 取状态的模块数，只减不增。"""
    _, deferred = _import_edges()
    importers = sorted(name for name, deps in deferred.items() if "aria_cli" in deps)
    assert len(importers) <= _BASELINE_ARIA_CLI_IMPORTERS, (
        f"反向导入 aria_cli 的模块从 {_BASELINE_ARIA_CLI_IMPORTERS} 增加到了 {len(importers)}。\n"
        "新代码请通过 AriaContext 显式拿 console/config 等状态，不要再从 aria_cli 反取——"
        "这正是 40f23c8 做了一半的那次迁移。\n"
        f"当前清单：\n  " + "\n  ".join(importers)
    )


def test_aria_cli_reference_count_does_not_grow():
    """引用次数是比模块数更细的刻度：同一个模块里少一处反取也算进展。"""
    _, deferred = _import_edges()
    modules = _modules()
    total = 0
    for name in deferred:
        if "aria_cli" not in deferred[name]:
            continue
        source = modules[name].read_text(encoding="utf-8", errors="replace")
        total += sum(
            1 for line in source.splitlines()
            if line.strip().startswith(("from aria_cli import", "import aria_cli"))
        )
    assert total <= _BASELINE_ARIA_CLI_REFERENCES, (
        f"对 aria_cli 的反向引用从 {_BASELINE_ARIA_CLI_REFERENCES} 处增加到了 {total} 处。"
    )


def test_baselines_are_tightened_after_cleanup():
    """清理之后要把基线调下来，否则守卫会慢慢失去约束力。

    跟 test_root_module_budget.py 里那条同样的理由：基线留在旧值而实际已经降
    下去，等于未来可以无声地加回来。
    """
    _, deferred = _import_edges()
    importers = [name for name, deps in deferred.items() if "aria_cli" in deps]
    assert len(importers) >= _BASELINE_ARIA_CLI_IMPORTERS - 3, (
        f"反向导入 aria_cli 的模块已降到 {len(importers)}（基线 "
        f"{_BASELINE_ARIA_CLI_IMPORTERS}），请把 _BASELINE_ARIA_CLI_IMPORTERS "
        f"更新为 {len(importers)}，锁定这次清理的成果。"
    )
