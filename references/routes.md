# routes — 模型路由与 CLI 包装

除 S/M 在 Claude Code 宿主内用 Agent 工具指定 Sonnet 审查这一条路线外，所有 `claude` CLI、cursor-agent 与 ZCode 调用一律经过已安装的包装器 `~/.cursor/skills/work-closure/scripts/run_cli.py`，由它原样落盘证据并校验实际 provider/model。Agent 工具路线不经包装器，模型由宿主保证，报告由主对话原样存入账本，只读核对见 `references/review.md`。规划与审查均为只读，不具有业务写权限。每次包装器调用使用本次全新的唯一证据目录；包装器对已存在且含文件的证据目录拒绝覆盖。

## 立项预检

立项时只对本次级别要用的外部路线各预检一次，不在半途才发现路线不可用：

- `claude` CLI：`~/.local/opt/node-v24.16.0-darwin-arm64/bin/claude --version` 能执行；首次使用或 CLI 升级后，经包装器跑一次只回一个词的 canary，确认 summary `model_match=true`。
- cursor-agent：`~/.local/bin/cursor-agent --list-models` 的输出包含本次请求的模型 ID。
- ZCode：`~/.local/bin/zcode` 能执行，profile 文件存在且字段完整。

预检不通过时，在立项阶段换用已核实的路线或置 `blocked`，并写入检查点。

## 角色与精确模型 ID

- 规划（仅 L 级且宿主主模型不是 Opus）：本机 `claude` CLI，模型 `claude-opus-5-5`，`--expect-model claude-opus-5-5`。
- 实现（L 级，或改动量大的 M 级）：ZCode，模型 `GLM-5.3-Flash`，provider `account:bigmodel-individual-coding-plan`。
- 审查 A：Sonnet。经包装器时用本机 `claude` CLI，模型 `claude-sonnet-5-5`，`--expect-model claude-sonnet-5-5`；S/M 在 Claude Code 宿主内也可用 Agent 工具指定 Sonnet。
- 审查 B（M/L）：cursor-agent，请求模型 `grok-4.7-high`；`--expect-model Grok 4.7 256K High`（来源：真实 init 记录），`--cursor-model-alias "Grok 4.7  High"`（来源：当前 CLI 目录；`4.7` 后为两个 ASCII 空格）。
- UI：宿主可用的浏览器工具；都没有时该步骤置 `waiting_capability`，其余独立步骤继续。

2026-10-16 起 cursor-agent 的模型目录不再提供 Claude 模型（请求 `claude-opus-5-5-high` 返回 "Cannot use this model"），原经 cursor-agent 的 Opus 规划与 Sonnet 审查路线停用。同日经包装器调本机 `claude` CLI 的 Opus 与 Sonnet canary 均通过：`status=ok`、`model_match=true`，observed 分别为 `claude-opus-5-5` 与 `claude-sonnet-5-5`。

cursor-agent 角色（目前只有 Grok）有两个已核实的完整显示标签：主标签（来自此前真实 init 记录）经 `--expect-model` 传入，另一个（来自当前 CLI 目录 `--list-models` / 真实 init 记录）经可重复的显式 `--cursor-model-alias` 传入。两个标签的精确字节（含 Grok 当前标签中 `4.7` 之后的两个 ASCII 空格）不得折叠或改写。该集合只校验显示身份，不能据此声称真实上下文容量已实测。

cursor-agent 的 `--model` 取 CLI 请求 slug，而其 system/init 事件中的 `model` 字段是显示标签而非 slug，因此 `--expect-model` 传该角色的完整显示标签，并与显式 `--cursor-model-alias` 共同组成已核实的有限完整标签集合，仅按逐字精确相等比较：不在集合内的标签（带额外前后缀、版本后缀、High/Low、Fast 变体）一律判不符。没有传 alias 时只认 `--expect-model` 单一标签，行为不变。不得 substring、startswith、宽松正则、自动清洗或自动接受新标签。不得把 slug 用作 cursor 的 expect-model。每次新增别名必须先核对当前 CLI 目录、实际 init 或本机源码证据并记录来源，不允许任意 fallback。

