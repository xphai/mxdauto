# Maple Automation Core

> **当前阶段（2026-08-29）**：G0 工程基线建设中，Core v2 处于 **Shadow / dry-run 准备阶段**。当前仓库已交付领域契约、Event Tape、Manifest schema/校验工具和静态 CI；后续 Shadow runner 的输出限定为状态、计划和对照证据，不调用真实 `InputSink`、键盘、receiver 或游戏窗口。Legacy 保持唯一真实输入下发权。当前阶段尚未进入 Canary、Certified 或 Core v2 输入接管。

## 1. Core v2 目标与治理边界

本仓库的目标是交付 **Maple Automation Core v2**：
- 以可复现事件流为核心的自动化运行时。
- 建立采集、感知、决策、执行、恢复、回放闭环。
- 通过认证路线（Certified Bundle）实现可回放、可回退的发布过程。

### Core v2 与 Legacy 的边界

#### 允许继承（仅适配，不重构）
- 复用 Legacy 的已有检测思路与模型输入/输出数据。
- 复用可追溯的路线数据与采集资产（含地图/动作样本）。
- 保留最小启动与兼容适配能力（用于平滑迁移）。

#### 不允许直接复用（硬隔离）
- Legacy 的输入下发逻辑禁止成为 Core v2 决策入口。
- Legacy 的私有运行状态（位置信息、路径状态、按键状态）禁止直接作为 Core v2 的真值来源。
- 旧有流程中的隐式 Sleep/无约束重试，不得作为 v2 关键控制流。

#### 实施顺序（先策略后战术）
- **战略方向（5.6Sol Ultra）**：定义目标、ADR、阶段门禁、发布决策。
- **战术方向（5.6 Luna 最高）**：在 5.3 Codex Spark 未接入当前任务时，按可执行战术包完成 G-1 与 G0 的落地。

---

## 2. G-1 / G0 治理基线交付目标

### G-1（主线确认）
- 确认 Core v2 唯一主线：`F:\mxd\product\maple-automation-core`
- 冻结 Legacy 新功能开发，仅允许迁移桥接与阻塞缺陷修复。
- 固定 Pilot 试验边界：
  - 1 张地图
  - 1 套角色档案
  - 1 套地图/路线/模型/阈值集合
- 建立路线图-测试-录像-会话追踪矩阵。

### G0（工程基线）
- 已纳入仓库的基线：`runtime-manifest.schema.json`、Manifest 校验工具、Frame/WorldState/Action/Event Tape 契约及 GitHub Actions 静态质量门禁。
- 当前执行中：将 commit、Runtime Bundle、测试报告和回放报告绑定为可追溯证据。
- G0 退出前需完成以下四项并留下报告：
  - 单元/契约测试自检
  - 固定黄金录像回放一致性检查
  - Core v2 Shadow 对比评估
  - 干净机 smoke 自检
- `schemas/runtime-manifest.example.json` 是 schema 验证 fixture，不是现场认证 Bundle。

> 以上为本阶段最小放行门槛；四项报告、Bundle 证据和输入所有权审计齐全后，才可提交 G1 评审。

### 当前阶段的输入边界

| 范围 | 当前规则 |
|---|---|
| Core v2 | Shadow/dry-run；可生成 `ActionSpec`、模拟 `ActionHandle`/`ActionResult` 并写入 Event Tape |
| Legacy | 唯一真实输入下发者；继续承载现场输入 |
| 接管 | 当前阶段未启用；需独立 Stage Gate 批准，CI 绿灯不自动授予接管权 |

---

## 3. 文档与合约约定

- ADR 存放：
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-001-core-singleline.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-002-framepacket-contract.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-003-worldstate-only.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-006-action-lifecycle.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-007-atomic-runtime-bundle.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-010-ci-evidence-contract.md`
- 发布清单：
  - `F:\mxd\product\maple-automation-core\schemas\runtime-manifest.schema.json`
- 执行规程：
  - `F:\mxd\product\maple-automation-core\docs\ROADMAP.md`
  - `F:\mxd\product\maple-automation-core\docs\CONTRIBUTING.md`
  - `F:\mxd\product\maple-automation-core\docs\templates\tactical-package.md`

新增/修改任何运行时行为时，必须更新：
1. 对应 ADR 或补充变更说明；
2. 相关测试或 Golden 回放脚本；
3. Bundle 元数据。

---

## 4. 开发与测试方式（执行级别）

### 开发流程
1. 新需求先建战术包：`目标-输入-输出-门禁-回归`。
2. 仅改动符合 ADR 的模块边界。
3. 每次改动提交需附带：
   - 任务目标
   - 验收标准
   - 关键证据（测试 ID/录像 ID）
4. 代码评审优先检查点：
   - 决策是否只依赖 WorldState；
   - 是否违反 `ActionController` 以外输入归属；
   - Frame/WorldState 是否可追溯。

### 测试策略
- **契约测试**：Frame / WorldState / Action / Manifest。
- **回放测试**：固定黄金录像 + 固定 Bundle，验证确定性。
- **对照测试**：Legacy Shadow 结果与 Core v2 决策链对比。
- **异常测试**：陈旧帧、断流、ACK 失效、模型加载失败。

### 当前 G0 本地门禁

在 `F:\mxd\product\maple-automation-core` 根目录执行：

```powershell
python -m pip install -e ".[dev]"
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest schemas/runtime-manifest.example.json
python -m mypy
python -m pytest --cov=maple_automation_core --cov-report=term-missing --cov-report=xml --cov-fail-under=90
```

上述命令与 `F:\mxd\product\maple-automation-core\.github\workflows\ci.yml` 的当前基线一致。当前 CI 会上传 `coverage-xml`；黄金回放、Shadow 和干净机报告仍需作为 G1 前置证据接入，不以静态 CI 结果代替。

---

## 5. 路线图同步规则

- 无 ADR 变更禁止越过 G-1/G0 门禁。
- 任何发布必须绑定：
  - `runtime-manifest`
  - 至少 1 次回放一致性结果
  - 至少 1 次 smoke 清机验证
- 治理文档与代码同速更新；文档后置者视作治理未完成，禁止进入下一阶段。

## 6. 首批实施顺序

```text
ADR/战术包
→ FramePacket / WorldState / Action 生命周期
→ Event Tape 与固定 Bundle
→ G0 静态 CI
→ 黄金回放
→ Core v2 Shadow 对照
→ 干净机 smoke
→ G1 评审
```

当前实施先完成契约、证据和 Shadow 对照，再讨论真实输入接管；任何任务都应从 `docs/templates/tactical-package.md` 开始。
