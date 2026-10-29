# 多文件订单服务在线测试

本次统一使用 `examples/order_project/`，仅依赖 Python 标准库。框架离线模拟模型
`examples/repair_demo.py` 仍被 `tests/` 和 CI 使用，与本在线输入无关。

所有命令从本仓库根目录执行。先在本机配置 DeepSeek 凭据，不要把真实密钥放入命令、
样例或报告；以下模型调用由你手动发起。

## 文件与审查边界

```text
order_project/
  app/                    6 个本次审查、修正的源码目标
    pricing.py            金额计算
    repository.py         数据读取、取消订单、批量读取明细
    service.py            平均值、分页、汇总
    checkout.py           配置与金额函数的调用方
    formatting.py         独立的状态展示模块
    __init__.py
  settings.py             页码起点、优惠比例，只读上下文
  contracts.py            客户端依赖的公开字段，只读上下文
  clients/report_client.py  现有消费者，只读上下文
  seed.py                 显式创建内存 SQLite 数据，只读上下文
  tests/test_smoke.py      现有冒烟测试，只读上下文
  README.md               正常业务契约，无缺陷答案
```

约 13 个 Python 文件；`app/` 目标快照明显低于 40,000 字符，完整项目上下文低于
120,000 字符。新增生成测试会写到 **`examples/order_project/generated_tests/`**，
框架测试仍在仓库根 `tests/`。修正不得通过改契约、配置或调用方掩盖问题。

人工答案、版本绑定标签和独立验收脚本位于本文件后半部分、`evals/labels/` 和
`evals/check_order_project.py`。务必使用下面的 `--source-root examples/order_project`，
这些验收资料位于该边界之外，模型无法通过项目工具直接读取答案。

## 重点功能覆盖矩阵

| 重点能力 | 使用的场景／检查材料 |
| --- | --- |
| 自然语言准入、目标定位、自动决定写入 | app/ 目录的只读、候选和写回三种请求；task_intent |
| 动态专家分工 | 金额、数据隔离、契约和查询次数风险；review_plan 的理由与问题 |
| 跨文件 ReAct | 读取配置、公开类型、调用方、已有测试；tool_completed 与 tool_observations |
| 语法、逻辑、安全、兼容和性能审查 | 八类缺陷、源码证据、定位及独立验收 |
| 反误报与不确定性处理 | 已有测试、参数化 IN 查询、只读消费者；缺测观察保留 candidate |
| 仲裁与重复归并 | 对同一根因的不同专家候选和最终 findings；有争议时定向复核 |
| 修正及测试生成 | fix_plan、候选补丁、generated_tests 路径、validation 与 regression |
| 原文件写回及运行归属 | 写入前后的差异、writes 备份回执、latest 指针与各运行目录 |
| diff 与文件模式边界 | 全文件八类缺陷，对比 diff 仅有两个新增缺陷 |
| 评估与修正验收 | 版本绑定标签、人工映射及九项独立验收 |
| 恢复、坏补丁、预算、断网和版本冲突 | 在线实际触发时检查事件；根 tests/ 提供确定性回归验证 |

这是一套覆盖主要能力的测试素材和操作流程；单次在线运行不会保证触发所有条件分支，
也不预先保证模型能检出或修正全部问题。

## 1. 只审查，验证动态规划与跨文件 ReAct

```bash
DEEPSEEK_MODEL=deepseek-chat .venv/bin/python -m src.cli \
  --source-root examples/order_project \
  --output output/order-review.md \
  "只审查 app/ 目录中的现有缺陷，结合项目契约、配置、调用方和已有测试核实问题，不要修改文件。"
```

检查 `Dynamic review plan`：应根据真实风险分配专家、理由和问题，不要求执行全部专家。
检查 `tool_completed` 和 `reports.*.tool_observations`：是否取得 `contracts.py`、
`settings.py`、调用方或测试等额外证据。没有额外证据需求时可以直接输出，不强制调用工具。

每次运行都有独立 `output/runs/<RUN_ID>/result.json`，不要仅凭旧的同名报告判断成功。

## 2. diff 模式，验证只审查本次引入的问题

**在任何写回操作前运行**。`order_project.diff` 只引入折扣计算和 SQL 拼接两处变化，
其他缺陷属于既有问题，不应在此模式当成本次新问题报告。

```bash
DEEPSEEK_MODEL=deepseek-chat .venv/bin/python -m src.cli \
  --file examples/order_project.diff \
  --source-root examples/order_project \
  --output output/order-diff-review.md
```

输入源码必须仍匹配 diff 新侧；修正后重复使用原 diff 会被版本检查拒绝。

## 3. 只生成修正候选，验证不会写原文件

```bash
DEEPSEEK_MODEL=deepseek-chat .venv/bin/python -m src.cli \
  --source-root examples/order_project \
  --output output/order-candidate.md \
  "审查 app/ 目录并修正确认的缺陷，保持公开接口和业务契约，只生成候选代码与回归测试补丁，不修改本地文件。"
```

检查 `proposed`、静态验证、模型回归和候选归属。仅仅出现补丁文件不代表成功；
`runtime_validation` 仍为 `not_run`。本命令不会创建项目内的生成测试文件。

## 4. 审查并直接修正原文件

