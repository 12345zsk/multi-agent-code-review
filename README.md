# Multi-Agent Code Review & Repair

基于 LangGraph 的代码审查与候选修正项目：8 类专家并行审查，仲裁问题后生成最小补丁、回归测试和完整报告。

课程主线是“多专家审查 → 问题仲裁 → 修正与验证 → 评估反馈”。[课程定位、优化优先级与六项汇报内容](docs/COURSE_PROJECT.md) 区分已实现能力和待验证方案，重点是协作效果与对照实验。

**当前范围：** 审查专家默认使用内置 ReAct 循环，无需命令行开关，按需用 `read_source` 读取本次输入的脱敏源码快照，最终输出结构化结果。默认修正只生成候选补丁；显式指定 `--mode fix --source-root <目录> --write-files` 后，校验通过的修正阶段会调用 `write_file` 修改本地源码及生成的测试文件，并保留备份。静态验证只在内存中检查 unified diff、应用上下文和 Python 语法；项目不运行目标代码或测试，运行测试的状态始终为 `not_run`。

- [架构与各文件职责](docs/ARCHITECTURE.md)
- [三组分工与维护约定](docs/DEVELOPMENT.md)
- [代表性真实模型审查样例与命令](examples/review_target.md)

## 安装

Python 3.11+，在项目根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

编辑本地 `.env`，设置 `DEEPSEEK_API_KEY`；可用 `DEEPSEEK_MODEL` 选择模型。也可以使用环境变量。真实凭据不得提交到 Git；模块导入、流程图和离线示例不要求凭据。

## 无需凭据的完整演示

```bash
python -m examples.repair_demo
```

该示例使用明确标注的**模拟模型响应**，经过生产代码中的 JSON 解析、8 专家汇合、仲裁、计划、补丁生成、内存验证和回归审查，输出平均值函数的修正与测试补丁。最终状态为 `proposed`，不代表真实模型准确率或运行测试通过。

仅查看 Graph 和占位结构：

```bash
code-review --graph --output output/workflow.mmd
code-review --scaffold --file examples/review_target.diff
```

## 命令行

```bash
code-review                                  # 审查暂存区
code-review --unstaged                       # 审查未暂存修改
code-review --branch main                    # 审查分支相对 main 的修改
code-review --commit abc1234                 # 审查某次提交，支持首个提交
code-review --file patch.diff                 # 自动保存到 output/review.md

# 生成修正和测试候选；显式允许读取当前目录下的已变更文件作为上下文
code-review --file patch.diff --mode fix --source-root .

# 指定专家并导出结构化报告
code-review --file patch.diff --agents bug_detector security --format json

# 内置 ReAct：专家按需调用 read_source，然后输出结构化审查结果
code-review --file examples/review_target.diff --source-root . --agents bug_detector security

# 直接写入本地修正和测试文件：先静态检查与模型回归，后备份、写入
code-review --file patch.diff --source-root . --mode fix --write-files
```

ReAct 默认作用于选中的审查专家；仲裁、修正规划和验证保持原有流程。专家先获得 diff、行号和可读源码目录，按需请求 `read_source(file_path, start_line, end_line)`。每个专家最多调用工具 3 次、请求模型 5 次（另有客户端内部网络重试）；每次读取最多 200 行、源码文本最多 16,000 字符。错误参数作为观察返回，未知工具或超出预算会明确失败。工具只访问输入快照，不读取任意实时文件；未传 `--source-root` 时只能使用 diff 和已有上下文。模型可以依据充分的 diff 直接结束，不强制为调用而调用。

正常模式要求模型支持原生工具调用，不支持时明确失败。CLI 和 Python API 均默认使用 ReAct，没有启用参数。`--scaffold` / `scaffold=True` 仍为不调用模型的离线占位模式。恢复时执行 `code-review --resume <RUN_ID>`，保留原运行保存的执行方式，包括历史非 ReAct 运行。`python -m unittest discover -s tests -p test_react.py -v` 可离线验证默认工具调用、限制及恢复。

`write_file(file_path, content)` 仅由校验后的 `file_writer` 节点调用，不开放给并行审查专家任意选择。它只接受本轮已通过静态和模型回归检查的完整候选内容。写入前检查全部目标，再逐文件复查：拒绝越界、符号链接、敏感文件、运行历史目录、来源内容发生变化或新文件路径已被占用的情况。现有文件保留权限及 CRLF 换行；原始字节备份与逐文件回执位于 `output/runs/<RUN_ID>/writes/`。新文件回执标明没有旧内容。首版不执行文件删除。

