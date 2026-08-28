# ADR-004: 输入所有权、阶段边界与有界独占租约

**状态**：接受（Accepted）  
**日期**：2026-08-29  
**决策负责人**：5.6Sol Ultra（战略 / Gate）  
**实施负责人**：5.6 Luna max（战术包）

## 背景

Legacy 当前拥有真实键盘、鼠标和双机 receiver 的写入能力；Core v2 当前只交付领域契约、Event Tape、Manifest schema 与静态 CI 定义。若两个运行时同时持有输入权，任何计划差异、重试、断连或状态漂移都会形成不可审计的双写风险。

ADR-001 规定 Core v2 是目标主线；ADR-006 规定动作生命周期和当前 Shadow 限制。本 ADR 进一步确定从 G0 到 G6 的输入所有权转移点，使“目标架构”与“当前运行边界”保持一致。

## 术语

| 术语 | 定义 |
|---|---|
| 输入写入者（writer） | 可以向本机键盘/鼠标、`InputSink`、receiver socket 或游戏窗口产生真实输入副作用的进程/模块 |
| 输入所有者（owner） | 当前唯一获准产生真实输入的运行时身份：`legacy` 或 `core_v2` |
| 独占租约（exclusive lease） | 把输入权限定到 owner、session、Bundle、Pilot、开始/截止时间和 generation 的可撤销授权 |
| dry-run sink | 只记录意图与模拟结果、真实副作用计数恒为 0 的适配器 |
| 双写（double write） | 同一现场窗口内出现两个有效 owner，或 Legacy/Core v2 都有真实命令被 receiver 接受 |

## 决策

### 1. G0～G2：Legacy 独占

1. Legacy 保持唯一真实输入所有者。
2. Core v2 仅运行 Replay、Shadow、simulator 或 dry-run；真实 `InputSink`、键盘、鼠标、receiver 和游戏窗口调用数必须为 0。
3. Core v2 允许读取去标识化的 Frame/Event 输入和 Legacy 的只读对照事件；Legacy 私有状态不作为 Core v2 的 `WorldState` 真值。
4. G0 的 CI、Golden Replay、Shadow 或 clean smoke 结果只证明对应证据范围，不授予真实输入权。
5. G2 的 receiver 测试使用 `DryRun`、simulator 或硬件在环诊断模式；真实游戏输入保持关闭。

### 2. G3：有界独占租约

G3 是 Core v2 首次获得真实输入权的阶段。每个 Canary 会话都必须绑定一份 Gate 批准的独占租约：

```text
lease_id
owner = core_v2
session_id
release_id / runtime_manifest_sha256
map_id / profile_id
generation
not_before / expires_at
heartbeat_lease_ms
operator / approvers
rollback_release_id / rollback_owner
```

租约只在以下顺序全部完成后生效：

1. G2 Gate 为 `PASS`，G3 Canary Charter 已批准；
2. 现场冻结唯一 Bundle、Pilot、窗口、值守人和停止条件；
3. Legacy 停止产生新动作并执行 `release_all`；
4. receiver 观察到 Legacy authority 已撤销、旧 generation 失效；
5. Core v2 载入完全匹配的 Bundle，通过 session/generation/TTL 检查；
6. receiver 只接受该 `lease_id` 的命令；
7. Event Tape 写入 `input.owner.granted` 后，会话才进入 Running。

租约到期、心跳超过 1500 ms、session/Bundle/generation 失配、停止条件触发或人工撤销时，receiver 与 Core v2 同时进入失效保护：停止接收新动作、终态化在途动作、执行 `release_all`、写入 `input.owner.revoked`，运行时进入 Paused/Faulted。

租约续期属于新的显式 Gate 操作。单次 Canary 的时长、次数与晋级顺序以 `docs/ROADMAP.md` 的 G3 Charter 为准。

### 3. G4～G5：认证范围内由 Core v2 独占

1. Core v2 只在已 Certified 的 `map_id/profile_id/capability/release_id` 范围内成为常态 owner。
2. 未认证功能保持 flag 关闭；Legacy 只作为经批准的紧急回退 owner。
3. 从 Core v2 回退到 Legacy 同样执行完整交接：Core v2 停止新动作、`release_all`、撤销租约、receiver 确认，再显式授予 Legacy 新 generation。
4. 任何 Bundle 或范围变化都需要新的认证证据，旧租约不继承。

### 4. G6：Core v2 为支持矩阵唯一常态 owner

Legacy 转为只读归档。退役观察期内若启用 Legacy 应急路径，必须生成事故记录、输入所有权 trace 和恢复计划；观察期结束后，回退优先且默认只使用上一 Certified Bundle。

## 双写不变量

**所有阶段的双写事件数必须为 0。**

### receiver 强制规则

- receiver 同一时刻只保存一个 `owner + lease_id + generation`；
- 旧 owner、旧 generation、过期 TTL、未知 session 或 Bundle 失配的命令被拒绝并写入审计事件；
- 第二个 writer 建连或发送有效命令时，receiver 进入 Faulted，执行 `release_all`；
- owner 切换依赖显式 revoke/grant，不依赖进程是否“看起来已经停止”；
- ACK 只证明命令送达，不代表动作成功或 owner 已安全交接。

### 最小遥测

```text
input.owner.grant_requested
input.owner.granted
input.command.accepted
input.command.rejected
input.owner.conflict
input.release_all
input.owner.revoked
```

每条事件至少携带：`session_id`、`frame_id`（适用时）、`release_id`、`owner`、`lease_id`、`generation`、receiver sequence、单调时间和结果原因。

### Gate 证明

1. 静态调用审计：真实输入 adapter 只能由 `ActionController` 到达；
2. 动态审计：G0～G2 Core v2 真实输入调用数为 0；
3. G3+ 所有 `input.command.accepted` 在同一时间窗内只对应一个 owner/lease/generation；
4. 交接前后均存在 `release_all` 和 revoke/grant 事件；
5. conflict 故障注入产生拒绝、释放与 Faulted，不产生第二条有效动作。

## 角色与审批

| 事项 | 负责人 |
|---|---|
| 阶段所有权策略、Gate 结论 | 5.6Sol Ultra（A/R） |
| lease/receiver/ActionController 战术实现 | 5.6 Luna max（R） |
| Canary 窗口与人工接管 | 现场负责人（A/R） |
| 双写、终态和证据审计 | QA/证据负责人（R），Sol-U（A） |
| Bundle 与回退 owner | 发布负责人（R） |

Luna-M 提供实现与原始证据，不自行授予新的阶段输入权。Sol-U 依据 Gate packet 给出 `PASS / HOLD / QUARANTINE / ROLLBACK`。

## 影响

- ADR-001 中的 Core v2 单一输入入口是目标状态；G0～G2 的当前运营状态仍由 Legacy 独占。
- Shadow 计划与 Legacy 实际动作可以并行记录，但真实命令只来自 Legacy。
- G3 的输入权是会话级、有时限、可撤销的独占授权，不是永久接管。
- 双写从约定提升为 receiver、Event Tape 和 Gate 共同验证的不变量。

## 回退

```text
停止新动作
→ 终态化在途 ActionHandle
→ release_all
→ revoke 当前 lease/generation
→ 验证 receiver 无有效 owner
→ 授予回退 owner 新 generation
→ 归档 Event Tape / receiver log / Gate 事件
```

任一步缺少确认时，系统保持 Paused/Faulted 和无 owner 状态，由现场人员接管。

