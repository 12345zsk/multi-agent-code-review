# 架构与实现说明

项目现已统一到 `src/workflow/` 的 19 个节点，其中 `task_interpreter` 仅在提供自然语言任务时调用模型，`file_writer` 根据已准入任务的写入决策执行。`run_review()` 保留为返回 Markdown 的兼容接口；原有独立 Graph、文本状态和六个旧 Agent 已被统一实现替代。

CLI 默认将报告和候选补丁保存到当前工作目录下的 `output/`，自动创建目录并显示保存路径。审查与修正报告分别为 `review.md`、`fix-review.md`，JSON 模式使用 `.json`；候选补丁为 `repair.diff`、`tests.diff`。显式路径参数优先，Graph 默认仍输出到终端。Python API 默认只返回数据，显式提供 run_root 或 write_root 可启用运行落盘及受限源码写入。

## 范围

已实现：8 专家模型审查、严格结构化输出、证据和位置校验、问题仲裁与去重、修正计划、候选代码/测试补丁、内存静态验证、模型回归审查、有限重试、Markdown/JSON 输出和 CLI。

内置能力：正常审查与修正中的专家默认使用 `model → read_source → model` ReAct 子图，无需启用参数，最终仍输出严格结构化 Findings。唯一工具只读本次输入的脱敏源码快照，详细限制见 README。离线 scaffold 不调用模型。

自动写入：自然语言要求修正本地文件时，任务解析节点结合源码边界决定有效 `write_root`，无需 CLI 写入开关；只审查、只给建议或只生成补丁时保持为空。diff 输入须显式提供 `--source-root`，`--target` 默认使用当前目录作为边界。`auto_write_root` 仅表示可用边界，不能单独触发写入；还需 fix 目标、持久化备份、确认问题、静态及模型回归通过。`file_writer` 调用受限 `write_file` 工具，输入及候选版本校验、原文件备份、回执和可恢复写入由 `src/file_tools.py` 负责。

不包含：联网漏洞数据库查询、执行被审查代码或测试、自动提交代码。Git 命令仅用于 CLI 获取用户选择的 diff，Agent 不会构造或执行命令。

输入有两种范围：`--file` / Git 输入使用 `review_scope=diff`，仅审查变更引入的问题；`--target` / `run_file_workflow()` 使用 `review_scope=file`，读取明确指定的现有源码文件并审查其已有缺陷。后者由 `src/source_input.py` 编码完整、仅含上下文的快照，不要求手工 diff，也不虚构文件新增。源码允许包含语法错误；候选修正必须通过语法验证后才可写入。绝对目标路径及相对文件名作为同一输入提供给任务解析器，避免范围不一致。

纯自然语言入口 `code-review "检查 examples/review_target.py，有错误就修正"` / `run_task_workflow()` 同样使用 file 范围，不要求指定 target。`task_interpreter` 使用严格的 `FileTaskIntent` 同时提取目标引用及审查/修正意图；引用必须是原任务中的完整字面路径或文件名。程序验证路径边界，仅允许明确路径或受限查找得到的唯一同名文件；不会按模型推荐替换文件，也不会回退到其他 Git 输入。当前一次只支持一个源码目标。修正仍通过原来的验证和写入链直接修改原文件。

## 实际 Graph

以下按当前生产代码绘制。外层包含 **19 个节点：14 个可调用模型的角色、5 个程序节点**，开始/结束、分组及边标签不计入节点数。蓝色为模型角色，绿色为程序节点。任务解析仅在提供自然语言目标时调用模型；其余正常审查专家默认内置 ReAct。

