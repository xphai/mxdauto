# 战术包：G1-OBS-002A Observation 确定性基础

## 1. 元数据

| 字段 | 内容 |
|---|---|
| package_id | `G1-OBS-002A` |
| stage / type | `G1 / contract + component` |
| branch | `feat/g1-obs-002a-foundation-20260830` |
| owner / reviewer | `5.6 Luna max / 5.6Sol Ultra` |
| status | `in_progress` |
| created_at | `2026-08-30` |
| dependency | 完整 `G1-FRM-001=Completed` docs seal；ADR-007、ADR-011、ADR-012、DEC-001 |

## 2. 目标和边界

交付从已接受 `FramePacket` 到类型化 Observation 的最小、可回退基础：不可变领域契约、确定性
preprocess、可注入 detector backend/fake，以及 fail-closed `ObservationResult`。全程
`input_owner=legacy`、Core v2 真实输入为 0。

明确不包含：

- `onnxruntime` 或新依赖锁；
- 模型/classes 字节复制入仓；
- 真实 ONNX 推理、GPU/CPU parity、精度/性能结论；
- 人工 detection truth、Model Card、NMS/temporal confirmation；
- WorldState、Planner、完整 Replay/Shadow 或完整 OBS Gate；
- receiver、键鼠、窗口写入或任何真实输入连接。

## 3. 允许范围

```text
src/maple_automation_core/domain/observation.py
src/maple_automation_core/domain/__init__.py
src/maple_automation_core/vision/__init__.py
src/maple_automation_core/vision/preprocess.py
src/maple_automation_core/vision/observation_adapter.py
tests/test_contract_observation.py
tests/test_observation_preprocess.py
tests/test_observation_adapter.py
README.md
docs/CONTRIBUTING.md
docs/REQUIREMENTS-TRACEABILITY.md
docs/ROADMAP.md
docs/adr/ADR-013-observation-model-binding.md
docs/tactical/G1-OBS-002A-observation-foundation.md
```

禁止修改 G0/G1-FRM packet、Candidate Bundle、模型资产、capture/frame admission 实现、输入模块和
既有 Replay/Shadow 语义。

## 4. 契约与实施

| 项目 | 本包要求 |
|---|---|
| Frame | 只接收 accepted 的 `FramePacket`；推理前与发布前均复验 freshness，并重算 Pixel V1 hash |
| Geometry | ADR-011 crop/resize；working space 固定 `1296×700` |
| Preprocess | DEC-001 ROI、固定 letterbox/BGR→RGB/NCHW；整数 resize 的 `scale_x/scale_y` 与 config/tensor digest 可复算 |
| Model | `ModelBinding` 精确绑定 model/classes/config/preprocess；backend 必须回报并匹配 provider/name/shape |
| Output | Detection canonical 排序；Observation 携带完整 lineage 和确定性摘要 |
| Fault | hash/shape/provider/preprocess/inference 等显式 reason；无成功 Observation，抑制后续计划 |
| Input | 不导入或调用真实 input adapter；所有真实输入计数为 0 |

实施顺序：

1. 落地 `DetectionBox/Detection/ModelBinding/Observation/ObservationFault/ObservationResult` 与
   contract tests；
2. 实现 Pixel V1 验证、确定性 crop/resize/ROI/letterbox、正逆 box 投影；
3. 实现 backend Protocol、fake backend 与 adapter fault matrix；禁止 silent fallback；
4. 对固定 pixels/fake output 连续三次比较 tensor、Detection、Observation/Result digest；
5. 通过 protected PR required `quality`，并由 current-main post-merge CI 复验。

## 5. 验收标准

- [ ] 领域对象不可变，严格 JSON round-trip；非法 hash、shape、class、provider、坐标和互斥状态均拒绝；
- [ ] 相同 detection 集合不受 backend 返回顺序影响，canonical digest 相同；
- [ ] 固定 pixels 连续三次产生相同 preprocess tensor/transform digest；
- [ ] crop、ROI、letterbox及正逆 box 投影有边界 golden tests；
- [ ] stale、Pixel/calibration/model/classes/shape/provider/preprocess/inference 故障全部
  `plan_suppressed=true`，且 backend 前置失败调用数为 0；
- [ ] fault 路径的 WorldState、ActionSpec、receiver/window/key/mouse 调用均为 0；
- [ ] 没有新增依赖、模型或私有素材；G0/G1-FRM sealed bytes 未变化；
- [ ] touched-file Ruff、Mypy 和最小相关 pytest 通过；
- [ ] protected PR required `quality` 与 current-main CI success，实际 commit/run/hash 已回填。

本地快速验证只运行相关文件：

```powershell
python -m ruff check src/maple_automation_core/domain/observation.py src/maple_automation_core/vision tests/test_contract_observation.py tests/test_observation_preprocess.py tests/test_observation_adapter.py
python -m ruff format --check src/maple_automation_core/domain/observation.py src/maple_automation_core/vision tests/test_contract_observation.py tests/test_observation_preprocess.py tests/test_observation_adapter.py
python -m mypy src/maple_automation_core/domain/observation.py src/maple_automation_core/vision
python -m pytest -q tests/test_contract_observation.py tests/test_observation_preprocess.py tests/test_observation_adapter.py
```

## 6. 回退与完成定义

回退只需移除 Observation adapter/feature flag 并恢复上一个 Frame-valid Bundle；FrameSource 与 Legacy
输入 owner 不变，不触发 `release_all` 或 receiver 切换。失败报告和 digest 保留，不覆盖成功历史。

`G1-OBS-002A=Completed` 仅在以下条件同时满足后成立：

1. ADR-013 与实现由 Sol-U 复核；
2. 本包验收项全通过；
3. protected PR 合并且 current-main required CI 成功；
4. source commit、PR、run ID/attempt、测试结果与 artifact hash 回填本文件。

完成本包只允许启动真实 backend/evaluation 后继包；`G1-OBS-002`、完整 G1 Gate 与 Core v2 输入权限
状态均不改变。