多个文件不是一个原子事务：遇到中断可能已写入部分文件，报告会标记实际进度，备份和回执仍保留。使用 `code-review --resume <RUN_ID>` 恢复时，已写入且哈希匹配的文件不会再次覆盖，原备份不会被新内容替换；目标被人工改动时拒绝覆盖。写入模式会随原输入保存，恢复时无需也不能追加 `--write-files`。导出的补丁是写入记录，不应再次应用到已修正文件。Python API 以 `run_workflow(..., mode="fix", file_contents=sources, run_root="output/runs", write_root=project_root)` 显式启用。

默认结果保存在**当前工作目录**下自动创建的 `output/` 中：审查报告为 `review.md`，修正报告为 `fix-review.md`，候选代码和测试补丁为 `repair.diff`、`tests.diff`。使用 `--format json` 时报告扩展名自动改为 `.json`。未指定报告路径时仍在终端显示报告，所有写入文件的绝对路径会显示在终端。输出目录已加入本项目的 Git 忽略规则。

`--output`、`--patch-output`、`--test-output` 可分别覆盖默认文件路径，父目录会自动创建。只有产生有效候选时才发布补丁；未产生有效候选的新运行会删除旧兼容补丁副本，历史产物仍保留在各自运行目录。

`--source-root` 是调用方显式提供上下文的入口，Agent 不会自行检索仓库。API 的 `file_contents` 与此等价：应提供**输入 diff 已应用后、修正补丁尚未应用前**的当前源文件。审查历史提交时，不应把无关版本的工作区文件当作该提交的当前源码。

默认修正最多尝试 2 次，使用 `--max-fix-attempts` 调整。未提供完整源码时，只检查补丁结构和范围，报告会说明无法完成内存应用及语法验证。新建文件的实际磁盘存在性仍需人工核对。

退出码：`0` 表示完成审查、生成候选或运行离线 scaffold；`1` 表示执行失败或流程未完成；`2` 表示输入、路径或凭据配置错误。发现代码问题会体现在报告 verdict 中，不自动把 CLI 退出码改为失败。

## 用自然语言指导任务

通过 `--task` 描述目标、重点和约束，仍使用 `--file` 或 Git 输入选项提供待审查的 diff：

```bash
# 审查，明确只选择逻辑与安全两类专家
code-review --file examples/review_target.diff --source-root . \
  --task "请只让逻辑与安全专家审查本次变更，重点检查边界条件和 SQL 注入。"

# 自动识别为 fix；生成候选代码与测试补丁，不写回源文件
code-review --file examples/review_target.diff --source-root . \
  --task "审查并修正确认的缺陷，保留现有接口，只生成候选补丁，不要修改本地文件。"

# 自然语言不能自行开启真实写入，仍需显式写入参数
code-review --file examples/review_target.diff --source-root . --write-files \
  --task "审查并修正确认的缺陷，验证后写回本地文件，保留现有接口。"
```

`task_interpreter` 在任何审查专家运行前，用当前模型解析完整请求，输出受校验的任务类型、专家范围、目标约束和写入要求。通过后的目标会传给专家、仲裁及所有修正模型节点；未明确限制专家时默认全部八类，“重点关注安全”不等于“只运行安全专家”。不传 `--task` 时保持原有命令行为，默认 review，不增加任务解析模型请求。

- **超出能力：** 返回 `unsupported` 和无法完成的原因。例如“审查并部署上线”“修复后运行目标测试”“新增登录功能”会整体拒绝，不会挑其中一部分执行。
- **意图不清或冲突：** 返回 `needs_clarification`，说明需要明确的信息；自然语言与显式 `--mode` / `--agents` 冲突、要求写入但未授权、要求不写入却提供写入参数，均停止。
- **解析出错：** 返回失败并记录 `task_interpreter` 的阶段诊断，不回退到任意任务。拒绝或待澄清时，CLI 退出码为 `2`，报告中明确本次未执行审查、修正或文件写入。

任务仍限定为已提供 diff 的代码审查与已确认缺陷修正，可生成候选回归测试、做静态/模型验证，并在显式授权后受限写入。自然语言不会开启运行代码/测试、联网、部署、自动提交、新功能开发或任意文件访问。语义边界依赖模型判断；schema、专家白名单、参数冲突和工具权限由程序约束，离线测试不代表真实模型对所有措辞的识别准确率。