```mermaid
flowchart TB
    START(["输入自然语言任务、diff 或指定源码文件<br/>入口校验请求、脱敏与运行初始化"])

    subgraph admission["一、任务准入与专家分发"]
        task_interpreter["任务边界、目标定位与写入判断<br/>task_interpreter"]
        context_collector["整理差异、文件与定位上下文<br/>context_collector"]
        review_planner["按用户范围分发；默认全部专家<br/>review_planner"]
    end

    subgraph specialists["二、选中专家并行审查：各自运行 ReAct，输出经证据校验的 Findings"]
        bug_detector["逻辑与边界<br/>bug_detector"]
        security["安全与信任边界<br/>security"]
        code_quality["质量与可维护性<br/>code_quality"]
        test_coverage["测试缺口<br/>test_coverage"]
        architecture["模块与依赖方向<br/>architecture"]
        performance["性能与资源<br/>performance"]
        dependency["依赖与版本风险<br/>dependency"]
        compatibility["接口与兼容性<br/>compatibility"]
    end

    adjudicator["汇合、去重、确认或拒绝问题<br/>adjudicator"]

    subgraph repair["三、仅 fix 模式进入修正闭环"]
        fix_planner["修正规划：保留反馈并开启本次尝试<br/>fix_planner"]
        patch_generator["生成完整候选代码补丁<br/>patch_generator"]
        test_generator["生成候选回归测试或说明无需生成<br/>test_generator"]
        validator["内存静态验证：补丁、范围、语法<br/>validator"]
        regression_reviewer["模型复核缺陷修复与回归风险<br/>regression_reviewer"]
        file_writer["复查、备份、调用受限 write_file<br/>file_writer"]
    end

    reporter["汇总任务结果、问题、产物与诊断<br/>reporter"]
    FINISH(["返回结果；启用持久化时保存运行记录<br/>CLI 发布报告及满足条件的有效候选"])

    START --> task_interpreter
    task_interpreter -->|"通过或未提供自然语言目标"| context_collector
    task_interpreter -->|"越界、需澄清或已记录错误"| reporter
    context_collector --> review_planner
    review_planner --> bug_detector & security & code_quality & test_coverage
    review_planner --> architecture & performance & dependency & compatibility
    review_planner -->|"空专家列表；最终标记未完成"| adjudicator
    bug_detector & security & code_quality & test_coverage --> adjudicator
    architecture & performance & dependency & compatibility --> adjudicator

    adjudicator -->|"review、无确认可修正问题或审查未完成"| reporter
    adjudicator -->|"fix；审查完整且无阻塞<br/>存在 confirmed 且 fixable 的问题"| fix_planner
    fix_planner -->|"计划 ready 且无阻塞"| patch_generator
    fix_planner -->|"计划未就绪或已记录错误"| reporter
    patch_generator -->|"已生成非空补丁且无阻塞"| test_generator
    patch_generator -->|"补丁未就绪或已记录错误"| reporter
    test_generator --> validator
    validator -->|"静态通过；或缺少完整源码并有说明"| regression_reviewer
    validator -->|"静态失败且尚有尝试次数"| fix_planner
    validator -->|"错误、其他结果或达到上限"| reporter
    regression_reviewer -->|"模型复核失败且尚有尝试次数"| fix_planner
    regression_reviewer -->|"模型与静态均通过<br/>且任务决定写回"| file_writer
    regression_reviewer -->|"任务只需候选、证据不足、错误或达到上限"| reporter
    file_writer --> reporter
    reporter --> FINISH

    classDef modelAgent fill:#eef2ff,stroke:#5262c6,color:#172554
    classDef program fill:#ecfdf5,stroke:#2e8b70,color:#14532d
    classDef boundary fill:#fff7ed,stroke:#b77933,color:#78350f
    class task_interpreter,bug_detector,security,code_quality,test_coverage,architecture,performance,dependency,compatibility,adjudicator,fix_planner,patch_generator,test_generator,regression_reviewer modelAgent
    class context_collector,review_planner,validator,file_writer,reporter program
    class START,FINISH boundary
```

