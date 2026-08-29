# ADR-011：Frame Admission、latest buffer 与故障锁存契约

**状态**：接受（Accepted）

**日期**：2026-08-29

**战略负责人**：5.6Sol Ultra

**实施负责人**：5.6 Luna max

**对应工作包**：`G1-FRM-001`

## 1. 背景

ADR-002 已把 `FramePacket` 定义为最小时序单元，但尚未定义采集后端到
`FramePacket` 之间的 admission 行为。逐帧队列会在处理速度低于采集速度时累积陈旧帧；
自动重编号、隐式纠正画幅或在异常后继续投递，则会破坏 Replay 与故障复盘。

G0 Golden Replay 直接读取已经构造好的 `FramePacket`/`WorldState`，只证明证据管道，
不覆盖实际 FrameSource 的 freshness、sequence、geometry 和 backpressure 语义。

## 2. 决策

Core v2 新增独立的 Frame Admission 边界：

```text
read-only capture backend
  → RawFrame
  → FrameSourceAdapter
  → accepted FramePacket | explicit suppressed event
  → capacity=1 latest admitted FramePacket buffer
  → Observation（后续 G1-OBS-002）
```

本边界只读取采集数据，不连接 `InputSink`、键盘、receiver 或游戏窗口。G0 sealed
Bundle 保持字节不变；G1 current-checkout regression 与 G0 seal verification 使用不同的
provenance。

## 3. 冻结配置

| 字段 | G1 Pilot 值 |
|---|---|
| logical source | `capture-card-primary` |
| source size | `1920×1080` |
| content rect | `[277,167,1366,768]` |
| working size declaration | `1296×700` |
| geometry policy | 固定 crop + resize；本阶段不引入 letterbox |
| latest capacity | `1` |
| max age | `250 ms`（边界值接受） |
| clock | 注入式 monotonic clock；Replay 使用 virtual monotonic clock |
| input owner | `legacy` |

`SourceGeometry` 与 transform version 共同形成 canonical calibration SHA-256。由 adapter
配置绑定而未在 RawFrame 重复声明的 source/session/clock/transform 使用该固定配置；一旦来源
显式声明，则必须与配置精确相等。geometry/size 始终校验，不做自动画幅修正。

## 4. latest 与 sequence

1. admission 后的 FramePacket slot 容量恒为 1；新接受帧原子覆盖尚未消费的帧，并累计 `superseded_count`；
2. consumer 每次只读取当时最新候选，不回放积压帧；
3. `frame_id == last_accepted` 为 duplicate；`frame_id < last_accepted` 为 out-of-order；
4. `frame_id > last_accepted + 1` 允许接受，同时记录 sequence gap；
5. content hash 相同但 frame ID 合法递增代表静止画面，不作为 duplicate；
6. Core v2 保留原始 frame ID，不重新编号掩盖后端问题。

## 5. freshness 与故障锁存

Admission 使用调用方注入的 `now_ns`，且要求与 RawFrame 属于同一 clock domain。
`age_ns <= max_age_ns` 接受；负 age、timestamp rollback、clock mismatch 均 fail closed。

瞬时拒绝：

- 当前没有候选帧；
- stale frame。

锁存拒绝：

- duplicate 或 out-of-order frame ID；
- capture timestamp rollback；
- source/session/clock mismatch；
- source size / geometry mismatch；
- backend/source error。

首次锁存事件保持为根因；后续 poll 继续抑制输出。所有 no-frame、stale、poll、ingest
和 latest read 都推进统一 monotonic observation watermark；任何回退均锁存。只有携带不同
`new_session_id` 的 `reset_session` 才清除水位与锁存状态。拒绝或锁存结果的 `FramePacket` 始终为空，
`plan_suppressed=true`。

## 6. 事件与下游边界

Frame Admission 输出稳定的 status/reason code、候选 frame ID、上次接受 frame ID、
calibration SHA-256、superseded/gap 计数和 `plan_suppressed`。pre-WorldState 事件写入 Event
Tape 时使用 `world_state_version=0`；out-of-order 候选 ID 放入 payload，EventRecord 自身沿用
最后接受的单调 frame ID。

G1-OBS-002 只能接收 `accepted` 的 `FramePacket`。所有 rejection 场景必须保持：

```text
observation_dispatch_count = 0
world_state_count = 0
action_spec_count = 0
real_input_call_count = 0
double_write_event_count = 0
```

## 7. CI 与证据语义

- `verify_bundle --strict-g0` 继续只读复核已封存的 G0 source/packet/run；
- `checkout-regression` clean smoke 构建、安装并测试当前 HEAD，报告的 `source_commit` 为
  `tested_commit=HEAD`，G0 Manifest 只作为只读 baseline；
- Frame Admission fixture 连续运行 3 次，事件/status/report digest 必须一致；
- 当前实现与报告不生成新的 G1 Candidate/Certified 结论。

## 8. 影响与后继工作

本 ADR 允许实施 `G1-FRM-001A` 的纯 Python admission contract、latest buffer、fault matrix
和确定性 fixture。完整 `G1-FRM-001` 仍需 content-addressed pixel store、VC-003 read-only
adapter（其 producer 必须提供 raw capacity=1/drain-to-latest 语义）、派生 corpus、5 分钟硬件
smoke、Event Tape provenance audit 与新 G1 Candidate
packet。上述证据闭环后才评审是否解锁 `G1-OBS-002`。
