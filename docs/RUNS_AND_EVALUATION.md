# 运行归属、故障定位、恢复与质量评估

## 安装

项目增加官方 `langgraph-checkpoint-sqlite` 依赖，用于跨进程保存 LangGraph 状态。更新已有环境：

```bash
uv pip install --python .venv/bin/python -e .
```

## 每次运行的归属

原来的审查/修正命令保持可用：

```bash
.venv/bin/python -m src.cli --file examples/order_project.diff --source-root examples/order_project --mode fix
```

提供源码上下文时，必须使用输入 diff 对应的改动后版本。程序在创建运行和调用模型前，按 diff 新侧检查已提供文件的内容、行号、换行标记以及新增/删除文件状态；不一致时拒绝执行，CLI 返回退出码 2，不覆盖已有输出。未提供的文件仍可按 diff-only 方式审查。局部 diff 无法证明既有文件在未展示区域也属于同一版本，调用方仍需保证来源一致。

不准备 diff 时，可用 `--target path/to/module.py --task "检查这个文件，有错误就修正。"` 审查并自动写回指定源码，无需指定 mode 或写入开关。`manifest.json` 和结构化结果的 `review_scope=file` 标识整文件范围，`target_file` 记录明确提供的绝对目标。输入保存完整脱敏快照和可用 `auto_write_root` 边界，任务准入决策及有效 `write_root` 进入检查点；恢复复用该版本和已完成决策，不重新读取磁盘替换输入或猜测权限。只审查或只生成补丁的任务不会设置有效写入根。写入前仍核对磁盘版本及历史回执。`--target` 与 `--file` 互斥，报告/补丁输出不能覆盖目标源码。两种范围的质量实验应采用对应的人工缺陷基准，不能把整文件发现当作 diff 新增缺陷评分。

也可以完全不传 target：`code-review "检查 path/to/module.py，有错误就修正"`。此时 `input.json` 只保存原始脱敏请求和项目边界；`task_interpreter` 在 `target_resolution` 阶段提取并验证完整文件引用，读取快照后将真实 `file_paths`、源码哈希、文件内容和任务决策一起提交到检查点。报告及 manifest 记录最终绝对目标，修正直接写入该原文件。`input_hash` 对应初始请求，实际文件版本另由 `source_hashes` 标识；不要把空的初始 `file_contents` 当作没有读取源码。

恢复时，已完成的文件定位和快照不重新执行，后续新增同名文件也不会改变目标。任务解析尚未提交就失败时可以重新执行该节点；已完成定位但检查点丢失或为空时停止，不能用当前文件重建旧运行。纯自然语言目标缺失、不唯一、越界，或任务中含不支持的动作时，不启动下游审查或写入。

当前自然语言入口支持多个明确文件和显式目录路径，目录最多展开 20 个源码目标，仍受输入预算约束。`target_files` 记录多个绝对目标，`source_hashes` 标识目标快照。专家由 `review_planner` 动态选择，`review_plan` 保存选择理由和具体问题；`dispute_reviews` 保存单轮定向复核，`failure_diagnoses` 保存修复失败分类和下一步指导。

项目工具先使用受限目录清单，按需读取相关源码、配置和测试。首次读取的脱敏内容保存在私有 `context_snapshots/<路径哈希>.json` 和检查点；公开结果只记录 `context_hashes`，不导出完整上下文。恢复复用已读取快照；尚未读取的文件如果已改变则拒绝读取，写入前已用上下文发生变化则报 `CONTEXT_VERSION_CHANGED`。工具读取不增加修正权限，已有测试只有明确包含在目标范围内时才允许修改。

当前工作流版本为 3。版本 3 之前的手动分发和节点布局不兼容，必须新建任务；`--allow-code-change` 不能绕过布局检查。版本 3 内兼容的代码变更仍需明确允许后恢复。重新运行会保留旧报告、备份和历史快照。

终端与报告会给出本次 `Run ID`。默认输出如下：

```text
output/
  latest.json                         最新一次发布的归属、状态和兼容文件 SHA-256
  latest-review.json                  最近一次 review 的运行目录指针
  latest-fix.json                     最近一次 fix 的运行目录指针
  review.md / fix-review.md           便于查看的报告副本，报告内包含 Run ID
  repair.diff / tests.diff            仅最新运行存在有效候选时发布
  runs/<RUN_ID>/
    manifest.json                     本次运行、会话、依赖版本、代码版本和产物清单
    input.json                        恢复所需的脱敏输入，私有文件
    checkpoint.sqlite                 持久化图状态，私有文件
    events.jsonl                      追加写入的结构化执行事件
    diagnostics.json                  故障及是否已恢复的诊断视图
    result.json                       可用于评分的结构化结果
    review.md 或 fix-review.md         本次运行报告
    candidates/repair.diff             通过静态与模型回归验证的候选
    candidates/tests.diff
    writes/<文件路径哈希>.bak             任务决定写回时的原始文件备份，私有文件
    writes/<文件路径哈希>.json            对应路径、原始/候选哈希和写入状态回执
    drafts/<SESSION_ID>/attempt-N/     按 patch_generator、test_generator、validator、final 分目录保留快照
```