选中的专家在同一并行阶段汇合，未选中的专家不执行；不是八个专家依次串行。`review_planner` 当前只保留用户指定范围，不会根据代码风险自主挑选专家。默认修正上限为 **2 次总尝试**，包含首次；回到 `fix_planner` 时保留上轮失败反馈并清空旧候选。

`reporter` 是程序节点，不是模型 Agent。静态检查、模型回归和真实运行测试是不同范围：当前项目**不执行目标代码或测试**。只有 `proposed`、静态通过且模型回归通过的结果，才发布为有效候选；缺少源码时即使得到修正建议，也保留验证限制，不能进入写入节点。

图中的“已记录错误”是图内路由条件；在 CLI 默认启用的持久化模式中，节点异常会中断图，由外层服务捕获并调用报告函数，详见下方恢复图。

### 每个审查专家内部的 ReAct 子图

下面展示专家内部的执行阶段；实际子图只有 `model` 和 `tools` 两个节点，证据校验在专家外层处理函数中完成，不增加外层节点数。

```mermaid
flowchart LR
    INPUT(["专家职责、已准入目标<br/>diff 或完整文件快照、目标行号、可读源码目录"])
    MODEL["模型分析并选择下一步<br/>model"]
    TOOLS["Action：调用 read_source<br/>仅读取输入的脱敏源码快照<br/>Observation：追加 ToolMessage<br/>tools"]
    FINDINGS(["最终 JSON 与 schema 校验通过<br/>专家继续校验证据、定位与问题 ID<br/>返回结构化 Findings"])
    ERROR(["超出预算、非法工具、模型错误<br/>或最终格式重答仍失败<br/>专家失败，进入诊断与恢复流程"])

    INPUT --> MODEL
    MODEL -->|"需要补充上下文；每轮一个请求"| TOOLS
    TOOLS -->|"读取结果或可纠正的参数错误观察"| MODEL
    MODEL -->|"证据足够，可直接结束"| FINDINGS
    MODEL -->|"首次 JSON/schema 格式错误；最多重答一次"| MODEL
    MODEL -->|"协议、预算或请求失败"| ERROR
    TOOLS -->|"不可恢复的工具执行错误"| ERROR
```

ReAct 对新运行默认生效，不需要开关；模型不必为调用工具而调用。每个专家最多 3 次工具调用、5 次模型请求，客户端网络重试另计。工具读取不超过 200 行、源码文本不超过 16,000 字符。参数错误可作为观察交回模型纠正；越界工具、并行工具请求或超限会失败。`write_file` 由外层 `file_writer` 控制，不开放给这些专家自主写入。

### 运行追踪、故障与恢复

这是外层服务行为，既不增加模型角色，也不是成功流程中额外串行的一组 Agent。以下以 CLI 默认持久化模式为例：

```mermaid
flowchart TB
    RUN["各节点执行<br/>关联 run_id、节点、阶段与尝试次数"]
    SAVE["保存结构化事件、用量及检查点<br/>ReAct 包含已完成的工具观察"]
    FAIL["节点异常或进程中断<br/>保留已提交检查点；不把失败当成功"]
    REPORT["外层服务生成失败报告与诊断<br/>写入失败时核实已写文件、备份与回执"]
    INSPECT["人工定位<br/>--inspect RUN_ID --node 节点名 --phase 阶段"]
    RESUME["显式恢复 --resume RUN_ID<br/>校验输入、代码版本与检查点"]
    PENDING["有未完成任务：从待执行节点继续<br/>复用完成的解析、并行结果和工具观察"]
    RETRY["已结束但修正失败：增加尝试<br/>保留问题及失败反馈，回到 fix_planner"]
    DONE["图已完成但产物保存中断<br/>复用完成结果继续保存"]
    STOP["成功、unsupported 或 needs_clarification<br/>不直接恢复；改变目标后新建运行"]

    RUN -.->|"执行期间持续记录"| SAVE
    RUN -->|"异常"| FAIL --> REPORT --> INSPECT
    INSPECT -->|"修复原因后恢复"| RESUME
    RESUME --> PENDING
    RESUME --> RETRY
    RESUME --> DONE
    RESUME -->|"运行已终止且不允许重跑"| STOP
    PENDING --> RUN
    RETRY --> RUN
```

