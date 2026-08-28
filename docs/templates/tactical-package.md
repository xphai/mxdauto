# Tactical Package：<PACKAGE_ID> <TITLE>

> 将本文件复制到任务记录或 PR 描述中再执行。删除尖括号占位符；所有 `[ ]` 必须在关闭任务前明确勾选或说明理由。

## 1. 元数据

| 字段 | 内容 |
|---|---|
| package_id | `<例如 G0-FRM-001>` |
| title | `<一句话目标>` |
| stage | `<G-1 / G0 / G1 / P1 / P2>` |
| type | `<contract / runtime / replay / shadow / release / docs / legacy-bridge>` |
| owner | `<负责人>` |
| reviewer | `<评审人>` |
| branch / PR | `<分支和 PR 链接>` |
| status | `<proposed / in_progress / blocked / done>` |
| created_at | `<ISO 8601>` |

## 2. 战略约束

- **关联 ADR**：`<ADR-001, ADR-002, ...>`
- **阶段目标**：`<本任务如何推进当前阶段>`
- **非目标**：`<明确不做的内容>`
- **Stage Gate**：`<放行条件、评审人和日期>`
- **当前 Shadow 约束**：
  - [ ] Core v2 仅 dry-run/Shadow，不调用真实 `InputSink`、键盘、receiver 或游戏窗口。
  - [ ] Legacy 保持唯一真实输入下发权。
  - [ ] 若任务需要变更以上两项，已暂停并取得独立 Stage Gate 决策：`<链接/记录>`。

## 3. 范围边界

### 允许修改的绝对路径

```text
<逐行列出，例如 F:\mxd\product\maple-automation-core\src\...>
```

### 明确禁止修改的路径

```text
<逐行列出，例如 Legacy 输入模块、未关联的 schema、发布目录>
```

### 现状证据

- 当前行为/文件：`<路径 + 行/函数或报告 ID>`
- 输入样本：`<固定录像、fixture、manifest、Event Tape>`
- 已知限制：`<事实，避免推测>`

## 4. 契约和接口

| 项目 | 当前版本/字段 | 本任务变化 | 兼容策略 |
|---|---|---|---|
| Frame/Observation | `<版本>` | `<none / add / change>` | `<策略>` |
| WorldState | `<版本>` | `<none / add / change>` | `<策略>` |
| Action lifecycle | `<ActionSpec/Handle/Result>` | `<none / add / change>` | `<策略>` |
| Runtime Bundle | `<manifest version>` | `<none / add / change>` | `<策略>` |
| Event Tape | `<schema version>` | `<none / add / change>` | `<策略>` |

如任一契约发生变化：

- [ ] 关联 ADR 已更新；
- [ ] contract test 已新增或更新；
- [ ] 回放兼容性和迁移步骤已写出；
- [ ] Legacy adapter 的依赖没有变成 Core v2 真值来源。

## 5. 实施步骤（可执行）

1. `<步骤、输入、输出和预期结果>`
2. `<步骤、输入、输出和预期结果>`
3. `<步骤、输入、输出和预期结果>`

### 依赖和阻塞

- 前置任务：`<ID/链接>`
- 外部资产：`<模型/地图/receiver/凭据；列出版本和 hash>`
- 阻塞条件：`<精确条件>`
- 升级对象：`<人/ADR/Stage Gate>`

## 6. 测试计划

| 层级 | 命令/fixture | 预期结果 | 实际证据 |
|---|---|---|---|
| Contract/Unit | `<命令>` | `<可观察断言>` | `<报告/日志>` |
| Component | `<命令>` | `<可观察断言>` | `<报告/日志>` |
| Golden Replay | `<固定 tape + Bundle + 命令>` | `<事件序列/哈希一致>` | `<replay_report_id>` |
| Shadow | `<固定输入 + 命令>` | `<Core 计划与 Legacy 实际对照>` | `<shadow_report_id>` |
| Fault/Smoke | `<故障或干净机命令>` | `<恢复/启动条件>` | `<报告>` |

当前 G0 最小本地门禁：

```powershell
python -m ruff check .
python -m ruff format --check .
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json schemas/runtime-manifest.example.json
python -m mypy
python -m pytest --cov=maple_automation_core --cov-report=term-missing --cov-report=xml --cov-fail-under=90
```

## 7. Telemetry 与证据

- 事件类型：`<例如 action.issued / action.terminal / shadow.diff>`
- 必带字段：`session_id=<...>`, `frame_id=<...>`, `world_state_version=<...>`, `release_id=<...>`
- 指标和阈值：`<名称、单位、阈值、采样窗口>`
- 日志/录像/Event Tape 路径：`<绝对路径或 artifact>`
- CI run：`<链接>`
- source commit：`<40 位 SHA>`
- Bundle：`<release_id + manifest hash>`
- test report：`<test_report_id>`
- replay report：`<replay_report_id>`

Shadow 任务额外填写：

- Core v2 真实输入调用次数：`<必须为 0>`
- Legacy 实际输入调用次数：`<实测值>`
- 计划/实际差异：`<报告 ID + 结论>`

## 8. Feature Flag 与回滚

- Flag 名称和默认值：`<名称 = off/on>`
- 可观测关闭方式：`<命令/配置>`
- 回滚 Bundle：`<rollback_release_id>`
- 回滚顺序：
  1. `<停止/取消动作并 release_all>`
  2. `<关闭 flag 或恢复上一 Bundle>`
  3. `<验证 Legacy 仍为唯一输入所有者>`
  4. `<收集 Event Tape、日志和现场 ID>`
- 回滚验证证据：`<报告/日期>`

## 9. 完成定义

- [ ] 所有实施步骤已执行并记录实际输出；
- [ ] 允许/禁止路径与 diff 一致；
- [ ] 正常、边界、超时、取消、陈旧/故障场景有测试；
- [ ] 每个 ActionHandle 有且只有一个 ActionResult（如适用）；
- [ ] 事件可沿 FramePacket → WorldState → Decision → Action 回溯；
- [ ] Shadow 阶段 Core v2 真实输入调用为零，Legacy 输入独占未改变；
- [ ] CI 和所需 evidence artifact 绑定当前 commit/Bundle；
- [ ] 回滚步骤已验证或明确标注未验证及后续任务；
- [ ] 评审人签字：`<姓名/日期>`。

## 10. 最终报告

```text
结果：<done / blocked>
变更：<摘要>
测试：<命令和结果>
证据：<ID/链接>
风险：<剩余事实风险>
回滚：<是否验证>
后续任务：<ID；没有则写 none>
```
