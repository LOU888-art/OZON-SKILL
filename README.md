# OZON 跨境电商 Codex 工作流

这是一个供 OpenAI Codex 使用的项目级 Skill，覆盖商品资料整理、选品初筛、OGL 定价与利润核算、俄语商品卡、合规复核和运营报告。

Skill 入口：

```text
.agents/skills/ozon-crossborder-workflow/SKILL.md
```

在 Codex 中最简单的调用方式：

```text
使用 $ozon-crossborder-workflow，读取我上传的 OGL定价工具表.xlsx 和商品表，
完成利润核算、选品分级和俄语商品卡草稿。不要执行上架或改价。
```

详细安装和使用说明见 [Codex 使用说明](docs/CODEX使用说明.md)。
