# 开源 → 闭源/商业化路线图 (Open-Core)

像 Claude Code 那样"源码不公开、二进制可用"的目标,不需要一次性闭源整个项目。
推荐 **open-core**:CLI / runtime / 工具系统保持**真正开源**(Apache 2.0),把**专有价值**编译后分发。
本仓库已为此铺好脚手架,下面是落地步骤。

## 许可证已从 BSL 1.1 改为 Apache 2.0

这是一个方向性的改变,不只是换个文件。BSL 的做法是"源码可见但禁止竞品",靠**许可证**保护商业价值;
Apache 2.0 的做法是"核心彻底开放",靠**边界**保护商业价值 —— 把高价值逻辑移出公开仓库。

对 open-core 来说后者其实更干净:BSL 让整个项目都不算开源,包括那些你本来乐意开放的部分,
代价是开发者生态、贡献者和集成方。Apache 2.0 把这个代价换成了一个明确的工程任务 ——
**把高价值逻辑移出公开仓库** —— 而这件事**大部分已经做完了**。

`packages/quant_engine` 还有约 4600 行在这个仓库里,所以这一版把它们一起按
Apache 2.0 发布了。Apache 2.0 的授权是**逐版本、不可撤回**的,已经发出去的
版本不能事后收回 —— 所以这句话值得写清楚,而不是留给读者推断。

但这不构成阻塞。审计的结论(见下面「阶段 2」和「剩下的还值得拆吗」)是:
真正的护城河 —— `services/`、`agent_runtime/`、`risk/`、`analysis/`、
`strategies/` —— **早就不在这个仓库里了**;留下的是 Dixon-Coles (1997)、
Black-Scholes、Elo、Markowitz 这些**已发表方法的实现**,不是专有 IP。

所以这里**不需要**在发版前先完成阶段 1 和阶段 2。这一段最初写的正是那个
要求,那是在做审计之前写的,当时以为引擎整个都还在树里。

## 已就位的脚手架

| 组件 | 作用 |
|---|---|
| `LICENSE` (Apache 2.0) | 核心真正开源;保护靠仓库边界,不靠许可证条款 |
| `licensing.py` | 功能授权闸门 —— 免费功能默认开放,专业功能需 license(支持 HMAC 签名) |
| `packages/quant_engine/is_available()` | 可选导入边界 —— 引擎缺失时免费壳优雅降级 |
| `tools/build_quant_engine.py` | 用 Nuitka 把专有引擎编译成 `.so`(无源码) |
| `CLA.md` | 贡献者版权协议 —— 保留你单方 relicense 的权利 |

## 分阶段执行

### 阶段 0 — 现在(已完成)
- ✅ Apache 2.0 + PRIVACY + opt-in 同意。
- ✅ 专有数学已隔离在 `packages/quant_engine/`(期权定价、蒙特卡洛、Kelly、Dixon-Coles…)。
- ✅ 调用点走 `try/except` + `is_available()`,缺引擎不崩 —— 其中两处 import 本身没有守卫,
  靠调用方的 `except Exception` 兜住;见 `tests/test_degrades_without_quant_engine.py`。

### 阶段 1 — 编译专有引擎
```bash
pip install nuitka
python tools/build_quant_engine.py --check    # 检查工具链
python tools/build_quant_engine.py --build     # 产出 dist_compiled/*.so
```
- 发布带 `.so`(而非 `.py`)的 wheel;反编译成本极高 ≈ 实质闭源。
- 免费壳把它作为**可选依赖** `import`,无授权时降级。

### 阶段 2 — 拆库(**大部分已经完成**,本文档此前没有反映这一点)

审计当前公开仓的结果:引擎的六个子包**已经不在这个仓库里了**。

| 子包 | 状态 | 内容 |
|---|---|---|
| `services/` | **私有** | 企业财务、Stripe 分析、A 股次日预测服务 |
| `agent_runtime/` | **私有** | quant strategist agent |
| `risk/` | **私有** | risk-compliance agent |
| `analysis/` | **私有** | signal pipeline |
| `strategies/` | **私有** | 策略基类 |
| `mcp_server.py` | **私有** | 引擎自己的 MCP server |
| `stochastic/` | 公开,1649 行 | Black-Scholes、CRR 二叉树、SVI、Itô、Kelly、蒙特卡洛 |
| `sports/` | 公开,2705 行 | Dixon-Coles、Elo、form、h2h、calibrator、tracker |
| `backtest/` | 公开,163 行 | core、engine |
| `portfolio/` | 公开,98 行 | 均值-方差优化 |

