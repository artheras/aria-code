# 接入自己的模型（本地 / 自建 / 任意云端）

Aria Code 内置了 12 个 provider（ollama、lmstudio、deepseek、openai、anthropic、
groq、siliconflow、dashscope、moonshot、zhipu、together、local）。要接入**未列出**
的服务——自建 vLLM、公司内网网关、Arthera 自己的云端 API、任何 OpenAI 兼容
端点——不需要写 Python，在配置文件里声明即可。

## 三个原则

1. **配置里只写环境变量名，不写密钥值。** 配置文件常被备份、同步、误提交，
   明文密钥的暴露面比环境变量大一个量级。检测到 `api_key` / `token` 这类明文
   字段时，该 provider 会被**拒绝加载**并打印替代写法——不是静默忽略，因为
   静默忽略会让你以为配好了，实际请求全部 401。
2. **一个坏条目不连累其他。** 某个 provider 写错只会跳过它自己，其余照常可用。
3. **凭证与策略分开。** 密钥进环境变量（`~/.aria/.env`），选哪个模型进
   `providers.yaml`。

## 配置位置

按以下顺序查找第一个存在的文件：

```
~/.arthera/providers.json      # CLI 的 /apikey 命令写这里
~/.aria/providers.yaml         # 推荐手写这个
~/.aria/providers.json
```

**只读用户级文件。** 项目里的 `./.aria.json`、`./.aria.yaml` 不能声明 provider，
也不能设置 `llm` 节（`default`、`fallback`、各 provider 的 `base_url`）：
克隆下来的仓库不应该能把对话和凭据发到它指定的服务器。Python 与原生运行时规则相同。

## 完整示例

`~/.aria/providers.yaml`：

```yaml
llm:
  # 默认模型
  default: mycorp/qwen-72b

  # 后备链：默认不可用时按顺序尝试
  fallback:
    - ollama/qwen2.5:7b
    - arthera-cloud/gemini-3.8-flash
    - deepseek/deepseek-chat

  # 按任务类型选模型
  code_tasks:     ollama/qwen2.5-coder:7b
  heavy_analysis: arthera-cloud/gemini-3.8-flash
  fast_response:  groq/llama-3.3-70b-versatile

# 声明内置列表之外的 provider
model_providers:
  mycorp:
    name: 公司内网推理网关          # 显示用
    base_url: https://llm.internal.example.com/v1
    env_key: MYCORP_LLM_TOKEN       # ← 只写变量名
    env_key_instructions: 找基础架构组申请，写进 ~/.aria/.env
    model: qwen-72b                 # 该 provider 的默认模型
    timeout: 120
    http_headers:                   # 固定请求头
      X-Tenant: research
    env_http_headers:               # 值来自环境变量的头；变量未设时**不发**该头
      X-Trace-Id: MYCORP_TRACE_ID

  arthera-cloud:
    name: Arthera 云端（Cloud Run）
    base_url: https://your-api.run.app/api/v2
    env_key: ARTHERA_API_TOKEN
```

对应的 `~/.aria/.env`：

```bash
MYCORP_LLM_TOKEN=实际的token
ARTHERA_API_TOKEN=实际的token
```

声明之后，`mycorp/<任意模型>` 就能像内置 provider 一样用在
`default` / `fallback` / `code_tasks` 里。

## 常见场景

**本地模型（已内置，无需声明）**

```yaml
llm:
  default: ollama/qwen2.5:7b        # Ollama，默认 http://localhost:11434
  fallback: [lmstudio/local-model]  # LM Studio，默认 http://localhost:1234
```

改端口用环境变量 `OLLAMA_BASE_URL` / `LMSTUDIO_BASE_URL`。

**自建 vLLM / OpenAI 兼容服务**

```yaml
model_providers:
  vllm:
    base_url: http://192.168.1.50:8000/v1
    model: Qwen2.5-72B-Instruct
    # 无鉴权就不写 env_key
```

**覆盖内置 provider**（比如把 openai 指向自建网关）

同名声明会覆盖内置项——用户显式写下的配置优先。

## 排查

```
/config          # 列出所有 provider 及可用状态，自定义项标 source=custom
```

`available=False` 时会给出具体缺哪个环境变量，以及你在
`env_key_instructions` 里写的获取说明。

## 原生运行时（Rust/Go）

原生运行时支持 Google（Gemini / Vertex AI），并提供 Ollama 本地快捷配置，
通过 Ollama 的 OpenAI 兼容 `/v1` 接口请求模型。其他服务通过用户声明的
OpenAI 兼容端点接入，例如 vLLM、LiteLLM、OpenRouter 和公司网关。

交互终端输入 `/model`，先选择服务，再从实际查询的模型列表中选择模型。
取消保留当前选择；查询失败会显示原因，也可以手动输入模型 ID。
无交互执行使用显式命令，不等待菜单：

```text
/models gcp
/models ollama
/model gcp <Google模型ID>
/model ollama <已安装模型名称>
/health
```