源码依据为 `src/workflow/graph.py`、`src/react.py` 和 `src/workflow/service.py`。如需无说明、由当前 LangGraph 直接导出的原始拓扑，可运行 `code-review --graph --output output/workflow.mmd`。

选择全部专家是默认行为。调用方可以通过 `active_agents` / `--agents` 指定子集；提供 `task` / `--task` 时，入口模型也能解析用户明确限定的专家范围。没有基于风险自动选择专家的路由，任务解析本身会产生模型调用成本。空专家列表可到达报告，但不会给出完成审查的结论。

`src/task_intent.py` 实现任务准入。严格 schema 约束 accepted / unsupported / needs_clarification，拒绝结果不能携带一个替代执行任务。越界与混合越界请求整体停止；意图不清、模式或专家冲突、写入要求与显式权限冲突时停止并提示。通过的规范化指导传给全部相关模型角色，但不能覆盖问题证据、补丁范围和工具权限。原始任务先脱敏，只有规范化目标进入公共报告。任务节点和决策进入检查点，恢复复用已完成解析；拒绝或需澄清的运行必须修改请求后新建，不能直接 resume。

每个专家在外层图中执行一次；所有选中专家在同一并行阶段完成后，仲裁器汇合结果一次。并行报告按专家 ID 合并，异常转换成节点名和异常类型，不输出原始 provider 错误。

## 8 类专家

| ID | 关注范围 |
| --- | --- |
| bug_detector | 控制流、边界、空值、异常和返回值 |
| security | 注入、认证授权、反序列化、敏感数据和信任边界 |
| code_quality | 可维护性、重复与具体复杂度问题 |
| test_coverage | 变更引入的测试缺口，区分缺少证据和确定未覆盖 |
| architecture | 模块职责、依赖方向和耦合 |
| performance | 复杂度、资源、无界工作与并发 |
| dependency | 给定依赖变更及约束，不联网、不编造 CVE |
| compatibility | 接口、数据、配置和行为兼容性 |

模型输出的每个问题必须引用变更中的文件，并给出该文件 diff 或已提供源码内确实存在的连续证据。`review/evidence.py` 按新旧两侧分别匹配，不拼接不连续的 hunk。专家同时接收带行号的新侧代码。证据只有一处时自动纠正错位行号；有多处且没有匹配的行号提示时，或证据仅属于已删除代码时，位置显示为 `?`。ID 由专家、文件、校正后的行和标题计算，不接受模型任意指定。

证据校验只能证明引用存在，不能证明模型推断正确。仲裁器继续结合 diff 和源文件确认、拒绝或保留不确定项。

专家必须遵守代码声明的类型和契约，不把契约外的错误类型输入直接判为缺陷，也不凭有限输入断言整个仓库没有测试或函数是热路径。覆盖摘要由程序生成候选数量与审查范围，不直接展示未校验的模型自由文本。

## 结构化模型交互

`src/model_agent.py` 中的 `invoke_json()` 发送任务角色、schema 和脱敏后的 JSON 上下文。`src/schemas.py` 使用 Pydantic 严格校验，禁止未知字段和类型转换。支持纯 JSON、完整 JSON 代码块和文本内容块。

JSON 或 schema 错误允许一次重答；不会回显原始错误响应。专家的内置 ReAct 每轮仅接受一个 `read_source` 请求，工具结果通过原生 `ToolMessage` 返回模型；仲裁和修正等直接结构化输出节点仍拒绝工具调用。无效工具调用直接拒绝。网络层由模型客户端设置 60 秒超时及最多 2 次请求重试；格式修复次数与网络重试次数是两个独立上限。