`validated_candidate` 要求：流程为 `proposed`，静态验证为 `passed`，模型回归为 `passed`。它仍不代表运行测试已经通过。没有完整源码、验证失败、模型回归失败或流程异常时，补丁只保存为 `draft`；根目录不发布为有效补丁。新运行没有有效补丁时，仅清理上一份 `latest.json` 中记录的有效候选副本，并要求文件路径、运行归属及 SHA-256 均一致；原产物仍保留在运行目录。无归属的已有文件、用户修改后的文件和无法核实的文件均保留，CLI 提示 `Preserved existing file`，`latest.json` 的 `preserved_outputs` 记录保留原因。这些文件不会列入本次 `exports`，不能通过“文件存在”判断本次运行生成了有效候选。

各阶段草稿独立保存，规范化补丁不会覆盖模型原始补丁；事件引用的路径和哈希可以定位生成阶段及验证阶段的确切版本。

每个产物记录 SHA-256。发布前验证哈希，使用文件锁与原子替换，最后写入 `latest.json` 作为发布完成标记。需要可靠读取时，先取 `latest.json`，再核对文件哈希；长期引用应使用运行目录中的文件。并发发布时，最后完成发布的运行成为 latest。首次发布会把未带运行归属的旧默认输出归档到 `output/legacy/`，标为 `legacy_untracked`，不冒充新的运行结果。

`--output`、`--patch-output`、`--test-output` 仍可指定兼容文件位置，三者不能相同，也不能覆盖私有运行历史。`--run-root` 可更改历史根目录。

## 定位哪个 Agent、哪一阶段出了问题

```bash
.venv/bin/python -m src.cli --inspect <RUN_ID>
.venv/bin/python -m src.cli --inspect <RUN_ID> --node security --phase schema_validation
.venv/bin/python -m src.cli --inspect <RUN_ID> --node validator
.venv/bin/python -m src.cli --inspect <RUN_ID> --finding <FINDING_ID>
```

以上命令读取已有记录，不调用模型。事件包含：

- `run_id`、`execution_id`、`span_id`、`event_id` 和单调递增的 `sequence`；旧记录的 `session_id` 仍可读取。运行追踪使用 `execution_id`，避免与需要脱敏的认证 Session 混淆；
- 节点名、执行阶段、修正尝试次数、时间与耗时；
- 输入摘要哈希、关联问题 ID、草稿路径和哈希；
- 模型调用与格式重答，以及模型返回的 token 用量（若提供）；
- JSON/Schema 校验失败、字段位置和错误类型；
- 安全错误码、异常类型、堆栈的模块/函数/行号；
- 静态验证失败的目标文件和语法行号。

例如 `security / schema_validation / MODEL_SCHEMA_INVALID` 表示安全专家返回的数据不符合 Schema；`validator / python_syntax / stats.py:1 / PYTHON_SYNTAX_INVALID` 表示候选文件语法失败。`diagnostics.json` 保留曾经失败的节点，后续同节点成功时标记 `resolved: true`。

日志不保存原始提示词、原始模型响应、异常消息或局部变量。格式失败时也不把无效响应原文放入重答请求。检查点需要保存已脱敏代码，因此运行目录默认仅当前用户可访问，文件默认权限为 `0600`；不要把检查点当作公开审查报告或提交到仓库。脱敏依然不能替代输入敏感数据管理。

正常模式默认使用 ReAct，无需启用参数。`react_step`、`tool_started`、`tool_completed` 事件关联专家、步骤、工具调用 ID、参数/结果摘要哈希和源码哈希；可用 `--inspect <RUN_ID> --phase tool_execution` 查看执行阶段。`react_step` 还记录工具名称列表和调用数量，便于定位批次协议或预算错误；不记录原始参数。合法批次串行执行，每个工具完成后保存检查点，中途失败恢复时只继续未完成的调用。ReAct 私有检查点另外保存脱敏的任务消息、工具请求和源码观察，以支持子图恢复；模型随工具调用返回的自由文本、模型思考字段及无效回答原文不保存。执行方式随输入一起保存，恢复时保留原设置，包括历史非 ReAct 运行；离线 scaffold 不调用模型。