**先保存原始样例副本或 Git 版本**，便于重做同起点实验。不要覆盖尚未保存的人工修改。

```bash
DEEPSEEK_MODEL=deepseek-chat .venv/bin/python -m src.cli \
  --source-root examples/order_project \
  --output output/order-fix.md \
  "检查 app/ 目录中的代码，结合项目契约、配置、调用方和已有测试，修正确认的缺陷并直接修改原文件，保持公开接口，补充有意义的回归测试。"
```

这条请求要求生成测试，不要求框架运行测试；当前框架尚不自动执行目标代码。
通过验证后应只修改 `app/` 及允许新建的 `generated_tests/`；备份和回执保存在
仓库根 `output/runs/<RUN_ID>/writes/`。所有改动都必须与确认问题对应。

## 5. 独立验收：修正前失败，修正后通过

```bash
.venv/bin/python -B -m evals.check_order_project --project examples/order_project
```

该脚本不调用模型，在临时文件副本中运行，并只使用内存数据库；不改原样例，也不在
`output/` 留下历史文件。文件副本不是操作系统安全沙箱，不应用于不可信程序。

原始样例预期：**9 项检查中，正常行为检查通过，8 项缺陷检查失败或报错**，退出码 1。
修正完整后预期 9 项全部通过、退出码 0。生成测试不能替代本独立验收。

已有冒烟测试可单独运行，它们在原始样例上应通过，说明已有少量测试不代表完整覆盖：

```bash
(cd examples/order_project && ../../.venv/bin/python -B -m unittest discover -s tests -v)
```

新生成测试在项目根运行，例如：

```bash
(cd examples/order_project && ../../.venv/bin/python -B -m unittest discover -s generated_tests -v)
```

## 6. 检出、定位与人工评分

全文件运行使用 `evals/labels/order_project.json`，diff 运行使用
`evals/labels/order_project_diff.json`。将实际 Run ID 替换到以下命令：

```bash
.venv/bin/python -m evals.run_eval \
  --report output/runs/<RUN_ID>/result.json \
  --labels evals/labels/order_project.json \
  --output output/order-evaluation.json
```

标签绑定原始输入哈希，不能对已经修正后的新输入继续使用原标签。自动匹配采用类别、
文件和一对一规则；模型可能归并发现或选择其他合理类别，应使用现有人工标注模板复核，
不能直接把自动匹配分数当语义准确率。修正成功率仍需绑定本次补丁哈希的独立验收记录。

## 7. 任务边界、恢复和失败路径

越界请求应整体拒绝，不能审查一部分后假装完成部署：

```bash
.venv/bin/python -m src.cli --source-root examples/order_project \
  --output output/order-unsupported.md "审查 app/ 目录并部署上线。"
```

不明确的目标应要求澄清，不应猜选文件：

```bash
.venv/bin/python -m src.cli --source-root examples/order_project \
  --output output/order-clarification.md "帮我处理一下项目。"
```

需要演示恢复时，可在模型阶段中断一个新运行，再使用保存的 Run ID；保持源码和框架
代码版本不变，不同时运行另一个写回任务：

```bash
.venv/bin/python -m src.cli --inspect <RUN_ID>
.venv/bin/python -m src.cli --resume <RUN_ID> --output output/order-resumed.md
```

检查恢复是否复用已完成工具、原快照及写入回执。多文件写入中断可能已有部分文件改变，
必须结合实际回执判断。错误补丁、调用预算耗尽、模型断网和版本冲突不能由一份普通
输入稳定触发；这些分支由根目录 `tests/` 的离线回归测试确定性验证，不伪造在线成功结果。

## 人工预期与反误报检查（不传给模型）

| 缺陷 | 根因文件 | 跨文件依据／验收 |
| --- | --- | --- |
| 百分比被当作固定金额扣减 | app/pricing.py | settings 的 25% 经 checkout 传入，2000 应得到 1500 |
| 空订单平均值除零 | app/service.py | 空汇总和客户端均要求有效空结果 |
| 一基页偏移错误 | app/service.py | settings.FIRST_PAGE=1，第一页应是 A1、A2 |
| 汇总字段与公开契约不符 | app/service.py | contracts.OrderSummary 与 clients/report_client.py |
| 用户订单 ID 进入 SQL 语句 | app/repository.py | 注入字符串不能改变查询，含引号 ID 应正常查询 |
| 取消订单遗漏租户过滤 | app/repository.py | alpha 不能取消 beta 的 B1，状态必须不变 |
| 明细按订单逐次查询 | app/service.py | 明确的每页最多两次 SELECT 契约及可用批量函数 |
| 状态函数定义缺少冒号 | app/formatting.py | 独立模块不影响其他模块的冒烟测试，修正后需可导入 |

反误报点：已有测试确实存在；批量查询的 IN 占位符数量动态拼接不等于拼接用户值；
金额输入的类型和范围已有契约；数据库连接应由调用方管理；没有依赖变更和 CVE 证据。
空输入会先在平均值处失败，非空有效输入才触发缺失汇总键，不能统称所有输入 KeyError。

争议复核和失败诊断是否触发取决于模型实际输出；正常情况可能无需这些分支。仅有
`tests.missing_coverage` 观察时应保留 candidate、禁止自动修正，不能声称全项目没有测试。
