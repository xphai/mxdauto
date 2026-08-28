# Core v2 贡献与实施流程

本文件是 `F:\mxd\product\maple-automation-core` 的执行规程。它把 ADR、战术包、代码评审、CI 证据和阶段门禁串成一条可执行路径。

## 0. 当前阶段的硬边界

截至 2026-08-29，仓库处于 **G-1 Strategic PASS / G0 CONDITIONAL PASS / G1 Ready（未开始）**：

- Core v2 任务的允许输出限定为不可变状态、动作计划和 dry-run 结果；G0 最小 synthetic Replay、Shadow 与 clean smoke 已接入，但只属于工程证据；
- Core v2 不得调用真实 `InputSink`、键盘、receiver 或游戏窗口；
- Legacy 保持唯一真实输入下发权；
- Legacy 仅接受阻塞缺陷修复和迁移桥接，不接受新的控制逻辑；
- 未经 Stage Gate 批准，不得宣称 Canary、Certified 或 Core v2 接管已经开始。
- 当前可绑定远端事实为 run [`33204844985`](https://github.com/xphai/mxdauto/actions/runs/33204844985)，最终 sealed packet 的 successor 复验为 run [`33205169227`](https://github.com/xphai/mxdauto/actions/runs/33205169227)；失败运行统一登记在 `evidence/failures/failure-index.json`。
- `main protected=true`；required `quality` strict、PR review、管理员约束、linear history、conversation resolution 已启用，force-push/delete 已关闭；[PR #1](https://github.com/xphai/mxdauto/pull/1) protected squash merge 时 G0 PASS 生效。

当前战略事实源：

- `docs/ROADMAP.md`：G-1～G6 阶段链；
- `docs/adr/ADR-004-input-authority-and-bounded-lease.md`：输入所有权与双写 0；
- `docs/decisions/DEC-001-pilot-baseline.md`：唯一 Pilot 候选；
- `docs/REQUIREMENTS-TRACEABILITY.md`：需求—Gate—证据状态；
- `docs/gates/G0-GATE-CHARTER.md`：G0 强制门禁，当前决定为 `CONDITIONAL PASS`。

违反上述任一项的任务必须先暂停并升级到战略负责人，禁止通过“临时开关”绕过。

## 1. 开始任务

1. 从 `main` 同步并创建任务分支，例如 `task/<id>-<short-name>`。
2. 复制 `docs/templates/tactical-package.md` 到任务记录（建议放在任务系统或 PR 描述中），填写所有必填项。
3. 明确任务类型：`contract`、`runtime`、`replay`、`shadow`、`release`、`docs` 或 `legacy-bridge`。
4. 在战术包中列出允许触及的绝对路径、明确禁止触及的路径和所依赖的 ADR。没有路径边界的任务不进入实现。
5. 若任务改变状态、动作、Bundle、输入所有权、回放或阶段门禁，先更新对应 ADR；仅更新实现而不更新治理文件不算完成。
6. 每个战术包至少引用一个 `REQ-*`、一个阶段 Gate 和 DEC-001（涉及 Pilot 时）；G0 包逐项引用 G0 Gate Charter 的检查编号。

## 2. 实现约束

### Core v2

- Planner、Control、Recovery 只消费不可变 `WorldState`；不得直接读取原始帧或 Legacy 私有字段。
- 所有跨模块数据携带 `session_id`、`frame_id`、单调时间和必要的版本/代次。
- 所有动作使用 `ActionSpec → ActionHandle → ActionResult` 生命周期；超时、取消、过期、输入错误都必须终态化并释放全部按键。
- 只有 `ActionController` 可以访问 `InputSink`。其他模块提交语义动作，不直接写键盘或 receiver。
- 配置、模型、地图、路线、MovementProfile 和 receiver 必须通过 Runtime Bundle 绑定；禁止运行中手工替换单个资产。

### Legacy

- Legacy 是参考、数据来源和迁移适配边界，不是 Core v2 的决策真值来源。
- Legacy 输入所有权在 Shadow 阶段保持独占。新代码不得从 Legacy 借用输入写入点，也不得在 Shadow 中通过真实 receiver 验证 Core v2 动作。
- G0～G2 的 Core v2 真实输入调用数为 0；G3 只使用 ADR-004 定义的有界独占租约；所有阶段双写事件数为 0。
- 需要修复 Legacy 阻塞缺陷时，战术包必须说明缺陷、影响范围、回退方式和迁移后删除条件。

## 3. 本地开发循环

在仓库根目录运行：

```powershell
python -m pip install --requirement configs/requirements.lock
python tools/verify_dependency_lock.py --lock configs/requirements.lock --check-installed
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest schemas/runtime-manifest.example.json
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest bundles/candidate-core-v2-20260829-shadow/runtime-manifest.json
python tools/verify_bundle.py --bundle-dir bundles/candidate-core-v2-20260829-shadow --metadata-only --strict-g0
python -m mypy
python -m pytest --junitxml=evidence/ci-run/junit.xml --cov=maple_automation_core --cov-report=term-missing --cov-report=xml:evidence/ci-run/coverage.xml --cov-fail-under=90
python tools/run_golden_replay.py --runs 3 --manifest bundles/candidate-core-v2-20260829-shadow/runtime-manifest.json --report evidence/ci-run/golden-replay-report.json
python tools/run_shadow.py --manifest bundles/candidate-core-v2-20260829-shadow/runtime-manifest.json --report evidence/ci-run/golden-shadow-report.json
python tools/run_clean_smoke.py --output evidence/ci-run/clean-smoke-report.json
```

full-external 校验还需配置 `MAPLE_LEGACY_ROOT`、`MAPLE_MODEL_ROOT` 后运行不带 `--metadata-only` 的 strict 命令。任务涉及回放、Shadow 或 Bundle 时，战术包必须列出固定输入、输出路径、预期差异和 artifact hash。没有真实生成的输出不得填写为通过。

## 4. Pull Request 必填内容

PR 描述直接引用战术包，并至少包含：

- 目标和非目标；
- 允许/禁止路径；
- 关联 ADR 和契约版本；
- 正常、边界、失败、超时、取消测试；
- CI run 链接、commit、Bundle（如适用）和 artifact 名称；
- Shadow 任务的真实输入调用计数（必须为零）以及与 Legacy 实际输入的对照说明；
- Feature Flag、回滚步骤和已知限制；
- 若未完成 G1 前置项，明确列出未完成项，不将其写成认证结果。

### 评审顺序

1. **范围**：改动是否属于战术包的允许路径，是否引入未批准的功能？
2. **边界**：是否违反 ADR-001、ADR-002、ADR-003、ADR-004、ADR-006、ADR-007 或 ADR-010？
3. **输入**：是否出现 `ActionController` 之外的 `InputSink` 写入；Shadow 是否保持 Legacy 独占？
4. **证据**：测试是否绑定 commit；Bundle 是否绑定实际 hash；结果是否可回放？
5. **回滚**：失败时能否关闭 flag、恢复上一 Bundle 或退回 Legacy？

## 5. 合并与阶段门禁

- CI 任一必需检查失败，PR 不合并。
- workflow conclusion 与 `ci-evidence.json.status` 必须同时通过；collector metadata failed 的 run 即使 workflow 页面为绿色也不合并、不绑定 Gate。
- 直接 push 的成功 run 只证明该 commit 的工程事实；它不替代 branch protection、required checks、实际 PR 或评审签字。
- 文档、代码、测试和 Bundle 元数据必须在同一变更链中更新；治理文件后置视为未完成。
- G0 只放行可复现契约、schema、静态质量和 dry-run/Shadow 准备；它不放行真实输入接管。
- G0 的评审清单和当前状态以 `docs/gates/G0-GATE-CHARTER.md` 为准；本地测试数、workflow 文件或示例 manifest 均不单独产生 G0 PASS。
- G0 minimal synthetic Replay/Shadow/clean smoke 不等价于 G1；进入 G1 后必须扩展固定录像 corpus、人工 truth/split、完整感知/WorldState/Planner 和 Shadow taxonomy。
- Canary/Certified 需要独立 Stage Gate、现场 session、故障注入和回退演练；CI 绿灯本身不授予这些权限。

## 6. 完成定义（DoD）

任务只有在以下项目全部满足后才关闭：

- [ ] 战术包中的目标、非目标和路径范围与实际变更一致；
- [ ] 相关 ADR/契约/Schema 已同步，或明确证明无需变更；
- [ ] 正常和所有声明的失败路径有可重复测试；
- [ ] 关键事件包含 `session_id`、`frame_id`、WorldState/action 版本和时间；
- [ ] Shadow 任务的真实输入调用为零，Legacy 输入所有权未改变；
- [ ] 本地命令和 CI 检查通过，证据绑定当前 commit；
- [ ] Feature Flag、回滚和恢复步骤经过演练或明确标记为后续任务；
- [ ] PR 评审人确认无新增 Legacy 私有状态依赖；
- [ ] 产物路径、报告 ID 和 Bundle ID 可被下一位执行者复用。

## 7. 升级条件

遇到以下任一情况，停止相关实现，在战术包记录并请求 ADR/Stage Gate 决策：

- 需要改变 `FramePacket`、`WorldState`、`ActionResult` 或 Manifest 必填字段；
- 需要新增输入所有者、修改 Legacy 输入归属或在 Shadow 中调用真实 receiver；
- 需要放宽测试阈值、忽略失败检查或把示例 manifest 当成认证 Bundle；
- 需要跨越 G0/G1/Canary/Certified 边界；
- 回放出现非确定性差异，或新动作未生成唯一终态。