业务节点统一使用带 schema 的 `invoke_json()`；ReAct 复用相同的最终 JSON 解析和校验契约。原有生产流程未使用的 make_model_handler 适配器已移除。

`src/react.py` 提供内置的最小 ReAct 子图，模型调用和工具执行为独立节点，继承外层检查点。每位专家拥有独立的消息历史和调用预算；完成的读取后若模型请求失败，恢复复用已保存的工具观察。事件记录步骤、工具调用 ID、参数与结果摘要哈希、源码哈希和耗时，最终 Findings 继续经过既有证据校验及仲裁。历史运行恢复时保留保存的执行方式，内部兼容分支不作为新运行的用户开关。

## 仲裁与修正

仲裁结果必须恰好覆盖每个候选 ID。重复问题只能引用同文件中的规范问题，禁止未知 ID、自引用和链式重复。规范问题采用仲裁器修订后的标题、描述、建议、严重性和分级依据，避免直接沿用专家的无依据附加判断或最高严重性。

`critical` 必须同时提供严重影响与暴露范围的具体代码证据，并在分级依据中解释其关联；任一证据缺失或不在已提供上下文中，程序将其限制为 `high`。引用存在的检查不能替代影响和暴露范围的语义判断，后者仍由模型与人工复核。Markdown 与 JSON 均保存分级依据。只有 `confirmed && fixable` 问题可进入修正。

修正计划须覆盖全部已确认可修正问题，代码补丁的 ID 必须与计划一致。模型生成标准 unified diff，基线是调用方提供的**输入变更完成后**的当前文件，或 `--target` 读取的完整当前源码。每轮输出完整候选补丁，不累加上一轮候选。

`repair/patches.py` 根据实际 hunk 内容重算行数；提供完整源码时，只使用精确匹配的上下文校正错位坐标，再由 `difflib` 生成规范补丁。上下文不存在或定位歧义时拒绝猜测并反馈重试。规范补丁会替换模型原始产物，供模型回归审查、报告和 CLI 导出使用；公共输入 diff 解析器仍严格拒绝格式错误。

测试生成器输出测试文件 diff，Python 使用标准库 `unittest`，不引入 pytest 等新测试依赖。静态检查要求包含 `TestCase` 子类和 `test_` 方法，避免生成无法被现有命令发现的普通函数；这只检查结构，不代表测试充分或测试执行成功。无法生成/复用测试时须说明原因并返回 `not_needed`。

每次尝试统一增加计数并清空候选文件、补丁和验证结果，同时将上一轮补丁及验证/回归摘要放入 `previous_attempt`，供新一轮规划使用。默认最多尝试两次。

## 三种验证范围

| 字段 | scope | 检查内容 |
| --- | --- | --- |
| validation | static | 补丁规范化、diff 格式、路径范围、问题引用、内存应用、Python 语法和 unittest 结构 |
| regression | model | 模型判断候选是否解决原问题，以及是否引入可见的新问题 |
| runtime_validation | runtime | 始终 not_run：不执行目标代码或测试 |

内存验证器不使用 `exec`、动态导入或 shell：仅对字符串应用补丁，使用内置 `compile()` 和 AST 检查 Python 源码，编译结果不会执行。检查规则：

1. unified diff 的 hunk 行数、上下文和坐标必须一致。
2. 代码补丁只能修改输入 diff 涉及的文件，或 `--target` 明确指定的单个文件；测试补丁只能修改测试路径；二者不能重叠。
3. 禁止路径越界、Git 元数据路径、重命名、空变更和包含脱敏标记的候选。
4. 提供完整基线时，精确匹配每个上下文/删除行；不做模糊应用。
5. 对候选中的 Python 文件做语法检查；不验证其他语言的语法、依赖、类型或运行行为。

未提供完整文件时，完成结构和范围检查后将 static 标为 `not_run`，说明缺失的验证能力；模型仍可基于 diff 回归审查并产出带限制的候选。文件系统中的新文件是否已存在、Python 目标版本和测试适配性仍需人工核对。

