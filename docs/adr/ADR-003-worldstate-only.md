# ADR-003: WorldState 是唯一决策输入

**状态**: 接受（Accepted）

**日期**: 2026-08-29

## 背景
不同模块若直接读取各自私有状态，系统行为不可预测，恢复与回放也会失真。必须将决策入口统一到不可变状态。

## 决策
`WorldState` 为 Core v2 所有 Planner/Control/Recovery 决策的唯一输入。任何模块不得直接读取底层 frame 原始私有状态或 Legacy 专有字段。

## 不变量
1. 输入链统一为：`FramePacket -> Observation -> WorldState -> Decision -> Action`。
2. WorldState 实例创建后不再被下游模块原地修改。
3. Planner 与 Recovery 必须仅使用 `snapshot.version/frame_id` 标识的 WorldState。
4. 任何动作成功/失败判定均使用“后验世界状态变化”确认。

## 实施要求
- 引入“决策前后的 WorldState 版本校验”；
- 记录 `world_state_version` 与 `result_version`；
- 当世界状态缺失/过期时拒绝动作并进入恢复路径。

## 影响
- 大幅降低模块间隐式耦合；
- 符合回放、故障注入、证据追溯的一致性要求；
- 为后续 PlatformGraph 与 RecoverySupervisor 提供稳定接口。
