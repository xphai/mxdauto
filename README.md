# Maple Automation Core

> **当前阶段（2026-08-30）**：**G-1 Strategic PASS / G0 PASS / G1 In Progress**。`G1-FRM-001A`、`G1-FRM-001B1`、`G1-FRM-001B2` 与完整 `G1-FRM-001` 均已完成；`G1-OBS-002A` 已完成合并封存，`G1-OBS-002B` 已完成代码与外部资产 CPU smoke（模型不入仓），完整 `G1-OBS-002` 仍为 `In Progress`。002A 通过 [PR #17](https://github.com/xphai/mxdauto/pull/17)（source commit `645d3a52d8e2e1364054ad4149f7815feeee733d`，PR run `33286071567` success，merge `1ccbceb79113a0322112b08d1a42a33dcacccad6`）进入 `main`；PR artifacts 的 SHA-256 前缀为 `g1-frame-source-b1=0cc18e...`、`frame-admission=6eee1f...`、`checkout=6a27b8...`、`ci-evidence=b7ca02...`、`build=ca6724...`、`quality=ee798f...`。main outer run [`33286521402`](https://github.com/xphai/mxdauto/actions/runs/33286521402) attempt 1 暴露两项 capture-stress 时序偶发失败，attempt 2 对同一 merge commit 完整重跑并 `success`；attempt 2 `ci-evidence` digest 为 `sha256:6d1147807a1600069b1a7731803f39b9777ef97772132ac172e09e7314469471`。G0 sealed packet 保持原有不可变事实；Core v2 real input calls=0，`input_owner=legacy`，整体 G1 与真实输入闭环仍为 `In Progress`。

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

## 2. G-1 / G0 基线与当前 G1 战术包

### G-1（主线确认）
- 确认 Core v2 唯一主线：`F:\mxd\product\maple-automation-core`
- 冻结 Legacy 新功能开发，仅允许迁移桥接与阻塞缺陷修复。
- 固定 Pilot 试验边界：
  - 1 张地图
  - 1 套角色档案
  - 1 套地图/路线/模型/阈值集合
- 建立路线图-测试-录像-会话追踪矩阵。

### G0（工程基线）
- 已通过并封存：`runtime-manifest.schema.json`、Manifest/Bundle 校验、领域契约、Event Tape、依赖锁和受保护 GitHub Actions `quality` 门禁。
- sealed G0 Candidate、commit、测试、Replay、Shadow、clean smoke 与 CI evidence 已形成不可变证据链。
- G0 已完成以下四项并留下报告：
  - 单元/契约测试自检
  - 固定黄金录像回放一致性检查
  - Core v2 Shadow 对比评估
  - 干净机 smoke 自检
- `schemas/runtime-manifest.example.json` 是 schema 验证 fixture，不是现场认证 Bundle。

### G1-FRM-001A（Completed）

- 固定采集契约：`1920×1080` source、`[277,167,1366,768]` content rect、`1296×700` working size、`250 ms` 最大帧龄。
- 显式处理 accepted/no-frame/stale/gap，以及重复、断序、时钟回退、画幅、source/session/clock/backend 故障。
- fatal fault 持续锁存，只有显式 session reset 清除；stale/no-frame 保持可恢复。
- G0 seal 校验与当前 checkout regression 分开运行，当前 wheel/report 绑定实际 `HEAD`，不重写 G0 packet。
- 合并收口：PR [#3](https://github.com/xphai/mxdauto/pull/3)，feature source `7cca4154a38e8bca29b917aa3c5abcc43a51391d`，merge `b30ddedb1f05945e68fb348b221cdfa123e83c59`；PR run `33225384485`、main run `33225488599`。
- 绑定结果：149 tests、91.38% coverage；Frame Admission `PASS`（3 runs / 15 scenarios / 32 events / zero input），main frame digest `1c4948afc636ffba45b1f4a769ec7ee3d6d5ea15f09b2b1f9596faa43f837a7d`；checkout smoke 20/20，5 artifact groups。
- `G1-FRM-001A` 完成时不产生 G1 PASS，也不提前启动 `G1-OBS-002`；随后 B2 会签收口已完成完整 `G1-FRM-001`，整体 G1 仍为 `In Progress`。

### G1-FRM-001（Completed）

- `G1-FRM-001B2` 已完成：VC-003 read-only adapter、content-addressed raw pixels、真实 corpus/truth、并发压力、300 秒硬件 smoke、source provenance 与 G1 Candidate packet 均已封存；Issue #13 六个精确角色均为 `approved`。
- 会签版收口：PR [#15](https://github.com/xphai/mxdauto/pull/15) merge `fe29a4ce5a8a98c49c85382f083d8429bfee2c38`，PR run `33283195258` success；main outer run `33283646596`（attempt 1）success，`ci-evidence` artifact digest `sha256:9e51d97d858e7432fe85be36fdaeefe7859dd2f4dc5f36ac6e81513d6885fb1c`；Candidate packet digest `4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73`。
- Core v2 真实输入调用继续为 0；Legacy 继续作为唯一真实输入下发者。

### G1-OBS-002A（Completed）

- 新增不可变 `ModelBinding`、`Detection`、`Observation`、有限故障码与互斥 `ObservationResult`；
- 固定 Pixel V1 校验、crop/resize/ROI/letterbox、RGB float32 NCHW 与模型坐标逆投影；
- 使用可注入 fake detector 验证 model/classes/config/preprocess/provider/shape 漂移的 fail-closed
  行为；真实 ONNX、NMS、人工 truth、性能与完整 OBS Gate 留给后继包。
- 合并封存：PR [#17](https://github.com/xphai/mxdauto/pull/17)，source commit `645d3a52d8e2e1364054ad4149f7815feeee733d`，PR run `33286071567` success，merge commit `1ccbceb79113a0322112b08d1a42a33dcacccad6`。
- PR artifacts（SHA-256 前缀）：`g1-frame-source-b1=0cc18e...`、`frame-admission=6eee1f...`、`checkout=6a27b8...`、`ci-evidence=b7ca02...`、`build=ca6724...`、`quality=ee798f...`。
- 合并后 main outer run [`33286521402`](https://github.com/xphai/mxdauto/actions/runs/33286521402) attempt 1 因两项 capture-stress 时序偶发失败而隔离；attempt 2 对同一 merge commit 完整重跑并 `success`。attempt 2 artifacts（SHA-256）：`g1-frame-source-b1=43d4f7a4735c6c151876a0d668aea4309679baa75ea2a8dab02e601194f0c922`、`frame-admission=82f8199ad80e0eab8c2f8ca04e225376f683152be983b23d31dac6d4c310c9ea`、`checkout=68db7194d87a072b9559364b204791dcbd9804be6073d45a83fee0204549f2d3`、`ci-evidence=6d1147807a1600069b1a7731803f39b9777ef97772132ac172e09e7314469471`、`build=d6b92239401aefe5625addcd028788831f36a546b225d8471d41f3dbe787e7b3`、`quality=e369a9e70bf3475170cc086fbd308e19c40cc41b4b89ce998cfaa4f4581fa421`。

### G1-OBS-002B（代码/外部资产烟测完成）

- 已接入 fail-closed `OnnxDetectorBackend` 与严格的 provider/tensor contract；实际 provider 为 `CPUExecutionProvider`，ONNX Runtime 为 `1.23.2`。
- 外部模型不入仓：model relative id 为 `weights/best_forest_v3.onnx`，SHA-256 为 `b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4`；classes SHA-256 为 `07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884`。
- Windows CPython 3.12 ORT wheel SHA-256 为 `25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435`；真实 CPU smoke 连续 3 次的 raw ONNX output digest 均为 `2c6a6f02f1c2c3b59179097a6590194c3f130ca309c979b7bde8ee07b9de830e`，Observation `result_digest` 均为 `fb25433072da9ca88989427d977c873e7166d6e47bac6e737962d04225a0bf20`。
- portable report 位于 `evidence/g1-obs-002b/g1-obs-002b-20260830-cpu.json`，绑定 source commit `672ec53327ea79f6ef3bd530f97a3006bd668aff`，report digest 为 `4379951ca0272bdf2e23ea37ec2a7602b92af8ac077450340924fa50582b64c6`。
- 输入审计保持 `input_owner=legacy`、`real_input_call_count=0`、`double_write_event_count=0`；本节完成不等于实机捕获或完整 `G1-OBS-002`。详见 [`docs/tactical/G1-OBS-002B-onnx-backend.md`](docs/tactical/G1-OBS-002B-onnx-backend.md)。

### 当前阶段的输入边界

| 范围 | 当前规则 |
|---|---|
| Core v2 | G1 offline/dry-run；可接纳帧、生成状态/计划、写入 Event Tape；真实输入调用为 0 |
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
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-011-frame-admission-contract.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-012-frame-pixels-and-capture-source.md`
  - `F:\mxd\product\maple-automation-core\docs\adr\ADR-013-observation-model-binding.md`
- 发布清单：
  - `F:\mxd\product\maple-automation-core\schemas\runtime-manifest.schema.json`
- 执行规程：
  - `F:\mxd\product\maple-automation-core\docs\ROADMAP.md`
  - `F:\mxd\product\maple-automation-core\docs\CONTRIBUTING.md`
  - `F:\mxd\product\maple-automation-core\docs\templates\tactical-package.md`
  - `F:\mxd\product\maple-automation-core\docs\tactical\G1-OBS-002B-onnx-backend.md`

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

### 当前本地门禁

在 `F:\mxd\product\maple-automation-core` 根目录执行：

```powershell
python -m pip install --requirement configs/requirements.lock
python tools/verify_dependency_lock.py --lock configs/requirements.lock --check-installed
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest schemas/runtime-manifest.example.json
python tools/verify_bundle.py --bundle-dir bundles/candidate-core-v2-20260829-shadow --metadata-only --strict-g0
python -m mypy
python -m pytest --cov=maple_automation_core --cov-report=term-missing --cov-report=xml --cov-fail-under=90
python tools/run_frame_admission_replay.py --runs 3 --fixture fixtures/g1/frame_admission_v1.json --schema schemas/frame-admission-report.schema.json --report evidence/ci-run/frame-admission-report.json
python -m pip install --no-deps --require-hashes --requirement configs/g1-observation-requirements.lock
python tools/run_observation_smoke.py --provider CPUExecutionProvider --model-root $env:MAPLE_MODEL_ROOT --classes-root $env:MAPLE_LEGACY_ROOT --core-root $env:MAPLE_CORE_ROOT --schema schemas/observation-runtime-report.schema.json --lock configs/g1-observation-requirements.lock --report evidence/g1-obs-002b/TARGET_EVIDENCE_ID.json
```

上述命令与 `F:\mxd\product\maple-automation-core\.github\workflows\ci.yml` 的主体门禁一致。Observation smoke 只读取仓库外的受控 model/classes/config roots，报告不得包含 raw pixels 或绝对路径；CI 还在干净 checkout 上执行 `run_clean_smoke.py --mode checkout-regression`，并分别上传 checkout regression 与 G1 frame-admission 证据；静态 `--strict-g0` 校验继续只读取 sealed G0 packet。

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
→ G1-FRM-001A frame admission
→ G1-FRM-001B hardware/corpus evidence
```

当前按 `G1-FRM-001（Completed） → G1-OBS-002A（Completed） → G1-OBS-002B（代码/外部资产烟测完成） → G1-LOC-003` 的依赖顺序实施；完整 `G1-OBS-002`、G1、G1 Gate 与真实输入闭环仍保持 `In Progress`，任何任务都应从 `docs/templates/tactical-package.md` 开始。