Python API 使用 `run_workflow(diff, task="审查并修正确认的缺陷，只生成候选补丁", file_contents=sources)`；不必再指定 mode。结果中的 `task_intent` 保存解析结论，原始脱敏任务仅保存在私有输入和检查点，不进入公共 JSON 导出。任务解析本身是可追踪的图节点；恢复复用已经完成的解析，不重新猜测目标。`unsupported` / `needs_clarification` 运行不能直接恢复，修改请求后启动新运行。自然语言入口不能与离线 scaffold、graph、inspect 或 resume 合用。

## Python API

```python
from code_review import run_review, run_workflow

# 兼容的文本接口，现在也使用统一工作流
review = run_review(diff)

# 完整结构化结果
result = run_workflow(
    diff,
    mode="fix",
    active_agents=["bug_detector", "security", "test_coverage"],
    file_contents={"src/app.py": current_source},
    max_fix_attempts=2,
)
print(result["final_review"])
patch = result.get("patch")
if patch:
    print(patch.diff)  # 候选补丁；没有应用到仓库
```

可注入 `model=` 进行离线测试，或通过 `handlers={节点名: 函数}` 替换节点实现。`scaffold=True` 明确切换为不调用模型的占位模式，正常模式已接入业务节点。

## 工作流

```text
输入校验与脱敏 → 自然语言任务解析（有 task 时；越界/不清则反馈并结束）
  → 上下文整理 → 专家选择
  → 8 类专家并行 → 证据校验 → 问题仲裁
      ├─ review / 无已确认可修正问题 → 报告
      └─ fix → 修正计划 → 代码补丁 → 测试补丁
                 → 内存静态验证 → 模型回归审查 → 候选报告
                       └─ 失败且未到上限 → 重新规划
```

专家：逻辑、安全、质量、测试覆盖、架构、性能、依赖、兼容性。模型输出必须满足严格 JSON schema；格式错误最多要求模型重答一次。证据或问题 ID 不匹配会明确失败，不会静默接受。

报告行号按连续代码证据校正，定位不唯一时显示 `?`。仲裁器修订问题描述并给出严重性依据；`critical` 缺少已提供上下文中的影响及暴露范围证据时限制为 `high`。覆盖摘要只报告候选数量和已提供代码的审查范围，避免推断全仓库测试情况。模型的语义判断仍需人工复核。

候选补丁由程序重算 hunk 行数，依据已提供源码精确校正坐标，再生成标准 diff；上下文缺失或歧义时不会模糊应用。Python 回归测试使用标准库 `unittest`，静态检查其测试类结构，实际测试仍须在隔离副本中执行。

| 流程状态 | 含义 |
| --- | --- |
| `completed` | 选中的审查节点已完成；是否有问题由 verdict 和 findings 表示 |
| `proposed` | 已生成候选修正并通过模型回归审查；静态检查范围另行展示，运行测试未执行 |
| `incomplete` | 占位模式、缺失结果或回归判断信息不足 |
| `failed` | 节点错误或修正验证/回归失败且未恢复 |

## 测试与评估

```bash
python -m unittest discover -s tests -v  # 离线：流程、解析、补丁、CLI、脱敏
python -m evals.run_eval                # 真实模型调用：结构化发现与人工基准评估
```

离线测试不代表真实模型审查准确率。评估按确认状态、类别、文件和人工位置基准统计误报、漏报与定位；人工验收修正后才计算修正成功率。依赖漏洞专家不会进行联网查询，也不会凭空报告 CVE。

CLI 为每次运行生成 ID，在 `output/runs/<RUN_ID>/` 保存清单、结构化结果、事件、诊断、SQLite 检查点、草稿及有效候选。`output/latest.json` 指明最新产物归属和哈希；失败或无修正的新运行不保留旧补丁副本。默认报告路径仍可使用。

```bash
code-review --inspect <RUN_ID> --node security --phase schema_validation
code-review --resume <RUN_ID>
```

详见 [运行归属、故障定位、恢复与质量评估](docs/RUNS_AND_EVALUATION.md)，包含输出目录、恢复限制、人工标注模板和指标定义。

支持标准 unified 文本 diff，单次输入最多 40,000 字符，完整源码上下文最多 120,000 字符。超限需拆分；二进制、重命名和 combined merge diff 会明确拒绝，不静默忽略。凭据脱敏覆盖常见形式，不应依赖它处理任意格式的敏感数据；包含脱敏标记的补丁会要求人工重建。

GitHub Actions 仅保留无需模型凭据的离线测试；课程演示与真实模型评估通过本地命令进行。
