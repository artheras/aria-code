# 本地项目与电脑文件

云端模型负责推理，本机的 Aria 负责读取文件、修改代码和执行命令。模型运行在
Google Cloud，并不要求项目上传到 Cloud Run；工具结果中实际读取的内容会作为
模型上下文发送给所选服务。

## 打开自己的项目

```sh
aria -C ~/Projects/my-app
aria code -C ~/Projects/my-app
aria-code -C ~/Projects/my-app
```

`-C` 在载入项目配置前切换目录。相对文件路径、工具的默认工作目录和自动验收
都使用这个项目目录，而不是启动 Aria 时所在的目录。目录必须已存在。

使用 Google Cloud 时，选用已授权项目可访问的模型：

```sh
aria -C ~/Projects/my-app --model google/gemini-2.5-flash
```

本机需要 Google 凭据和项目配置：可使用 Application Default Credentials，
或已经登录的 `gcloud` 与默认项目。Arthera 的 `/login` 与 Google Cloud 授权是
两种账户。不要把访问令牌放进项目文件。更多设置见 [模型配置](model-providers.md)。

如果已配置 `backend_chat`，本地项目任务会在 Google 凭据可用时，为本次任务
选择直接 Google 推理与本机工具；保留模型选择，也不改变已保存的后端设置。
缺少工具或凭据时，Aria 会明确说明，避免把推测的文件内容当作实际操作。

## 额外目录与权限

默认 `workspace-write` 模式允许修改选定项目、临时目录及 Aria 输出目录。
本地文件工具可读取用户主目录中的文件；其他位置可用 `--read-dir` 明确授权。
修改项目以外的目录需要 `--add-dir`。这两个参数可重复使用：

```sh
aria -C ~/Projects/my-app --read-dir /Volumes/Reference --add-dir ~/Projects/shared-library
```

| 参数或模式 | 行为 |
| --- | --- |
| `--read-dir DIR` | 扩展文件工具的可读目录，不授予写权限 |
| `--add-dir DIR` | 扩展文件工具与命令沙箱的可写目录 |
| `permission_mode=read-only` | 拒绝文件修改；macOS 命令仍可写临时目录 |
| `permission_mode=full-access` | 用户主动选择的宽权限模式，命令不使用工作区沙箱 |

命令行目录授权只对本次进程有效，不写进用户配置。模型不能通过工具参数改变
项目根目录、授予目录权限或自行跳过确认；符号链接会按实际目标路径检查。
工具确认与目录权限分开：允许某个编辑工具运行，不等于允许它修改所有目录。

macOS 的 `run_command` 使用现有 Seatbelt 沙箱。命令的 `cwd` 可以改变执行位置，
但不会因此扩大可写目录；需要写入的额外位置应由用户使用 `--add-dir` 指定。
从仓库子目录启动时，命令沙箱还允许写入所在 Git 仓库。命令读取仍受系统账户
权限控制，`--read-dir` 不是命令进程的读取隔离。Linux 和 Windows 目前仍使用
现有命令策略，不能宣称已经具备与 macOS 相同的进程隔离。

文件工具受到操作系统权限约束；例如 macOS 未授予终端访问 Documents 的权限时，
Aria 无法绕过该限制。远程工作区应使用受限制的执行上下文或
`ARIA_RUNTIME_SCOPE=remote`，避免采用本地 CLI 的主目录读取规则。

## 真实执行与验收

```sh
aria-code -C ~/Projects/my-app -p "修复这个项目的失败测试并运行检查" \
  --model google/gemini-2.5-flash --allow-tools read_file,edit_file,run_command --json
```

`--allow-tools` 是本次进程的工具确认授权，适用于无人值守任务；不改变目录范围。
实际写入的文件会触发自动验收，失败日志会交回模型修复。只有暂存、尚未应用的
改动不会验证旧文件。没有退出状态的检查不能被视作通过，后续修改也会使旧的
绿色验收失效。

JSON 的 `acceptance` 记录执行过的命令与退出码。`stop_reason` 区分完成、轮数上限、
预算耗尽、重复失败和验收失败；未完成任务与失败验收会返回非零退出码。
无法推断检查命令时会报告未验证，不代表代码已通过测试。可为项目配置
`acceptance_commands`，详见 [验收闸门](acceptance-gate.md)。

## 托管后端的本机工具协议

`backend_local_tools=true` 是兼容新后端的显式选项，默认关闭。请求携带工具 schema、
`tool_execution=client` 和 `tool_protocol=aria-local-v1`。后端必须先确认这两个字段，
再返回待本机执行的 `tool_call`；本机 runtime 负责执行与回传结果。未确认的旧后端
返回明确的“不支持本机工具”错误，后端自行返回 `tool_result` 也会被拒绝，避免同一个
操作在服务器与电脑上重复执行。此客户端协议不代表线上后端已经实现或部署。

## 参考

实现参考 [Codex 的工作区与进程沙箱](https://github.com/openai/codex/blob/ec8251c545a1aeca985b6a0db5517cd8e1b34b35/codex-rs/core/src/sandboxing/mod.rs)
及 [App Server 的客户端与执行器分层](https://learn.chatgpt.com/docs/app-server)。
Aria 使用自身的 Python runtime 和权限配置；上述参考不意味着其所有平台隔离能力
与 Codex 相同。
