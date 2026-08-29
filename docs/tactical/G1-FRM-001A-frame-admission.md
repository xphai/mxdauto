# 战术包：G1-FRM-001A Frame Admission 基础闭环

| 字段 | 值 |
|---|---|
| package_id | `G1-FRM-001A` |
| requirement | `REQ-CAP-001`、`REQ-SAFE-002`、`REQ-OBS-002` |
| gate | G1 |
| decision | Sol-U 已批准执行 |
| implementation | 5.6 Luna max |
| baseline | `main@4a43b24b27f496429a62060f415a4b4995e1f232` |
| ADR | ADR-002、ADR-003、ADR-004、ADR-010、ADR-011 |

## 1. 目标

1. 建立严格不可变的 RawFrame、FrameSource config/event/result 契约；
2. 建立线程安全、容量为 1 的 latest buffer；
3. 用注入式时钟执行 freshness admission；
4. 对 duplicate、out-of-order、timestamp、source/session/clock、画幅与 backend error
   形成显式、锁存、抑制计划的结果；
5. 用 synthetic/de-identified fixture 连续 3 次产生相同 digest；
6. 将当前 checkout regression 与 G0 sealed Candidate 复核分离。
7. 加固 admission evidence 所依赖的 Event Tape：同进程同路径多 writer 串行化，持久化
   record 出现额外顶层键时 fail closed。

## 2. 非目标

- VC-003/OpenCV 硬件 adapter 与 5 分钟硬件 smoke；
- capture backend 的 raw capacity=1/drain-to-latest producer；001A 的单槽位于 admission 后；
- 像素解码、BGR8 byte store 或录像拆帧；
- ONNX、检测、玩家定位、WorldState reducer、Planner；
- receiver、键鼠、窗口写入；
- G1 PASS 或 `G1-OBS-002` 解锁。

## 3. 允许路径

```text
src/maple_automation_core/capture/**
src/maple_automation_core/replay/frame_admission.py
src/maple_automation_core/replay/event_tape.py
tests/test_frame_source*.py
tests/test_frame_admission_replay.py
tests/test_event_tape_concurrency.py
tests/test_clean_smoke_modes.py
tests/test_bundle_evidence.py
tools/run_frame_admission_replay.py
tools/run_clean_smoke.py
fixtures/g1/frame_admission_v1.json
schemas/frame-admission-report.schema.json
.github/workflows/ci.yml
docs/adr/ADR-011-frame-admission-contract.md
docs/adr/ADR-010-ci-evidence-contract.md
docs/tactical/G1-FRM-001A-frame-admission.md
docs/ROADMAP.md
docs/REQUIREMENTS-TRACEABILITY.md
docs/CONTRIBUTING.md
README.md
```

G0 `bundles/`、G0 `evidence/`、Legacy、receiver、模型和输入路径保持不变。

## 4. 验收矩阵

| 场景 | 预期 |
|---|---|
| fresh frame / age=250ms | accepted；FramePacket 可追溯；plan_suppressed=false |
| age>250ms | stale；瞬时拒绝；下一合法帧可接受 |
| 单槽覆盖 | 只返回最新帧；superseded_count 精确 |
| frame ID gap | 接受并记录 gap |
| duplicate/out-of-order | 锁存；后续输出持续被抑制 |
| timestamp rollback/future | 锁存 |
| source/session/clock/size mismatch | 锁存 |
| backend error | 锁存且保留首次根因 |
| reset_session | 必须传入不同的新 session ID；清除水位与 latch；旧 session 拒绝 |
| 三次 fixture replay | status/event/output digest 完全一致 |
| 输入审计 | Core real input=0；double write=0 |
| Event Tape 同路径并发 | 同进程多实例 sequence/hash chain 完整；额外物理键拒绝 |

## 5. 门禁与证据

- Ruff lint/format、Mypy strict、全量 Pytest；repo coverage `≥90%`；
- Frame Admission report 通过独立 JSON Schema；
- CI 上传 `g1-frame-admission-<run_id>` 与 current-checkout smoke；
- required `quality` 成功并通过 protected PR 合并；
- G0 strict metadata 验证继续成功。

## 6. 回退

删除/关闭 G1 frame feature entry，停止 producer，清空进程内 latest slot，保留 fixture、报告和
失败记录；运行状态回到 G0 sealed baseline，Legacy 继续保持唯一真实输入 owner。

## 7. 完成判定

本包合并后标记 `G1-FRM-001A Completed / G1-FRM-001 In Progress`。硬件、pixel store、
corpus 与新 G1 Candidate packet 属于后继 `G1-FRM-001B`；在其闭环前 G1 Gate 保持进行中。
