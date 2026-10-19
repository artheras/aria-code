# 训练 Aria 自己的模型：按量化研究的标准来做

一次微调就像一个交易策略：回测里漂亮的数字，很可能只是看过答案、样本太少或运气好。
所以这里先定规则，再碰数据。规则写进了代码（`aria_code/training/`），不靠自觉。

## 1. 对应关系

| 量化研究 | 训练 Aria |
|---|---|
| 不用未来数据（无前视偏差） | 锁定的测试集（`hard` 套件）永远不进训练；轨迹只要碰过评分文件就丢弃 |
| 样本外检验，最终测试集只用一次 | 调参只看验证集；锁定测试集只在最后决定是否上线时跑一次，并记录在案 |
| 多重检验 | 每看一次锁定测试集都记一笔；看得越多，结论越不可信 |
| 超额收益要有置信区间 | 和「基座模型 + Aria 提示词」成对比较，每题重复多次，用 bootstrap 区间和符号检验，区间不跨 0 才算提升 |
| 幸存者偏差 | 只有评分通过、且自身验收没有失败的轨迹才是样本；碰巧通过的不算 |
| 交易成本 | 同时报告每题耗时（和 token），分数涨了但成本翻倍要说出来 |
| 行业暴露 | 编程、金融、物流的样本数量单独统计，避免训出偏科的模型 |
| 样本内 vs 样本外 | 私有仓库 `artheras/aria-evals-holdout` 里放公开题的「孪生题」（同一能力，换数据、数字和措辞）。公开分数明显高于孪生题分数，说明公开题被模型见过或被调参调过头 |
| 情景生成 | 用题目模板换数据、换数字批量生成变体扩充训练集；但测试集必须是没见过的模板，才检验得到泛化 |

## 2. 现在能做什么、不能做什么

- **能微调的模型**：Vertex AI 的监督微调支持 `gemini-3.5-flash`（也是 Aria 的默认模型）和 `gemini-3.1-flash-lite`；2.5 系列随模型一起退役。Gemma 走开源模型微调路径。见 [Tune Gemini models with supervised fine-tuning](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini-use-supervised-tuning)。
- **数据还不够**：50 道题、每题几次，只有几十条轨迹；监督微调一般需要几百到几千条高质量样本。在数据够之前训练，得到的只会是过拟合。

## 2a. 步骤 0：先测各模型的成绩单

训练一个"路由器"之前，先回答一个更便宜的问题：在 Aria 真正会遇到的任务上，便宜的模型是否常常做得出来？
用同一批可验证的题目、同一套检查，跑几个模型，比较通过率、耗时，并算出"理想路由"能省多少：

```bash
# 默认只打印计划和调用次数，什么都不跑、不花钱
python -m aria_code.evals.scorecard run \
  --model aria=google/gemini-3.8-flash --model gpt=openai/gpt-5.4-mini --model local=qwen2.5:7b --local local \
  --suite evals/suites/core.yaml --suite evals/suites/operations.yaml --repeat 3 --out scorecard/
# 先用一道题冒烟，确认流程和花费：--only csv-quoted-fields --repeat 1 --yes
python -m aria_code.evals.scorecard report scorecard/ --cost aria=0.02 --cost gpt=0.05
```

- 每个模型的轨迹同时保存在 `scorecard/<名字>/trajectories/`：这是第一批可用于训练的素材，只有通过验证的才会被数据集构建器采用。
- 报告里的"理想路由"是**上界**：它假设事先知道哪个模型会做对。真实的路由器要猜，失败的便宜尝试也要付费。
  上界都很小，学出来的路由器就不值得做。
- 评测报告不含 token 数，所以成本用 `--cost 名字=美元/每次尝试` 自己估。
- 本地小模型（例如 7B）在笔记本上每轮要几十秒，300 秒做不完一题；超时记为"错误"而不是"答错"，不计分。

## 3. 步骤

1. **测基线（不训练）。** 用 `gemini-3.5-flash` 跑 Evals，`--repeat 3`，保存报告和轨迹。这是之后所有比较的基准。
2. **扩充训练数据。** 为 core 和 operations 的题目写参数化的变体生成器（换数据、换数字、换货主），每个模板几十个变体；`hard` 套件不生成变体，保持锁定。
3. **收集轨迹。** 在变体上跑 Evals（可以用更强的模型做「老师」，例如 `gemini-3.8-flash`），只保留验证通过的轨迹。
   **用原生运行时收集**：微调后的模型由原生运行时（Rust + Go）提供服务，它学到的工具必须是上线后
   真正拥有的工具（原生运行时的 8 个工具，而不是 Python CLI 的全部工具）：

   ```bash
   python -m aria_code.evals.runner evals/suites/core.yaml --runtime native \
     --model google/gemini-3.8-flash --repeat 3 --trajectories trajectories --report evals-core.json
   ```

   `--runtime native` 通过 `aria-native exec` 驱动 Go 运行时（`$ARIA_NATIVE_BINARY`、
   `$ARIA_GO_RUNTIME_BINARY`），并打开 `ARIA_EVENTS_FULL`：轨迹里是完整的工具参数与结果
   （密钥形态的值已遮蔽，结果超过 2 万字符截断）。Evals 工作流的 `runtime` 输入选 `native` 同理。