公开侧对私有子包的 import 全部走守卫并降级(`backtest_cmds.py` 那句
`except ImportError` 下面就写着 "Moat feature")。也就是说**护城河已经搬走了**。

`tests/test_engine_boundary.py` 把这条边界钉住了,其中最重要的一条不变量是:
**对已私有子包的 import 必须有守卫**。没守卫的 import 只在公开 checkout 里炸,
而开发机旁边就放着私有仓,永远测不出来。

剩下的 4615 行还要不要搬走,是个需要单独判断的问题 —— 见下面「剩下的还值得拆吗」。

### 阶段 3 — 授权与付费
- 用 `licensing.py`:专业功能调 `require_feature("...")`。
- 签发 license:`~/.arthera/license.json` `{key,tier,features,exp,sig}`。
- 生产环境设 `ARIA_LICENSE_PUBKEY`(随构建分发的 HMAC 校验密钥)→ 强制签名校验,防伪造。

### 阶段 4 — 服务端化(最强护城河)
- 最高价值逻辑(实时因子、ML 训练、组合优化)逐步迁到**后端 API**。
- 服务端代码天生不可分发;客户端只拿结果。Claude Code 的核心能力也在服务端。

## 剩下的还值得拆吗

先看公开侧**实际用到**的是哪些 —— 只有三个模块有外部 import:

| 模块 | 被谁用 |
|---|---|
| `sports/predictor` | `football_data_client`、`football_agent` |
| `sports/tracker` | `football_data_client` |
| `stochastic/options_pricing` | `analysis_cmds`(只用 `black_scholes`) |

其余全部只被引擎内部调用。`sports/ml_model.py` 已删除:没有任何模块、测试或入口导入它。它曾在 25 条样本上对 Dixon-Coles 做 A/B,交叉验证 Brier 1.0446,基线 0.3251,更差约 3.2 倍,所以没有接入预测路径。样本量上来之前不要把它加回去。

再看这些模块**是什么**。它们的 docstring 自己交代得很清楚:

- `dixon_coles.py` —— 「实现 Dixon & Coles (1997) 经典论文的完整模型」
- `elo.py` —— World Football Elo,公式就写在 docstring 里
- `options_pricing.py` —— Black-Scholes (1973)、CRR (1979)、SVI 参数化;
  标题写的是「Citadel / SIG **风格**」,即仿其风格,不是其专有实现
- `portfolio/optimizer.py` —— `scipy.optimize.minimize` 上的 Markowitz 均值-方差

**这些是已发表方法的实现,不是专有 IP。** 教科书和论文里都有。

所以拆走它们的收益接近于零,代价却是实打实的:公开版的 `/football` 预测和
`black_scholes` 会一起消失 —— 从开源产品里拿掉能用的功能,换不到任何 IP 保护。

**结论:阶段 2 对这四个子包不值得做。** 真正的护城河(services / agent_runtime /
risk / analysis / strategies)已经在私有仓,阶段 3(授权)和阶段 4(服务端化)
才是后面该投入的方向。

### 如果以后还是要拆,先知道这一点

`sports/` **不是自包含的**。四个文件反向依赖公开侧:

```
sports/predictor.py   ┐
sports/elo.py         ├─→ from aria_code.packages.aria_core.paths import aria_home
sports/calibrator.py  │
sports/tracker.py     ┘
```

方向本身是对的(私有依赖公开,而不是反过来),但它意味着私有仓**必须把
`aria-code` 列为依赖**,而不是简单 `cp -r` 一个目录就能独立跑起来。
`stochastic/`、`backtest/`、`portfolio/` 没有这个问题,只依赖 numpy / pandas / scipy。

## 法律与社区
- **接受外部 PR 前先要求签 [CLA](CLA.md)** —— 否则贡献者保留版权,你将无法把含其代码的部分 relicense。
- 许可证是**逐版本**的,且都不可撤销:≤4.1.2 是 MIT,4.2.0–4.4.5 是 BSL 1.1,0.44.0 起是 Apache 2.0。
  任何一版发出去之后都收不回来 —— 这正是为什么 `quant_engine` 的拆分要在下次发布之前做完。
  正式商业化前请律师复核 LICENSE/NOTICE/PRIVACY/CLA。

## 要避免的坑
- ❌ 把 license 校验当 IP 保护 —— 校验能被绕过;**真正的保护是编译**。
- ❌ 在免费壳里硬 `import` 专有引擎 —— 必须可选 + 降级(已用 `is_available()` 约束)。
- ❌ 无 CLA 就接受大量外部贡献 —— 会锁死你的 relicense 能力。
