# ADR-013：Observation、模型绑定与确定性预处理契约

**状态**：已接受（Accepted）

**日期**：2026-08-30

**战略负责人**：5.6Sol Ultra

**实施负责人**：5.6 Luna max

**对应工作包**：`G1-OBS-002A`、`G1-OBS-002B`

## 1. 背景

ADR-011/012 已冻结 `FramePacket` admission、Pixel V1、CAS 和 FrameSource provenance；下一边界
必须把已接受帧转换为可回放的检测 Observation。DEC-001 同时冻结了 Pilot 模型、类别、输入尺寸、
阈值和 ROI。若运行时自动采用模型内嵌 classes/shape、静默更换 provider，或让 backend 返回顺序进入
摘要，固定 Frame 仍可能产生不同结果，并掩盖 Bundle 漂移。

## 2. 决策

`G1-OBS-002A`/`G1-OBS-002B` 使用以下只读管线：

```text
accepted + fresh FramePacket
  → Pixel V1 bytes/hash verification
  → deterministic crop/resize/ROI/model-letterbox
  → DetectorBackend（002A 使用 fake；002B 使用经绑定校验的 ONNX backend）
  → Observation | ObservationFault(plan_suppressed=true)
  → 后续 G1-WST（本包不实现）
```

本包新增不可变、严格可序列化的领域对象：

- `DetectionBox`：半开区间、有限数值、正面积且严格位于声明的 `working` space；
- `Detection`：`class_id + class_name + confidence + box`；
- `ModelBinding`：release/model/classes/config/preprocess hash、输入输出 tensor contract、provider
  偏好、阈值和 ROI；
- `Observation`：frame/pixel/calibration/model/config/preprocess/provider lineage 与 canonical detections；
- `ObservationFault`：有限 reason code、同一预期 lineage 和诊断 details；
- `ObservationResult`：成功与故障严格互斥。成功时 `plan_suppressed=false`；故障时恒为 `true`。

所有 SHA-256 规范化为小写。Detection 按置信度、类别和坐标排序，backend tensor/anchor 遍历顺序
不得影响 `detection_digest` 或 Observation digest。

## 3. Pilot 模型绑定

逻辑值沿用 DEC-001 与 ADR-007 的外部资产绑定，不把模型复制进仓库：

| 字段 | 冻结值 |
|---|---|
| model | `best_forest_v3-candidate` / SHA-256 `b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4` |
| model relative id | `weights/best_forest_v3.onnx`（相对于 `MAPLE_MODEL_ROOT`；模型字节不入仓） |
| classes | `[mob]` / SHA-256 `07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884` |
| input | `images`, float32 NCHW `[1,3,640,640]` |
| expected output | `output0`, `[1,5,8400]`；feature 数必须为 `4 + len(classes)` |
| runtime/provider | ONNX Runtime `1.23.2`；请求与实际均为 `CPUExecutionProvider` |
| runtime wheel | Windows CPython 3.12 wheel SHA-256 `25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435` |
| detection / IoU | `0.25 / 0.45` |
| ROI | `[0.04,0.00,0.98,0.84]` |

classes、shape、hash、provider 或配置任一不一致均产生显式 fault；禁止自动同步、静默 fallback 或继续
输出成功 Observation。backend 响应必须显式回报实际 provider、input/output name 与 shape，全部精确
匹配后才写入 Observation；缺失声明同样 fail closed。构造参数与 Frame metadata 中同时出现的 asset
声明必须逐一一致，禁止用一份正确声明遮蔽另一份冲突声明。
`output0` 的 feature 轴固定为 `[cx, cy, width, height, class_probability...]`：前四项使用
model-input pixel space，不含独立 objectness；低于 detection threshold 的 anchor 直接忽略。本包不执行
NMS，任何高于阈值但坐标越界、非有限或非正面积的输出均使整帧 fail closed。

## 4. 确定性预处理

1. 重新验证 Pixel V1 bytes 与 `FramePacket.content_hash`；
2. 按 ADR-011 geometry 执行 `1920×1080 → [277,167,1366,768] → 1296×700`；
3. 在 working image 上按固定舍入规则裁剪 DEC-001 ROI；
4. 等比 resize 后使用固定值 `114` 居中 letterbox 至 `640×640`；
5. BGR→RGB，除以 255，输出只读 contiguous float32 NCHW tensor；
6. 按整数 resize 后的实际宽高分别记录 `scale_x/scale_y`，并以它们执行精确正逆坐标变换；
7. 记录 preprocess config SHA-256 与 tensor SHA-256。