4. **构建数据集。**

   ```bash
   python -m aria_code.training.sft_dataset trajectories/ --suites evals/suites --out sft/
   ```

   输出 `train.jsonl`、`validation.jsonl`（Vertex Gemini 微调格式）和 `manifest.json`（每个输入文件的 sha256、规则、各领域样本数、被排除的原因、锁定的题目）。

5. **在 Vertex 上微调** `gemini-3.5-flash`，训练集用 `train.jsonl`，验证集用 `validation.jsonl`：

   ```bash
   aria-runtime training tune submit sft/ --name aria-v1 --bucket gs://<桶>/aria-sft \
     [--base gemini-3.5-flash] [--region us-central1] [--epochs N] [--adapter-size 4]
   aria-runtime training tune status sft/tuning-aria-v1.json
   aria-runtime training tune wait   sft/tuning-aria-v1.json      # 成功后登记到 tuned_models
   ```

   `submit` 要求数据集带 `manifest.json`，把文件按内容哈希上传到 Cloud Storage（同一路径从不覆盖），
   再创建监督微调任务；`sft/tuning-aria-v1.json` 记录任务名、基座模型、区域、每个文件的 sha256
   和 manifest 的 sha256——上线的模型可以追溯到它见过的每一个字节。`wait` 在任务成功后把端点以
   `aria-v1` 登记进 `tuned_models`，**不会**把它设成默认模型：先按第 6 步比较。
6. **在验证集上比较。** 用微调后的模型跑 Evals（`--runtime native --model aria-v1`，或在 CI 里直接写端点全名；同样 `--repeat 3`），然后：

   ```bash
   python -m aria_code.training.compare baseline/*.json --candidate tuned/*.json
   ```

   输出成对比较的平均提升、95% 区间、变好/变差的题数、符号检验 p 值和耗时。

7. **上线的门槛**（全部满足才进行下一步）：
   - 验证集上 `ci95` 的下限大于 0；
   - 没有任何一个领域（编程、金融、物流）的平均通过率下降超过 5 个百分点；
   - 平均耗时增加不超过 25%。
8. **锁定测试集，只跑一次。** 用 `hard` 套件比较基线和候选模型，结论记录在发布说明里。不通过就回到第 2 步，并且这次查看也要记录下来。

## 3a. 在 Aria 里使用微调后的模型

微调完成后，模型部署在一个 Vertex 端点上，按端点的资源名调用
（`tuning_job.tuned_model.endpoint`）。在 `config.json` 里给它起个短名字，选中即可：

```json
{
  "tuned_models": {"aria-v1": "projects/123456789012/locations/us-central1/endpoints/987654321"},
  "model": "aria-v1"
}
```

或在会话里 `/model aria-v1`（也可以直接写完整的端点名）。原生运行时对这样的模型：

- 一律走 Vertex 的 Gen AI SDK 路线（Chat Completions 没有记录对微调端点的支持），
  用 `gcloud auth login` 或应用默认凭据，不能用 Gemini API key；
- 用端点自己的项目和区域（包括 `us`/`eu` 多区域），与 `gcp_location`、
  `GOOGLE_CLOUD_LOCATION` 给基座模型设的区域无关——区域不一致会得到 404；
- 和基座模型一样经过合同、审批、验收、复核和交付报告。

比较微调模型与基座模型时（第 6 步），Evals 两边用同一套配置，只换 `model`。

## 4. 私有保留集（孪生题）

- **放在哪里**：`artheras/aria-evals-holdout`，结构与 `evals/` 相同（`suites/*.yaml`、`fixtures/`），每道题用 `twin_of` 写明对应的公开题。公开仓库里不出现保留集的任何内容。
- **怎么跑**：仓库设置了 `HOLDOUT_DEPLOY_KEY`（保留集仓库的只读 deploy key）时，Evals 工作流会拉取并运行保留集；没设置就跳过。
- **不公开什么**：保留集的报告和轨迹不上传为 artifact，日志不打印题目，成绩表只显示通过数、均值 ± 标准误，以及「公开 − 孪生」的差距和 95% 区间：

  ```bash
  python -m aria_code.evals.gap --public evals-*.json --holdout holdout.json --suites holdout/suites/*.yaml
  ```

  区间下限大于 0，就说明公开分数高估了真实能力。
- **不进训练**：保留集的题目、轨迹和答案永远不进训练集，也不按它生成变体。

## 5. 不做的事

- 不在锁定测试集上调参、挑检查点或改提示词。
- 不把评分文件、标准答案或 `eval.outcome` 放进任何训练样本。
- 不只报单次运行的分数；每个套件都带标准误。