模型与服务一起持久化，后续消息使用新服务，原会话历史保留。
模型列表和健康检查不调用生成接口；选择成功表示配置已保存，不能代替推理验证。
`/models` 最多展示 Google 返回的第一页，菜单最多展示 98 个模型；其他模型可以按 ID 选择。

Google Cloud 使用现有登录/ADC、项目与区域配置。可以在终端设置
`/config set gcp_project=PROJECT` 和 `/config set gcp_location=REGION`；
`GOOGLE_CLOUD_PROJECT`、`GOOGLE_CLOUD_LOCATION` 环境变量优先。
`gcp` 是强制 Vertex AI 的选择，`google` 沿用当前 Google 认证设置。

Ollama 地址依次读取 `ollama_base_url`、`OLLAMA_HOST`，默认
`http://127.0.0.1:11434`。可以使用 `/config set ollama_base_url=URL` 修改。
用户在 `model_providers.ollama` 中声明的配置优先，用于远程认证等定制；
服务未启动或未安装模型会明确报错/提示，不会自动下载模型。

接口参考：[Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)、
[Vertex AI quickstart](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart)。

```yaml
# ~/.aria/providers.yaml（或 $ARIA_HOME/providers.json、~/.aria/providers.json）
model_providers:
  mycorp:
    base_url: https://llm.internal.example.com/v1
    env_key: MYCORP_LLM_TOKEN            # 只写变量名
    model: qwen-72b
    query_params: {api-version: "2025-04-01"}   # 例如 Azure
  vertex-gateway:                        # 令牌来自命令，按 refresh_interval_ms 刷新
    base_url: https://gateway.example.com/v1
    auth: {command: gcloud, args: [auth, print-access-token]}
```

然后 `/config` 里设 `local_provider: mycorp`（`/model <名字>` 切换该端点提供的模型）。

与上文 Python 行为的差别：

- **只读用户级文件**（与 Python 相同，见上文“配置位置”）。
- **凭据只来自环境变量（`env_key`）或命令（`auth.command`）。** 命令只能打印令牌本身；
  缓存 `refresh_interval_ms`（默认 5 分钟，0 = 只在 401 后刷新），服务器返回 401 时重取一次。
  `env_key` 与 `auth` 二选一。
- **拒绝的条目：** 文件里写明文密钥、缺 `base_url`、`base_url` 不是 http(s)、
  使用内置 Google 的名字（`google`、`gemini`、`vertexai`…）。其余条目照常可用。
- **排查：** `aria-runtime providers` 列出读到的文件、可用的 provider（不含密钥，只显示
  是否已设置）以及每个被跳过条目的原因。未声明的 `local_provider` 在会话开始前就报错，
  并指出该写在哪个文件里。

## 与 codex 的对应关系

本设计对标 codex 的 `model_providers`（`~/.codex/config.toml`）。字段语义一致：
`base_url` / `env_key` / `env_key_instructions` / `http_headers` /
`env_http_headers`。差别是 codex 还提供明文 `experimental_bearer_token`，
但其源码注释写着 *"Use of this config is discouraged in favor of `env_key` for
security reasons"*——所以这里不提供该入口。

## Working with other models

Aria's model does the work; models from other providers can check it, answer a question for it, or call
Aria for what only it knows.

**A different model reviews the change.** `/config set review_gate=true` has a fresh-context reviewer read
each coding turn's diff. By default that is the model that wrote it. Set another provider's model to make
the author and the approver different:

```text
/config set review_model=anthropic/claude-sonnet-5-5     # or openai/<model>, google/gemini-3.8-flash
/config set review_model=off                             # back to the builder
```

The delivery report names the reviewer (`✓ no blocking findings (reviewed by anthropic/…)`). The diff, the
goal and the check results go to that provider, so it is a setting, never a default; the builder's Aria
backend and token are not used for it.

**Ask another model.** `/consult claude <question>` (or `/consult provider/model <question>`) sends the
question, and any `@file` references in it, to that model and files its answer in the conversation, so
what you tell Aria next can build on it. The other model sees only the question: no history, no project,
no tools. `claude` is built in; add `chatgpt` or any other short name with
`/config set consult_aliases={"chatgpt": "openai/<model>"}`.

**Other models call Aria.** `aria-code-mcp` is an MCP server (stdio) that any MCP client can register.
Besides market, research, report and broker tools it now offers what a coding agent lacks:

| Tool | Answers |
|---|---|
| `aria.risk.assess` | how risky is this command or tool call (L0–L4, capabilities, what it reaches, whether it needs approval) |
| `aria.contract.check` | would the project's `.aria/policy.yaml` allow this call, and which rule decided |
| `aria.project.impact` | what depends on these files or symbols: importers, tests, services |
| `aria.project.commands` | the project's test, build, lint and simulation commands |

All four are read-only: nothing runs, and a `root` outside the server's working directory or your home is
refused. Register it, for example, with `claude mcp add aria-code -- aria-code-mcp`.

