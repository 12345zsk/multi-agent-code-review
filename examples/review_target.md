# 代表性真实模型审查样例

`review_target.py` 是一个仅依赖 Python 标准库的订单服务代码文件，包含有意保留的缺陷。导入时不读写文件、不连接数据库、不执行示例。数据库连接由调用方提供；人工复现时仅使用内存数据库。

`review_target.diff` 将该文件表示为新增文件，让审查器看到完整的函数契约和实现。两个审查命令都使用真实配置的模型，不传 `--scaffold`，也不使用 `DemoModel`。

## 预期发现清单

本清单用于人工验收，不作为模型的输入，模型看到的只有代码与 diff。

| 函数 | 预设缺陷 | 对应专家 |
| --- | --- | --- |
| average_order_amount | 空列表触发除零，违反返回 0.0 的约定 | bug_detector、test_coverage |
| get_order_page | 起始位置按零基页计算，一基页的第一页被跳过 | bug_detector、test_coverage |
| find_order | 把外部 ID 拼进 SQL，违反“ID 作为数据”的约定 | security、bug_detector |
| cancel_order | 未检查 acting_user_id 与 owner_id，非所有者也能取消订单 | security、bug_detector |
| add_order_tag | 可变默认字典导致未传入 store 的调用之间共享状态 | bug_detector、code_quality |

全部 8 个专家默认启用，但不要求每个专家都产生问题。没有提供依赖变更或架构缺陷证据时，相关专家应返回空发现列表。

## 准备

在项目根目录配置本地 `.env` 中的 `DEEPSEEK_API_KEY`。以下命令沿用已有 `.venv`，自动创建当前工作目录下的 `output/` 并写入结果，不需要手动创建目录、提交或暂存 Git 文件。

## 只审查

```bash
.venv/bin/python -m src.cli \
  --file examples/review_target.diff \
  --source-root . \
  --mode review
```

## 审查并生成修正候选

```bash
.venv/bin/python -m src.cli \
  --file examples/review_target.diff \
  --source-root . \
  --mode fix \
  --max-fix-attempts 2
```

第二个命令会重新审查输入，不读取第一个命令的报告。代码修正和回归测试只作为候选 diff 导出，不修改原样例，不执行生成的测试。候选文件只有在相应模型节点产生非空产物后才写出，不能仅凭文件存在判断成功；重复运行时应结合本次报告核对结果，避免误读旧文件。

默认结果位置：

- 只审查：`output/review.md`。
- 审查加修正：`output/fix-review.md`、`output/repair.diff`、`output/tests.diff`；补丁仅在产生非空候选时写出。
- 添加 `--format json` 后，报告自动改为 `output/review.json` 或 `output/fix-review.json`。

`--output`、`--patch-output`、`--test-output` 仍可分别指定其他路径，并自动创建父目录。旧命令中显式指定的 `/tmp/...` 会覆盖默认目录；要使用 `output/`，请省略这些路径参数或改为 `output/...`。要减少模型调用量，可在任一命令中添加：

```bash
--agents bug_detector security code_quality test_coverage
```

## 人工验收

- 审查完成时通常为 `completed`；检查五类问题的证据、定位、确认状态和误报，而不只看发现数量。
- 修正候选成功时应为 `proposed`。提供完整源文件后，`validation` 应列出内存应用和 Python 语法检查结果，`regression` 展示模型回归判断。
- `runtime_validation` 始终为 `not_run`。人工审阅候选后，再在隔离副本中应用和运行测试。
- Python 测试补丁使用标准库 `unittest`。在隔离副本应用代码及测试补丁后，用 `python -m unittest discover -s tests -p 'test_review_target.py' -v` 执行生成的测试，不需要安装 pytest。
- 若状态为 `failed` 或 `incomplete`，检查本次报告，不把遗留产物或部分补丁当作通过。

修改样例后，可在项目根目录重新生成输入 diff：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from difflib import unified_diff

path = Path("examples/review_target.py")
diff = "".join(unified_diff(
    [], path.read_text(encoding="utf-8").splitlines(keepends=True),
    fromfile="/dev/null", tofile="b/" + path.as_posix(),
))
Path("examples/review_target.diff").write_text(diff, encoding="utf-8")
PY
```