提供 `--task` 时，`task_interpreter` 是首个任务节点，可通过 `--inspect <RUN_ID> --node task_interpreter` 查看模型请求、JSON/schema 阶段和诊断。`result.json` 中的 `task_intent` 保存通过、越界或需澄清的决策。越界/需澄清是任务准入结果，不是程序崩溃，报告和 manifest 的状态分别为 `unsupported` / `needs_clarification`；没有专家、补丁或写入执行，CLI 退出码为 2。改变要求需启动新运行，不能直接恢复这两类终止结果。已通过解析、在下游失败的运行恢复时复用原解析，保留任务约束和权限。

预算收尾通过 `react_finalization` 事件记录角色、步骤、已用次数和剩余工具次数；超额批次通过 `tool_batch_rejected` 记录整批拒绝，调用均不执行。收尾请求不携带工具定义，只能基于已有观察输出最终 JSON，格式错误最多重答一次，总模型请求仍不超过 10 次。收尾阶段中断可复用观察继续，不重复已完成工具。报告显示 `Context collection limited`，不因未发现问题而直接批准；拒绝收尾继续请求工具时报告 `REACT_FINAL_ANSWER_REQUIRED`，不会无限搜索或伪造成功。

`result.json` 的 `reports.<专家>.tool_observations` 保存受控检索摘要，包括 `truncated`、`unavailable` 和 `match_count`，不包含查询词或原始参数。这些摘要与源码一起传给仲裁与争议复核，并随检查点恢复。报告中的 `Evidence limits` 提示搜索不完整；`tests.missing_coverage` 目前只作为待核实的候选观察，不计为已确认缺陷，不触发自动修正。静态零匹配不能替代权威覆盖率证据。

输入/命令行校验失败会在创建运行前拒绝，不存在对应 Agent 执行。SDK 内部网络重试属于同一次程序级模型调用；事件记录程序调用及 JSON 重答，不提供底层 HTTP 请求转储。

## 恢复执行

```bash
.venv/bin/python -m src.cli --resume <RUN_ID>
```

恢复使用保存的原始 diff、源码、专家选择和模式，不能同时提供新的 diff 或源码。它打开同一个运行 ID，新增会话 ID，并保留之前的事件和草稿：

- 节点异常或进程中断：从 LangGraph 已提交的检查点继续，仅执行未完成任务；并行阶段已保存的成功任务不会重复调用。
- 修正验证或模型回归被拒绝，且旧流程已经结束：保留审查结果，从修正规划开始增加一次尝试。新一轮与普通重试共用初始化逻辑，先提取上一轮代码补丁、静态验证和模型回归摘要作为 `previous_attempt`，再清空候选产物及验证状态。可用 `--additional-fix-attempts 2` 明确增加两次。
- 图已完成但最终产物写入中断：复用已完成状态，重新完成结果保存。
- 已成功完成、明确越界或需要澄清的运行禁止再次 resume，改变任务应启动新运行。

恢复前同样检查保存的源码是否匹配 diff 新侧，因此旧版本程序保存的不一致输入也会被拒绝。每次恢复使用运行锁，禁止同一个运行同时恢复。代码版本与原运行不同时默认拒绝；确认契约兼容后，才使用：

```bash
.venv/bin/python -m src.cli --resume <RUN_ID> --allow-code-change
```

自然语言决定写回的运行，会在静态和模型回归通过后调用 write_file。写入前检查全部目标；每个文件先保存原始字节备份和 prepared 回执，再原子替换并标记 written。文件写入节点中断时恢复同一候选，哈希匹配的已完成文件只确认、不重复覆盖。原文件权限与 CRLF 换行保留；新增文件没有旧内容备份。多文件写入不是事务，失败报告的 `write_result` 会显示 partial/unknown 等进度，不能据流程失败推断磁盘未改变。可用 `--inspect <RUN_ID> --node file_writer` 排查。备份不加入公开报告；需要回退时先核对回执中的路径及哈希。写入不执行目标代码或测试。

所有新增回归测试统一放在 `generated_tests/`，框架测试保留在 `tests/`；只生成候选时，测试 diff 也使用独立目录。写回模式下，新增 Python 回归测试使用 `generated_tests/test_<名称>__run_<运行 ID 的 12 位哈希>.py`，在静态校验和模型复审前确定路径。不同运行保留各自的测试，同一运行的重试、重复校验和恢复保持路径不变；已有快照的测试修改仍使用原路径。报告、补丁和写入回执记录最终文件名。若新增目标在写入前被其他操作创建，会以 `WRITE_NEW_FILE_EXISTS` 拒绝覆盖；已有文件版本变化仍报 `FILE_VERSION_CHANGED`。此前因同名测试失败的旧运行应重新启动任务以生成新候选，不要删除或覆盖已有测试来绕过保护。旧在线样例和历史生成测试已清空。当前订单样例的新增测试写到其源码边界下的 examples/order_project/generated_tests/。

