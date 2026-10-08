# 验收闸门 (Acceptance Gate)

> 让「完成」变成一句**跑过**的话。

## 问题

改动之前，Aria 在验证这件事上一直停在「建议」：

| 位置 | 行为 |
|------|------|
| `workspace/verify.py` | `VerificationPlanner` 能从改动的文件推断出该跑什么检查 |
| `apps/cli/tools/write_tools.py` | 写完文件后把推断出的命令塞进 `suggested_verification` 字段 |
| `/verify` (`core_cmds.py`) | 用户主动敲的时候才真的执行 |

三条路径全部以「建议」结束。循环里没有任何一处真的跑过这个命令、读过失败输出、或者
因为红灯而拒绝结束这一轮 —— 模型说一句「任务完成」，回合就结束了，无论代码是不是能跑。

这就是「会写代码的 agent」和「敢交给它一个仓库的 agent」之间的全部距离。强编码 agent 的
优势不在于补丁写得更漂亮，而在于**它在结构上无法在检查是红的时候停下**。守住这条线的是
循环，不是模型 —— 所以它属于 runtime，也因此对 7B 本地模型和前沿云端模型同样生效。

## 机制

`runtime/acceptance.py` 里的 `AcceptanceGate`：

1. **上膛**：某个工具真的往磁盘写入了内容（`write_file` / `edit_file` / `multi_edit` …）。
2. **击发**：模型不再请求工具的那一刻 —— 也就是循环原本会 `break` 并报告成功的那个出口。
3. **判定**：跑 `VerificationPlanner` 推断出的命令（或配置里写死的命令）。
   - 绿 → 回合照常结束，但这次带着证据。
   - 红 → 把**失败输出**（裁剪过、抽出了 `file:line` 定位）作为下一轮的用户消息回灌，
     模型对着真实信号修，而不是对着自己对刚写过什么的记忆修。

```
round N     模型调用 write_file           → 闸门上膛
round N+1   模型说「任务完成」,不再要工具  → 闸门击发
              ├─ pytest -q → exit 0       → verified: true,回合结束
              └─ pytest -q → exit 1       → 失败输出回灌 → round N+2 继续修
```

## 三条不让它变成负担的约束

- **只读回合不付这个成本。**上膛需要一次*已落盘*的写入。行情提问、代码审查、
  `stage_only` 的暂存改动 —— 一条检查都不会跑。
  （暂存改动尤其重要：文件还没落盘就去跑测试，验的是旧状态，绿灯毫无意义。）
- **有界。**连续 `max_attempts` 轮红灯之后闸门不再上膛，回合**如实结束** ——
  结果里带 `verified: false` 和失败的命令，而不是一直循环到预算耗尽。
  一个说明自己没验过的答案是有用的；一个死循环不是。
- **不自造沙箱。**命令走调用方自己的 runner。CLI 传进去的就是 `run_command` 工具本身，
  于是工作区沙箱、命令策略、trace 全部原样生效 —— 闸门执行不了任何用户当前权限模式
  本来就不允许的东西。

## 结果契约

`AgentTurnResult.acceptance` 是三态的，这是刻意的：

| `verified` | 含义 |
|-----------|------|
| `true` | 检查跑了，全绿 |
| `false` | 检查失败，或文件在通过检查后又被修改、尚未重新验收 |
| `null` | 什么都没验 —— 只读回合，或者这个工作区推断不出任何检查 |

把后两者合并成 `false`，每个分析回合看起来都像失败了；合并成 `true`，就是这个模块存在
的全部意义所要防止的那个谎。

已经应用到磁盘的改动即使同时标记 `staged: true`，仍然需要验收。
检查结果必须包含退出状态，不能仅凭 `success: true` 推定命令通过。
`pending_paths` 记录尚未覆盖的改动。达到轮数上限、预算耗尽或检查失败时，
runtime 返回未完成状态；CLI 的 `--json` 输出包含 `stop_reason`，并以非零退出码结束。
`-C` 指定的项目目录也用于推断与执行检查，见 [本地工作区](local-workspace.md)。

## 配置

CLI 的闸门由 `providers/runtime_bridge.py:build_acceptance_gate()` 构造，读 session config：

| 键 | 默认 | 说明 |
|----|------|------|
| `acceptance_gate` | `true` | 关掉闸门 |
| `acceptance_commands` | `[]` | 写死验收命令，覆盖推断。这是「对本仓库而言绿是什么意思」的挂钩点 —— `make check`、`bazel test //...` 这类推断永远猜不到的东西 |
| `acceptance_max_attempts` | `2` | 连续红灯几次之后放弃并如实报告 |
| `acceptance_timeout` | `300` | 单条命令超时（秒）。超时算**失败**，不算通过 |

`permission_mode` 为 `read-only` / `plan` 时不构造闸门 —— 写不了东西，也就没什么要验的。

## 为什么这是行业化的前提

每个行业对「对」的定义不同，但**表达方式是同一个**：一条会返回退出码的命令。

- 软件团队：`pytest -q` / `npm test` / `tsc --noEmit`
- 数据/风控：一段跑在样本上的校验脚本，schema 不符就非零退出
- 物流/ERP：对账脚本，账不平就非零退出
- 金融：回测跑通 + 指标落在阈值内

所以 `acceptance_commands` 是 domain pack 真正的落地口子：pack 不需要教会核心它那一行的
知识，它只需要说清楚**这一行的绿灯长什么样**，闭环就自动成立了。
