# ADR-007: Atomic Runtime Bundle 作为发布和回退单位

**状态**: 接受（Accepted）
**日期**: 2026-08-29

## 背景

运行时不是单一 Python commit。配置、角色档案、模型、类别表、输入尺寸、阈值、数据 split、地图注册、`PlatformGraph`、路线、MovementProfile、receiver 和证据报告必须相互匹配。单独替换其中一个文件会产生不可复现的行为，也会使 ADR-002 的回放谱系和 ADR-003 的状态决策边界失效。

仓库已经提供 `schemas/runtime-manifest.schema.json` 和 `tools/validate_runtime_manifest.py`。Manifest 示例只用于 schema 验证，不代表已认证的现场发布。

## 决策

### 1. Bundle 定义

一个 `Runtime Bundle` 是不可变、可追溯、可整体激活或整体回退的发布单元。每个 Bundle 必须包含一个通过 schema 校验的 `runtime-manifest.json`，并绑定以下字段：

| 范畴 | Manifest 字段 |
|---|---|
| 身份与源码 | `local_urn`, `runtime_manifest_version`, `release_id`, `source_commit`, `upstream_commit` |
| 配置与角色 | `config_schema`, `effective_config_sha256`, `profile_id`, `profile_sha256` |
| 模型输入 | `model_id`, `model_sha256`, `classes`, `input_size`, `thresholds` |
| 数据谱系 | `dataset_version`, `split_sha256` |
| 地图与控制数据 | `map_id`, `map_fingerprint`, `platform_graph_sha256`, `route_manifest_sha256`, `movement_profile_sha256` |
| 输入适配器 | `receiver_version`, `receiver_hash` |
| 质量证据 | `test_report_id`, `replay_report_id` |
| 现场与回退（可选字段） | `field_session_ids`, `rollback_release_id`, `created_at` |

字段的格式、必填性和正则约束以 `schemas/runtime-manifest.schema.json` 为唯一准则；本 ADR 不复制或放宽 schema。

### 2. 目录和激活模型

发布器必须先在临时 staging 目录生成完整 Bundle，再以同一文件系统上的原子目录替换完成发布。推荐布局如下：

```text
releases/
  <release_id>/
    runtime-manifest.json
    config/
    profile/
    model/
    maps/
    routes/
    receiver/
    evidence/
  current.json                 # 仅在 Bundle 认证后原子更新
```

规则如下：

1. staging 中所有文件先写完、关闭并计算 SHA-256；禁止在已激活目录中原地覆盖文件。
2. `runtime-manifest.json` 中的 hash 必须对应 staging 中实际字节；路径、文件名或内容变化都生成新的 `release_id`。
3. 发布前必须通过 schema 校验、契约测试、回放证据和当前阶段要求的 smoke；任何一个检查失败都保留为 Candidate/Quarantined，不更新 `current.json`。
4. 激活只切换一个 Bundle 指针或目录名；运行中的会话读取已经解析的 Bundle 快照，不在会话中热替换模型、地图、路线或 receiver。
5. 回退只选择 `rollback_release_id` 指向的完整 Bundle，并重新校验 manifest 与全部 asset hash；禁止通过手工替换单文件完成回退。
6. `runtime-manifest.example.json` 使用的占位 hash 和报告 ID只能验证工具链，不得作为 `Certified` 发布证据。

### 3. Bundle 生命周期

```text
Candidate
  → Offline-valid
  → Replay-valid
  → Shadow
  → Canary
  → Certified
  ↘ Quarantined
```

当前仓库处于 **G0 工程基线 / Core v2 Shadow**：Bundle 可以用于离线契约、schema 和回放准备，但 Core v2 不获得真实输入权。真实输入在 Shadow 期间始终由 Legacy 独占。`Canary`、`Certified` 和 Core v2 receiver 接管必须由独立 Stage Gate 明确批准；它们不是本 ADR 的自动结果。

## 证据绑定

发布记录至少要能从 `release_id` 追到：

```text
release_id
→ source_commit / upstream_commit
→ resolved config / profile / model / map / route / receiver hashes
→ test_report_id
→ replay_report_id
→ shadow/canary field_session_ids（如适用）
→ rollback_release_id
```

测试报告和回放报告必须记录 Bundle ID、源码 commit、运行环境和通过/失败的门禁。Shadow 报告另须记录 Core v2 计划、Legacy 实际输入和两者差异；报告不得把计划动作描述为已执行动作。

## 实施要求

1. 将 Bundle 解析为不可变 `ResolvedBundle`，在 runtime 启动时一次性校验并打印 `release_id`、manifest hash 和各 asset hash。
2. 发布工具必须拒绝缺失字段、schema 失败、hash 不匹配、证据 ID 缺失或 rollback 目标不可解析的 Bundle。
3. CI 的 manifest 校验至少执行：

   ```powershell
   python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json schemas/runtime-manifest.example.json
   ```

   现场或候选 Bundle 还必须把实际 `runtime-manifest.json` 作为位置参数传入同一工具。
4. 运行时日志和 Event Tape 必须记录载入的 `release_id`；会话中不得混用两个 Bundle。
5. 在 G0 完成前，先交付 schema/manifest/哈希/证据的离线闭环；真实接管、原子激活和回退演练列为后续 Stage Gate 的显式任务。

## 影响

- 发布、复现和回退拥有统一的物理边界。
- 资产漂移会在启动或发布前失败，而不是在现场表现为隐性行为变化。
- Manifest 校验、回放、Shadow 报告和输入所有权可以被同一个 `release_id` 串联。
- Bundle 原子性增加了发布准备工作，但避免了“源码已更新、模型或路线未更新”的不可审计状态。