## `claude` CLI 调用（Opus 规划、Sonnet 审查）

可执行文件 `~/.local/opt/node-v24.16.0-darwin-arm64/bin/claude`（2026-10-16 canary 所用版本 2.1.283）。用 Python subprocess 参数数组经包装器调用，禁止 shell=True、shell 拼接或字符串模板替换。argv 依次为：`--print`、从 UTF-8 文件读出的本次已保存工作单字符串、`--model`、该角色精确模型 ID、`--tools`、`Read,Grep,Glob`、`--restricted`、`--strict-mcp-config`、`--no-session-persistence`、`--output-format`、`stream-json`、`--verbose`。工作单字符串必须紧跟在 `--print` 之后：`--tools` 接受多个值，放在它后面的位置参数会被当成工具名吞掉。

该 CLI 没有 workspace 参数，以启动包装器时的进程工作目录作为 workspace：规划为本次真实任务目录；审查在 L 级为该路的隔离冻结副本，在 S/M 为基准的干净检出（见 `references/review.md`）。`--restricted` 去掉执行命令或代码的内置工具，把文件工具限制在工作目录内，并忽略用户、项目与本地 settings；`--strict-mcp-config` 跳过 MCP 服务器；`--tools Read,Grep,Glob` 只保留只读文件工具；三者共同保证规划与审查只读。模型校验用缺省字段规则：system/init 事件的 `model` 字段与 `--expect-model` 逐字相等，不传 `--cursor-model-alias`。

## cursor-agent 调用（Grok 审查）

可执行文件 `~/.local/bin/cursor-agent`。已实际核对 `cursor-agent --help`：`--mode` 的取值为 `plan` 与 `ask`，`plan` 是只读规划，`ask` 是只读问答。用 Python subprocess 参数数组调用，禁止 shell=True、shell 拼接或字符串模板替换。argv 依次为：`--trust`、`--mode`、`ask`、`--model`、该角色精确请求模型 ID、`--workspace`、本次真实工作目录、`--print`、从 UTF-8 文件读出的本次已保存工作单字符串、`--output-format`、`stream-json`。Grok 审查用 `--mode ask` 以产出直接审查报告，只读，不具有业务写权限。审查 workspace：L 级为该路隔离的冻结副本目录，S/M 为基准的干净检出；都不得包含另一路报告。报告来源兼容 `plan.md` 是为了兼容真实既有记录与异常协议形式，不要求故意用 `plan` 模式做审查。工作单字符串由调用方读出本次真实工作单文件内容后作为参数值传入，不传文件路径；包装器参数（label、requested-model、expect-model、out）由本次真实 workspace、已保存工作单与本次唯一证据目录生成，不得引用临时候选目录或固定占位路径。不修改全局配置。

## ZCode 调用（实现者 GLM）

可执行文件 `~/.local/bin/zcode`。argv 依次为：`--cwd`、实际候选工作目录、`--prompt`、工作单字符串、`--output-format`、`stream-json`。使用默认 headless 权限入口，不传 resume 或 continue，不使用不存在的 `--model` 参数或被忽略的 `ZCODE_MODEL`。

provider/model 只通过该子进程的环境变量 `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` 指向独立 profile 文件注入；不修改全局默认，不导出全量环境。profile 结构必须为：顶层 `schemaVersion` 等于 `1`；`config.providerConfigRules.providerRules`、`config.modelConfigRules.providerModelRules`、`config.modelConfigRules.manualProviderModelRules` 三个空规则数组；`config.defaultModelSelection.providerId`、`config.defaultModelSelection.modelId`、`config.defaultModelSelection.options.reasoningLevel`。缺少 `reasoningLevel` 即视为无效 profile。`reasoningLevel` 取 `low`、`high` 或 `max`，由规划者按任务复杂度选择；本轮定点修复选 `low`。

