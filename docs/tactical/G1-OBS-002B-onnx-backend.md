# 战术包：G1-OBS-002B ONNX backend 与外部资产烟测

## 1. 元数据

| 字段 | 内容 |
|---|---|
| package_id | `G1-OBS-002B` |
| parent | `G1-OBS-002A` |
| stage / type | `G1 / runtime integration + offline smoke` |
| owner / reviewer | `5.6 Luna max / 5.6Sol Ultra` |
| created_at | `2026-08-30` |
| status | `Completed (code + external-asset smoke)` |
| dependency | `G1-OBS-002A=Completed`；ADR-007、ADR-013、DEC-001 |
| input boundary | `input_owner=legacy`；`real_input_call_count=0`；`double_write_event_count=0` |
| source commit | `cde7cc969a4a4d2508199460420cc8fc1ed4427f` |
| evidence report | `evidence/g1-obs-002b/g1-obs-002b-20260830-cpu.json` |
| report digest | `17a2c15edb910096c93d7d4bdbeb9d7e114033ef530861eea9243ec5fcaf669d` |

## 2. 目标与边界

本包把 002A 的可注入 detector contract 接到一个 fail-closed ONNX backend，并以受控外部资产完成
Windows CPython 3.12 的真实 CPU observation smoke。backend 在建 session 前验证外部 root、规范化
relative id、模型 SHA-256、provider、tensor 名称、dtype 和具体 shape；推理前后再次验证 provider 与
tensor contract。没有 provider fallback、静默资产替换或模型字节入仓。

本包只关闭“代码 + 外部资产 + CPU smoke”范围，不关闭完整 `G1-OBS-002`、G1 Gate 或真实输入闭环。
本包不连接 `InputSink`、receiver、键盘、鼠标、游戏窗口或 VC-003，也不把本次 smoke 写成实机捕获。

## 3. 允许与禁止范围

### 允许范围

```text
src/maple_automation_core/vision/onnx_backend.py
src/maple_automation_core/vision/__init__.py
tests/test_onnx_backend.py
tests/test_observation_smoke.py
tools/run_observation_smoke.py
configs/g1-observation-requirements.lock
schemas/observation-runtime-report.schema.json
README.md
docs/CONTRIBUTING.md
docs/REQUIREMENTS-TRACEABILITY.md
docs/ROADMAP.md
docs/adr/ADR-013-observation-model-binding.md
docs/tactical/G1-OBS-002A-observation-foundation.md
docs/tactical/G1-OBS-002B-onnx-backend.md
```

### 禁止范围

```text
模型、classes、原始像素和其他外部资产不得复制到仓库；
G0 sealed packet、G1-FRM-001 Candidate、FrameSource/VC-003 实现不得改写；
InputSink、receiver、键鼠、窗口和 Legacy 输入所有权不得接入或切换；
不得以本包 smoke 宣称实机捕获、GPU/CPU parity、模型精度或完整 G1 PASS。
```

## 4. 冻结绑定

外部资产只通过受控 root + relative id + SHA-256 绑定。模型和原始 classes 文件保留在仓库外；公开
文档只记录可复核的逻辑标识、relative id 与 hash。

| 资产/契约 | 冻结值 |
|---|---|
| model logical id | `best_forest_v3-candidate` |
| model root / relative id | `MAPLE_MODEL_ROOT` / `weights/best_forest_v3.onnx` |
| model SHA-256 | `b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4` |
| classes | `[mob]` |
| classes SHA-256 | `07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884` |
| provider | requested and actual `CPUExecutionProvider` |
| ONNX Runtime | `1.23.2`；Windows CPython 3.12 wheel SHA-256 `25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435` |
| input contract | `images`；float32 NCHW `[1,3,640,640]` |
| output contract | `output0`；float32 `[1,5,8400]` |
| ModelBinding digest | `5d19b9d3c28eab8840ee182672d8f3c1e608af56781a3a95b4d74164daa73060` |

