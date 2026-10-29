# 智能体生成的回归测试

本目录用于保存以本仓库为源码边界时生成的回归测试；框架自身的测试保存在根目录 `tests/`。

自然语言要求修正本地文件时，通过静态校验和模型复审的新测试会写入本目录。Python 文件名带有运行哈希，用于区分运行；只生成候选时，这些路径只出现在测试补丁中。项目不自动执行生成的测试。

请在包含对应候选修正的隔离副本中，按报告中的文件名单独验证，例如：

```bash
.venv/bin/python -m unittest discover -s generated_tests -p 'test_名称__run_运行哈希.py' -v
```

旧历史生成测试已清空。新的多文件在线样例使用 `examples/order_project` 作为源码边界，
其生成测试写入 `examples/order_project/generated_tests/`。操作和独立验收见
`examples/ONLINE_TESTING.md`。不要把不同运行、不同预期的测试合并为一次修正的验收结论。