```json
{
  "schemaVersion": 1,
  "config": {
    "providerConfigRules": {"providerRules": []},
    "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
    "defaultModelSelection": {
      "providerId": "account:bigmodel-individual-coding-plan",
      "modelId": "GLM-5.3-Flash",
      "options": {"reasoningLevel": "low"}
    }
  }
}
```

ZCode 的模型校验参数：`--model-field payload.modelId`、`--provider-field payload.providerId`、`--expect-model GLM-5.3-Flash`、`--expect-provider account:bigmodel-individual-coding-plan`。显式 `--model-field`/`--provider-field` 不允许 `--cursor-model-alias`，同传时包装器报参数错误返回 2；ZCode 同事件 provider/model 仍仅与精确期望相等，不受 cursor 别名影响。`payload.modelId` 与 `payload.providerId` 必须取自同一事件并与期望精确相等；两者出现在不同事件、或缺一时，包装器判定 `model_mismatch`。

## 模型字段规则

- 缺省（不给 `--model-field`/`--provider-field`）：只认 `type` 为 `system` 且 `subtype` 为 `init` 的事件里的 `model` 字段（cursor-agent 为显示标签，`claude` CLI 为精确模型 ID），与 `--expect-model` 加显式 `--cursor-model-alias` 组成的已核实有限完整标签集合逐字精确相等比较。
- 显式两个字段（ZCode 取法）：model 与 provider 必须在同一事件同时出现，值与期望精确相等；任一事件只出现其一即记为不完整事件，最终判 `model_mismatch`；不允许 alias。
- user 消息或普通输出文本中出现的 model 字段不作为实际执行模型。
- 任一模型事件明确不符时，包装器立即终止其子进程并始终返回 3，禁止进入下游阶段，`child_exit_code` 保留真实信号值。未因包装器模型校验主动终止时，子进程自身非零退出码原样透传，status 为 `child_failed`；`model_match` 独立表示模型是否实际验证：没有任何模型事件时为 false，模型已实际验证时可为 true；无论真假，`child_failed` 都不满足通过门，不自动重试或继续安装，能力/执行缺失按 `blocked`、审查失败按 `review_incomplete` 处理。没有任何模型事件且子进程自身退出 0 时返回 3（status `model_mismatch`）。既有测试覆盖无模型事件且子进程退出 7 时保留 7 的用例。非 JSON 行只计数不报错。

## run_cli.py 证据与退出码

证据目录内：stdout 原样写 `stdout.jsonl`，stderr 原样写 `stderr.txt`；`summary.json` 为紧凑单行，记录 label、requested/observed model、去重后的 `accepted_cursor_models`（cursor-agent 与 `claude` CLI 为实际允许的有限完整标签集合，ZCode 为 null）、去重后的 models_seen/providers_seen、模型与 provider 事件计数、不完整事件数、exit_code、child_exit_code、termination、起止时间与耗时、stdout/stderr 字节数与 SHA256、usage（没有则记 `unavailable`）、status、executable、参数数量与参数数组的 SHA256。summary 不记录完整 command、prompt、环境或用户任务文本，长输入也保持紧凑。另存 `plan.md`（createPlanToolCall 携带的真实计划）与 `response.md`（根事件 `type=result` 且 `result` 为字符串时提取，兼容嵌套 `result.result`；response 可能只是计划的前言，不冒充唯一计划）。退出码优先级：包装器因模型明确不符主动终止子进程时返回 3（status `model_mismatch`）；没有任何模型事件且子进程自身退出 0 时返回 3（status `model_mismatch`，模型未验证）；子进程自身非零退出时原样透传真实退出码（status `child_failed`；`model_match` 独立表示模型是否实际验证，缺模型时 false、已验证模型时可为 true；无论真假都不满足通过门，主对话不得自动重试或继续安装，状态按能力缺失 `blocked`、审查失败 `review_incomplete` 处理）；命令不存在返回 127，status 为 `capability_unavailable`；证据目录已存在且含文件返回 2 并拒绝写入。既有测试还覆盖无模型事件且子进程退出 7 时保留 7 的用例。本包装器直接 subprocess 参数数组调用子进程，不声称 OS 沙箱。