代码版本、依赖版本、模型名称、原始输入哈希和恢复会话都会留档。检查点反序列化使用明确类型白名单，禁用 pickle。恢复依赖完整可读的本地检查点与输入；损坏的数据库不会被猜测重建。进程在模型响应与检查点提交之间中断时，该请求可能重试，不承诺网络调用恰好执行一次。

库 API 默认保留无文件 I/O 的用法；传入 `run_root=` 才启用磁盘存储：

```python
from code_review import run_workflow, resume_workflow

result = run_workflow(diff, mode="fix", file_contents=sources, run_root="output/runs")
recovered = resume_workflow(result["run_id"], run_root="output/runs")  # 仅用于失败/中断运行
```

## 根据人工标注评分

每条 Finding 具有结构化 `category`，如 `security.sql_injection` 或 `correctness.pagination`。评分只使用最终 `confirmed` 发现；候选项不计为成功检出，被拒绝项不计入预测。

代表性订单样例的人工基准在 `evals/labels/order_project.json`，包含八类预设缺陷的类别、文件、期望行范围及源码哈希；diff 范围单独使用 evals/labels/order_project_diff.json。修改样例后，应重新人工确认并更新标注；不能直接把模型输出作为基准。

先评估指定运行中的结构化结果（不调用模型）：

```bash
.venv/bin/python -m evals.run_eval \
  --report output/runs/<RUN_ID>/result.json \
  --labels evals/labels/order_project.json \
  --output output/evaluation.json
```

默认按类别和文件做一对一匹配，优先分配定位正确的结果，明确标记为自动匹配人工基准。标题、描述、报告中的 APPROVE 或 SQL 关键词不参与评分。定位与检出分开统计，避免错一行同时计算为误报和漏报。每条预测最多对应一条基准；重复报告会增加 FP。

要核实语义判断、修正效果，生成与该运行结果绑定的人工标注模板：

```bash
.venv/bin/python -m evals.run_eval \
  --report output/runs/<RUN_ID>/result.json \
  --write-annotation-template output/human-annotations.json
```

模板中的每个确认问题由人工填写 `expected_id`，并设置 `reviewed: true`；误报填 `expected_id: null`。所有确认项都必须核对，未知 ID、重复映射、不同运行/结果或不同源码版本会拒绝评分。

`repairs` 可填写逐个缺陷的验收记录：

```json
{
  "expected_id": "sql_injection",
  "status": "passed",
  "method": "isolated_tests",
  "patch_sha256": "填写本次代码补丁 diff 文本的 SHA-256",
  "evidence": "人工检查过的隔离测试日志路径与结论"
}
```

`method` 支持 `human_review` 或 `isolated_tests`。这些记录是人工提供的验收证据，评分器只校验归属与格式，不执行日志中提及的命令、不自动应用补丁或运行测试。补丁哈希为 `sha256(report['patch']['diff'].encode('utf-8'))`，对应运行目录中的候选代码补丁。更新补丁或恢复后，旧人工标注不能直接复用。

```bash
.venv/bin/python -m evals.run_eval \
  --report output/runs/<RUN_ID>/result.json \
  --labels evals/labels/order_project.json \
  --annotations output/human-annotations.json \
  --output output/evaluation-human.json
```

评分输出包括：

| 指标 | 定义 |
| --- | --- |
| precision / recall / F1 | 一对一匹配后按 TP、FP、FN 计算 |
| false_discovery_rate | FP / (TP + FP)，误报占已确认预测的比例 |
| miss_rate | FN / (TP + FN)，已标注缺陷的漏报比例 |
| localization_accuracy | 已匹配且有位置基准的问题中，行号落在标注范围内的比例 |
| clean_false_alarm_rate | 干净样例中出现确认误报的样例比例 |
| repair_success_rate | 人工验收通过的修正数 / 已完成人工验收的修正数 |
| repair_verification_coverage | 已验收修正数 / 基准中可修正缺陷数 |

分母为零时返回 `null`，不虚构 100% 成功。没有人工修正验收记录时，`repair_success_rate` 为 `null`，模型回归通过率与静态状态另行展示。失败/未完成运行不分配质量分数，单独统计失败数量。多样例汇总累加 TP、FP、FN 后计算微平均，不直接平均百分比。

真实模型批量评估仍可运行：

```bash
.venv/bin/python -m evals.run_eval --mode review
```

它会调用模型，为每个样例保存独立运行记录，并输出 `output/evaluation.json`。批量评估统一加载新订单项目的整文件与 diff 两种范围，标签在 evals/labels/，加载器在 evals/cases.py。它们不能代表全部语言和企业代码库的质量。
