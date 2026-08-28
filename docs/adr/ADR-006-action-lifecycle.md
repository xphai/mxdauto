# ADR-006: Action 生命周期与终态证据

**状态**: 接受（Accepted）
**日期**: 2026-08-29

## 背景

动作如果只有“发出按键”而没有明确的开始、租约、截止时间和终态，过期命令可能继续作用于游戏窗口，按键也可能在异常路径中保持按下。仅凭 receiver 的 ACK 不足以判断动作是否改变了世界状态；ACK 只证明命令已经送达。ADR-002 要求所有事件可追溯到 `FramePacket`，ADR-003 要求决策只读取 `WorldState`，因此动作必须同时携带起点和后验状态证据。

当前领域契约已经提供以下不可变对象：

- `ActionSpec`：请求和决策起点；
- `ActionHandle`：经仲裁后获得的执行租约；
- `ActionResult`：唯一的终态结果；
- `ActionTermination`：`success`、`timeout`、`precondition_lost`、`observation_stale`、`input_failure`、`cancelled`。

## 决策

Core v2 采用以下单向生命周期：

```text
PROPOSED(ActionSpec)
    └─ admission ─> ISSUED(ActionHandle)
                       └─ preconditions + lease ─> STARTED
                                                      └─ verification ─> TERMINAL(ActionResult)
```

### 状态和转换约束

1. `ActionSpec` 必须是不可变请求，并包含 `session_id`、`action_id`、`kind`、`requested_at_ns`、`timeout_ns`、`origin_frame_id` 和 `origin_world_state_version`。
2. 只有 `ControlArbiter` 批准的请求才能生成 `ActionHandle`。`issued_at_ns` 不得早于请求时间，`expires_at_ns` 不得晚于 `ActionSpec.deadline_ns`；`generation` 用于区分重新仲裁后的租约。
3. 只有在前置条件仍由最新 `WorldState` 支持、会话仍匹配且租约未过期时，`ActionHandle` 才能进入 `STARTED`。`started_at_ns` 必须位于租约内。
4. 每个已签发的 handle 必须且只能产生一个 `ActionResult`。成功和失败都必须终态化，禁止以异常、进程退出或无响应代替结果。
5. `ActionResult` 必须引用原始 frame/world-state 版本和后验 frame/world-state 版本。`result_frame_id` 不得早于 `origin_frame_id`，`result_world_state_version` 必须严格大于 `origin_world_state_version`。
6. `ActionTermination.SUCCESS` 只在后验 `WorldState` 满足动作的成功谓词时使用；receiver ACK、发送队列入队或按键状态变化本身不是成功谓词。
7. 到达 deadline、租约过期、前置条件丢失、观测过期、输入错误或显式取消时，必须生成相应终态，并执行 `release_all`。任何终态路径都不得遗留按键或继续持有输入租约。
8. 已过期、属于旧 `session_id`、属于旧 `generation` 或引用过期 `WorldState` 的请求必须在输入边界前丢弃；丢弃也要写入 Event Tape，且不得下发 receiver。

### 输入所有权和 Shadow 限制

- `ActionController` 是 Core v2 唯一允许访问 `InputSink` 的模块；Planner、Behavior、Recovery、UI 和 Legacy adapter 都不得直接写入输入。
- 在当前 G0 / Core v2 **Shadow** 阶段，Core v2 只能生成 `ActionSpec`、模拟生命周期、记录 `ActionResult` 和对照报告；Core v2 不得调用真实 `InputSink`、键盘、receiver 或游戏窗口。
- Shadow 期间由 Legacy 保持唯一真实输入下发权。Shadow 结果用于比较和回放，不得改变 Legacy 的输入或控制状态。
- 只有通过后续 Stage Gate 并批准了接管范围后，才允许把同一 `ActionController` 接到认证 receiver；接管不是本 ADR 的默认行为，也不因测试通过自动发生。

## Event Tape 记录要求

至少记录下列事件类型，并为每条记录提供 ADR-002 所需的 `session_id`、`frame_id`、`world_state_version` 和单调时间：

```text
action.proposed
action.issued
action.started
input.ack
action.terminal
action.rejected
input.release_all
```

`action.terminal` 的 payload 必须包含 `handle_id`、`action_id`、`generation`、`termination`、起点版本、结果版本和证据引用。`input.ack` 只能作为送达证据保存，不得覆盖终态判定。

## 实施要求

1. 保持 `src/maple_automation_core/domain/actions.py` 的 `ActionSpec`、`ActionHandle` 和 `ActionResult` 序列化字段稳定；契约字段变化先更新本 ADR 和对应 contract test。
2. 在 `ActionController` 中实现显式状态守卫、单次终态化、deadline/lease 检查、session/generation 检查和统一 `release_all`。
3. 将动作成功谓词和后验 `WorldState` 版本校验放在 `ResultVerifier`；不得以 sleep、固定时长或 ACK 作为唯一完成条件。
4. Shadow runner 必须提供 dry-run sink，并在证据中证明真实输入调用次数为零；Legacy 输入调用点必须保持可审计。
5. contract test 覆盖正常完成、超时、取消、前置条件丢失、陈旧观测、输入失败、重复终态、旧 generation、旧 session 和按键释放。
6. Golden replay 必须验证相同 Event Tape 和 Bundle 得到相同生命周期事件序列；任何非确定性差异都阻止晋级。

## 影响

- 动作从“发送命令”变成可回放、可验证、可恢复的协议对象。
- 失败和取消路径拥有统一的输入释放责任，降低 stuck 风险。
- Shadow 阶段可以验证规划和终态语义，同时保持 Legacy 的真实控制权不变。
- `ActionController`、`ResultVerifier` 和 Shadow runner 是进入后续接管阶段的必要实现，不是 G0 的隐含完成项。