## 状态与判定

- `completed`：审查流程完成，不表示代码无问题。
- `proposed`：修正/测试候选已生成且模型回归通过；运行测试未执行，静态检查范围另列。
- `incomplete`：scaffold、没有专家、缺失节点结果或模型回归不足以判断。
- `failed`：节点错误或未恢复的静态/回归失败。
- `unsupported`：自然语言请求中包含当前不支持的要求，整个请求不执行。
- `needs_clarification`：任务不明确，或目标、参数、写入权限存在冲突；不执行审查或修正。

报告中的 verdict 针对**输入 diff 或输入源文件**，不会因为生成补丁就把原来的 `REQUEST CHANGES` 改为 `APPROVE`。确认的中/高/严重问题要求修改；候选问题或流程失败要求讨论；只有选中范围已完成且没有这些问题时才可批准。

## 文件职责

```text
code_review/__init__.py       对外导出文本、diff、指定文件及纯自然语言 API
src/
  main.py                    兼容文本接口；模块运行委托统一 CLI
  cli.py                     Git/文件输入、显式源文件读取、报告与补丁导出
  contracts.py               dataclass 产物、共享状态和节点接口
  config.py                  默认模型与输入大小限制
  llm.py                     延迟读取环境配置并创建 DeepSeek 客户端
  model_agent.py             JSON/schema 模型交互及低层适配器
  react.py                   内置 ReAct 子图与只读 read_source 工具
  task_intent.py             自然语言意图及目标 schema、文件定位与写入决策
  source_input.py            完整引用校验、受限同名查找、安全读取与源码快照
  file_tools.py              受限 write_file、版本检查、备份与写入回执
  schemas.py                 各模型节点的严格响应 schema
  diff.py                    unified diff 解析、路径检查与内存应用
  privacy.py                 常见凭据形式的脱敏
  serialization.py           dataclass 转 JSON、脱敏与导出字段选择
  runtime/store.py           独立运行产物、原子发布、清单与归属
  runtime/tracing.py         节点/阶段追踪、安全错误码与诊断
  workflow/
    registry.py              8 类审查专家的职责注册
    graph.py                 基础流程、可选写入节点、异常边界、路由和重试
    service.py               公共结构化 API、输入校验和初始状态
    nodes.py                 上下文、规划与 Markdown 报告
    scaffold.py              显式离线占位模式，与正常业务分离
  review/nodes.py             专家、证据校验与仲裁
  review/evidence.py          连续证据匹配、行号定位与分级证据校验
  repair/nodes.py             计划、补丁、测试、内存验证与模型回归
  repair/patches.py           模型补丁行数校正、精确定位与规范输出
examples/
  repair_demo.py             使用模拟模型跑完整生产流程
  review_target.py           正式审查样例，有意保留五类缺陷
  review_target.diff         与正式样例一致的新增文件输入 diff
  review_target.md           缺陷清单、审查与候选修正命令
 tests/                      离线流程、解析、CLI、脱敏和异常测试
 evals/                      结构化检出、定位、人工修正验收与微平均评估
 .github/workflows/          离线 CI 和按需 PR AI 审查
```

`__init__.py` 用于定义包或导出接口。CLI 默认使用本地 SQLite 检查点，并在独立运行目录保存归属清单、事件和诊断；库 API 可用 `run_root=` 启用持久化。完整源码和输入 diff 不会出现在 JSON 报告导出中，但私有恢复输入和检查点会保存已脱敏代码，候选补丁本身也包含代码，需要按源码管理。运行、恢复及评估协议见 `docs/RUNS_AND_EVALUATION.md`。

凭据脱敏是常见模式的尽力处理，不是任意敏感数据的自动识别保证。不要提交真实凭据或依赖脱敏功能把敏感源码变成可公开资料。