classes、shape、provider、配置或任一资产 hash 不匹配时，backend/report 进入 fault；不继续发布成功
Observation。`output0` 的 feature 轴保持 ADR-013 定义，当前包不执行 NMS 或 temporal confirmation。

## 5. 实施与烟测证据

`tools/run_observation_smoke.py` 先验证外部 model/classes/effective-config 的 hash，再使用同一
不可变 `FramePacket` 连续运行三次 `ObservationAdapter`。报告是严格 JSON，只含 hash、相对标识、运行
时元数据、digest 和隐私/输入审计，不包含 raw pixels、绝对路径或模型 custom metadata。

### 5.1 已完成结果

| 检查项 | 结果 |
|---|---|
| backend | ONNX backend code path 已完成，provider 与 input/output contract fail-closed |
| external assets | model/classes 以仓库外受控资产加载并按上述 SHA-256 校验 |
| CPU smoke | 真实 CPU `3` 次运行；三次 raw ONNX output digest 一致：`2c6a6f02f1c2c3b59179097a6590194c3f130ca309c979b7bde8ee07b9de830e`；三次 Observation `result_digest` 一致：`fb25433072da9ca88989427d977c873e7166d6e47bac6e737962d04225a0bf20` |
| portable report | `PASS`；source `cde7cc969a4a4d2508199460420cc8fc1ed4427f`；report digest `17a2c15edb910096c93d7d4bdbeb9d7e114033ef530861eea9243ec5fcaf669d` |
| artifact binding | tool `c0b3c22af6f509ffde9ebcc6e887610496028c36ab32cdf338eb4455a8fbb365`；schema `17ca3435f7a95274471a2386c6ceaab9b52739350d2caf987db60478c3fd525b`；lock `b512b0cac28dd0c73c2cf34733d8b76bd3dcb92432946603c93c0ffa19da5be9` |
| preprocess digest | 三次一致：`d85c25d9fedb84179fb0c5bbcf37b358963d1d009e5cdc657c0583f900c8b434` |
| tensor digest | 三次一致：`dcedfa517bd079f4933c6db6ec7aebee53178575affe4af1a33364a8f6d3b7f9` |
| input audit | `input_owner=legacy`；`real_input_call_count=0`；`double_write_event_count=0` |
| stage result | 代码/外部资产烟测完成；完整 `G1-OBS-002` 与整体 G1 仍 `In Progress` |

其中 `2c6a6f...830e` 是 raw ONNX output digest，`fb2543...bf20` 是完整 Observation result digest；
preprocess digest 为 `d85c25...b434`，tensor digest 为 `dcedfa...b7f9`。这些摘要都只表示本包的离线
observation smoke 确定性，不表示实机捕获、模型精度或现场 session。

### 5.2 可复现命令

在仓库根目录执行；将三个 root 变量指向受控、仓库外的资产目录。报告使用全新的 evidence id，不能
覆盖 `current` 或 `sealed` 状态：

```powershell
$env:MAPLE_MODEL_ROOT = 'TARGET_MODEL_ROOT'
$env:MAPLE_LEGACY_ROOT = 'TARGET_LEGACY_ROOT'
$env:MAPLE_CORE_ROOT = 'TARGET_CORE_ROOT'
python -m pip install --no-deps --require-hashes --requirement configs/g1-observation-requirements.lock
python tools/run_observation_smoke.py `
  --provider CPUExecutionProvider `
  --model-root $env:MAPLE_MODEL_ROOT `
  --classes-root $env:MAPLE_LEGACY_ROOT `
  --core-root $env:MAPLE_CORE_ROOT `
  --schema schemas/observation-runtime-report.schema.json `
  --lock configs/g1-observation-requirements.lock `
  --report evidence/g1-obs-002b/TARGET_EVIDENCE_ID.json