ADR-011 的“Frame admission 不使用 letterbox”保持不变；这里的 letterbox 只属于模型输入预处理，并由
独立 preprocess hash 标识。

## 5. 故障与下游边界

至少覆盖 stale frame、frame lineage、Pixel missing/hash、calibration、model/classes/binding hash、输入/
输出 shape、provider、preprocess 和 inference fault。任一 fault 必须满足：

```text
successful_observation_count = 0
world_state_count = 0
action_spec_count = 0
real_input_call_count = 0
plan_suppressed = true
```

freshness 在预处理/推理前和成功发布前各复验一次；若推理期间越过 lease，整帧返回
`frame_stale`，不得发布过期 Observation。

pre-WorldState Event Tape 使用 `world_state_version=0`。本包不连接 `InputSink`、receiver、键鼠或游戏窗口；
Legacy 继续独占真实输入。

## 6. 非目标与晋级边界

002A 本身不引入 `onnxruntime`，不复制或发布模型；002B 已完成 fail-closed ONNX backend、仓库外
model/classes/runtime hash 绑定与 CPU observation smoke，但不声明 GPU/CPU parity、真实模型精度/性能、
人工 detection truth、NMS/temporal confirmation、完整 Replay/Shadow 或 `G1-OBS-002=Completed`。
provider fallback、人工会话隔离 truth、P/R 和 Model Card 仍由后续评估包完成。

### 6.1 G1-OBS-002B 烟测收口

002B 的代码与外部资产烟测已完成：真实 CPU 连续 3 次运行中，raw ONNX output digest 均为
`2c6a6f02f1c2c3b59179097a6590194c3f130ca309c979b7bde8ee07b9de830e`，Observation `result_digest`
均为 `fb25433072da9ca88989427d977c873e7166d6e47bac6e737962d04225a0bf20`。前一个 digest 是 raw
ONNX output digest，不是 Observation result digest，也不是实机帧/捕获 session digest。

portable report `evidence/g1-obs-002b/g1-obs-002b-20260830-cpu.json` 绑定 source
`cde7cc969a4a4d2508199460420cc8fc1ed4427f` 与 report digest
`17a2c15edb910096c93d7d4bdbeb9d7e114033ef530861eea9243ec5fcaf669d`；ModelBinding digest 为
`5d19b9d3c28eab8840ee182672d8f3c1e608af56781a3a95b4d74164daa73060`。

输入审计保持 `input_owner=legacy`、`real_input_call_count=0`、`double_write_event_count=0`。模型与
classes 保持在仓库外；本节不构成 VC-003/实机捕获完成，不授予 Core v2 输入权。完整 `G1-OBS-002`、
整体 G1 与 G1 Gate 仍为 `In Progress`。

本 ADR 已随 `G1-OBS-002A` protected PR 合并并接受（Accepted）：PR [#17](https://github.com/xphai/mxdauto/pull/17)、
source commit `645d3a52d8e2e1364054ad4149f7815feeee733d`、PR run
[`33286071567`](https://github.com/xphai/mxdauto/actions/runs/33286071567) `success`，merge commit
`1ccbceb79113a0322112b08d1a42a33dcacccad6`。

PR artifacts（SHA-256 前缀）如下：

| artifact | digest 前缀 |
|---|---|
| `g1-frame-source-b1` | `0cc18e...` |
| `frame-admission` | `6eee1f...` |
| `checkout` | `6a27b8...` |
| `ci-evidence` | `b7ca02...` |
| `build` | `ca6724...` |
| `quality` | `ee798f...` |

合并后 main outer run [`33286521402`](https://github.com/xphai/mxdauto/actions/runs/33286521402) attempt 1
暴露两项 capture-stress 时序偶发失败并已隔离；attempt 2 对同一 merge commit 完整重跑并 `success`，
`ci-evidence` artifact digest 为 `sha256:6d1147807a1600069b1a7731803f39b9777ef97772132ac172e09e7314469471`。
该 outer verification 已闭环；完整 `G1-OBS-002`、整体 G1 与 G1 Gate 继续保持
`In Progress`，`real_input_call_count=0`、`input_owner=legacy` 不变。

在此 Accepted 契约之上，`G1-OBS-002B` 的代码/外部资产 CPU smoke 已完成；该状态仅证明 ONNX
backend 的绑定与确定性 smoke，不改变完整 Observation evaluation、实机捕获或真实输入闭环状态。
