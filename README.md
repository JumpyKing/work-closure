# work-closure

A multi-model delivery loop skill for Claude Code, Cursor and Codex. The host model grades each task as S, M or L, plans it, implements it (directly, or through GLM on ZCode), sends the change to independent reviewers (Claude Sonnet through the `claude` CLI, Grok through `cursor-agent`), runs falsifiable acceptance checks, and keeps a checkpoint with stop-loss rules. The skill text is written in Chinese.

## 简介

work-closure 是一套多模型交付闭环 Skill：立项定级、规划、实现、自检、冻结、独立审查、按需外部步骤、收口，共 8 步。全程写检查点，同一问题连续失败 3 次即止损并做根因分析。

按改动本身的风险分三级，从 L 往下判断，命中即停：

| 级别 | 条件（命中任一即是） | 实现 | 审查 |
| --- | --- | --- | --- |
| L | 改鉴权、资金、密钥、数据、部署或 CI 配置；多里程碑任务 | GLM（经 ZCode） | Sonnet 与 Grok 两路，完整证据门 |
| M | 改对外接口或健康检查端点；跨两个以上相互独立的功能区 | 主对话或 1 个子代理，改动量大时交给 GLM | Sonnet 与 Grok 两路，审同一份 diff |
| S | 不命中 L、M 条件 | 主对话或 1 个子代理 | Sonnet 一路，只审 diff |

定级后主对话在开工的第一条消息里用一句话写明级别与理由，不等确认即继续；只在拿不准是否属于 L，或级别取决于只有用户知道的事实时，才先问用户。完整规则见 `SKILL.md`。

## 依赖

- 宿主：Claude Code、Cursor 或 Codex 之一。
- Python 3：`scripts/run_cli.py` 只用标准库，负责调用外部模型 CLI、原样落盘证据并校验实际模型。
- `claude` CLI：Sonnet 审查；宿主主模型不是 Opus 时，还用于 L 级的 Opus 规划。
- `cursor-agent`：M、L 级的 Grok 审查。
- ZCode 与 GLM：L 级实现，或改动量大的 M 级实现。
- `rtk`：被委派模型发出的 shell 命令统一以 `rtk` 开头，见 `references/work-order.md`。

只做 S 级任务时，有宿主和 Sonnet 审查路线即可使用。

## 安装

把仓库克隆到宿主的 Skill 目录，目录名保持 `work-closure`。Claude Code：

```bash
git clone https://github.com/JumpyKing/work-closure ~/.claude/skills/work-closure
```

Cursor 克隆到 `~/.cursor/skills/work-closure`，Codex 克隆到 `~/.agents/skills/work-closure`。

装好后按自己的机器调整：

- `references/routes.md`：`claude`、`cursor-agent`、`zcode` 可执行文件的位置，以及模型 ID、显示标签、ZCode provider。包装器按 Skill 目录内的 `scripts/run_cli.py` 调用，不用改。模型信息按 2026 年 10 月实测写定，换账号或模型目录变化后需要更新。
- `references/work-order.md`：账本目录 `~/.cursor/work-closure/tasks/` 与 `rtk` 的位置。

这些路径会作为 Python subprocess 的参数数组直接执行，`~` 不会被展开，请改成你机器上的绝对路径。

## 使用

在宿主里提出一个有明确交付物的任务即可触发，例如"给 formatDuration 函数加负数支持，并跑现有单元测试"。主对话会先写明级别与理由，再按该级别走完闭环。普通问答、纯调研任务不会触发。

## 目录

- `SKILL.md`：入口，含定级、8 步闭环、状态、止损与路由。
- `references/routes.md`：路线预检、各 CLI 的真实 argv 与模型校验规则。
- `references/review.md`：S/M 的 diff 审查，L 级的冻结清单与双审证据门，审查轮次预算，以及一轮全部 APPROVE 后默认交付的规则。
- `references/work-order.md`：工作单与检查点字段、账本位置、执行纪律。
- `scripts/run_cli.py`：唯一的 CLI 包装器；收到 SIGTERM、SIGHUP 或 SIGINT 时清理子进程组，仍写出 summary。
- `scripts/test_run_cli.py`：包装器的回归测试，只用标准库和假子进程，不调用真实模型 CLI；运行 `python3 -I -B scripts/test_run_cli.py`，约 30 秒。

## 许可证

[MIT](LICENSE)