```

`TARGET_MODEL_ROOT`、`TARGET_LEGACY_ROOT`、`TARGET_CORE_ROOT` 和 `TARGET_EVIDENCE_ID` 是执行者在
本地命令层提供的值；它们不应写入公开报告。完整 ONNX smoke 需要能读取外部资产；缺失或 hash 不符
时，报告应为 fault/FAIL，而不是使用 fake backend 冒充真实资产结果。

## 6. Evidence → Finding → Path

### E-002B-001

- title: 受控外部 ONNX 资产的三次 CPU observation smoke
- observed_at: `2026-08-30`
- source_type: command
- source_ref: `evidence/g1-obs-002b/g1-obs-002b-20260830-cpu.json`；`tools/run_observation_smoke.py`；本节 §5.2
- content_hash: report `17a2c15edb910096c93d7d4bdbeb9d7e114033ef530861eea9243ec5fcaf669d`；raw ONNX output `2c6a6f02f1c2c3b59179097a6590194c3f130ca309c979b7bde8ee07b9de830e`；Observation `result_digest` `fb25433072da9ca88989427d977c873e7166d6e47bac6e737962d04225a0bf20`；preprocess `d85c25d9fedb84179fb0c5bbcf37b358963d1d009e5cdc657c0583f900c8b434`；tensor `dcedfa517bd079f4933c6db6ec7aebee53178575affe4af1a33364a8f6d3b7f9`
- repro_command: 见本文件 §5.2；需要仓库外受控 model/classes/config roots
- raw_excerpt: `CPUExecutionProvider × 3; raw ONNX output digest and Observation result_digest equal across runs; input_owner=legacy; real_input_call_count=0; double_write_event_count=0`
- linked_workitem: `G1-OBS-002B`

### F-002B-001

- title: ONNX backend 与外部资产绑定在 CPU smoke 范围内通过
- severity: `info`
- category: `other`
- status: `validated`
- evidence_ids: `[E-002B-001]`
- location: `src/maple_automation_core/vision/onnx_backend.py`；`tools/run_observation_smoke.py`
- impact: 固定的 model/classes/provider/tensor contract 可被复核，且三次 observation smoke 摘要一致。
- confidence: `high`
- repro_steps:
  1. 按 §5.2 提供仓库外 model/classes/config roots。
  2. 执行三次 smoke，并分别比较 raw ONNX output digest、Observation `result_digest` 与输入审计字段。
- remediation: `n/a for this integration smoke`

### P-002B-001

- title: accepted FramePacket 到 ONNX Observation 的调用路径
- path_type: `callflow`
- start: `accepted + fresh FramePacket`
- goal: `canonical Observation smoke result`
- steps:
  1. action: 验证 Pixel V1 bytes、frame lineage 与外部资产 hash；evidence: `E-002B-001`；finding: `F-002B-001`
  2. action: 执行固定 crop/resize/ROI/letterbox 并构造 float32 NCHW tensor；evidence: `E-002B-001`；finding: `F-002B-001`
  3. action: 通过 `CPUExecutionProvider` 推理，复验 provider/shape，并发布三次 raw ONNX output digest 与 Observation `result_digest`；evidence: `E-002B-001`；finding: `F-002B-001`
- residual_risks: 完整人工 truth、P/R、NMS/temporal、GPU/CPU parity、WorldState/Planner/Shadow 与现场闭环尚未完成。

## 7. 完成定义、回退与后续

本包的完成定义已满足：backend 代码与单元覆盖已落地；外部 model/classes/runtime 绑定已按 hash
校验；真实 CPU smoke 三次摘要一致；portable report 已绑定 source/tool/schema/lock/ModelBinding 并通过
严格 verifier；输入审计保持零；模型与 raw artifacts 未入仓。此状态只表示
代码/外部资产烟测完成。

回退只需关闭 Observation backend/feature flag，保留 002A 的 fake/dry-run 路径并恢复上一有效 Bundle；
不切换 Legacy 输入所有权，不触碰 receiver 或真实窗口。失败报告和 digest 继续保留，禁止覆盖已封存
证据。

后续 `G1-OBS-002` 仍需完成独立人工 truth/split、Model Card、NMS/temporal 规则、Replay/Shadow、
WorldState/Planner、故障闭环和对应 Gate；`G1-LOC-003`、`G1-WST-004` 等依赖包完成前，整体 G1 与
真实输入闭环保持 `In Progress`。
