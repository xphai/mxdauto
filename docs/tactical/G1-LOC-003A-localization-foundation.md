# 战术包：G1-LOC-003A 定位代码基础

## 1. 元数据

| 字段 | 内容 |
|---|---|
| package_id | `G1-LOC-003A` |
| title | 版本化 affine、平台图、独立匿名 player anchor 与纯 fail-closed resolver 代码基础 |
| parent | `G1-OBS-002B`（code/external smoke Completed） |
| stage / type | `G1 / localization contract + deterministic unit tests` |
| owner / reviewer | `5.6 Luna max / 5.6Sol Ultra` |
| branch | `feat/g1-loc-003-foundation-20260830` |
| implementation source | `d0fd36ef77e859b34af7628b778d32d2df628ba4` |
| created_at | `2026-08-30` |
| status | `Completed (code foundation)`；完整 `G1-LOC-003` 为 `In Progress` |
| evidence level | `L1` local code + deterministic tests；本包未创建独立 LOC 远端 Gate 证据 |
| dependency | `G1-OBS-002B`；ADR-011、ADR-012、DEC-001 |
| input boundary | `input_owner=legacy`；Core v2 `real_input_call_count=0`；`double_write_event_count=0` |

### 上游 OBS002B 合并绑定

- PR [#20](https://github.com/xphai/mxdauto/pull/20) required run [`33289661770`](https://github.com/xphai/mxdauto/actions/runs/33289661770) 为 `success`。
- merge commit [`9aff755f18d3bd48c77084cfaf10ea4df6344f69`](https://github.com/xphai/mxdauto/commit/9aff755f18d3bd48c77084cfaf10ea4df6344f69)。
- main outer run [`33290009677`](https://github.com/xphai/mxdauto/actions/runs/33290009677) 为 `success`。
- 以上只关闭 `G1-OBS-002B` 的 code/external smoke 合并绑定，不转化为 LOC、实机或完整 G1 证据。

## 2. 目标与边界

本包只收口定位层的可复用代码基础，不实现完整定位 Gate。输入由调用方提供已构造的
`ObservationResult` 与独立匿名 `PlayerCandidate`；resolver 不读取设备、原始像素、时钟、输入
通道或可变全局状态。

沿用上游 B2 accepted `FramePacket`/CAS 的 pixel digest lineage：accepted CAS pixel digest → `Observation.pixel_digest` → `PlayerCandidate.pixel_digest` 必须精确绑定；
003A resolver 不读取 CAS，但任何跨 CAS 或异像素候选均进入 fail-closed。

### 已落地的代码基础

1. **版本化 affine transform**：`LocalizationTransform` 是不可变的二维 2×3 affine 矩阵，绑定
   `map_id`、`map_fingerprint_sha256`、`profile_id`、`transform_version`、`calibration_sha256` 和
   `working_size`；提供
   working pixel → world 以及精确 inverse，并拒绝退化矩阵、非有限数和上下文漂移。
2. **平台图**：`PlatformGraph` 绑定 map fingerprint、graph version 和不可变平台线段；平台匹配按
   vertical error、horizontal error、platform ID 确定性排序，且同时要求
   `vertical_error <= max_vertical_distance` 与 `horizontal_error <= max_horizontal_distance`。任一
   距离阈值超限时返回 `unknown`；近似并列或共享端点返回 `ambiguous`，不做任意归属决定。
3. **独立匿名 player anchor**：`PlayerCandidate` 带 session/source/frame/time/generation、匿名
   `subject_id`、confidence、visibility、来源、evidence hash 与 `pixel_digest`。resolver 要求
   candidate 的 `pixel_digest` 与 `Observation.pixel_digest` 精确一致，避免同元数据异像素候选混入。
   `PlayerAnchorSource.MINIMAP_YELLOW_MARKER` 是来源标签，当前不执行 marker 像素提取。
4. **纯 fail-closed resolver**：`resolve_player_location` 交叉绑定 observation lineage 与 candidate，
   精确比对 transform 与 platform graph 的 map fingerprint，校验 freshness、session/frame/time、generation、
   identity、pixel digest、calibration/working size、地图与平台归属；固定 session 内 source/transform/graph
   version 必须保持一致，`as_of` 与 observation/previous state 必须保持单调时间谱系，失败请求也推进
   `last_checked_as_of_ns` clock fence；fault、unknown、ambiguous 或 degraded 时返回显式结果并抑制 plan。

### 明确不在本包内

- 从 minimap 图像提取 yellow marker；B2 accepted `FramePacket`/CAS 的 extraction 属于后续 `G1-LOC-003B`。
- 人工 truth、评估指标、独立 split、模型质量或任何 `100 圈` 结果。
- VC-003 设备打开、实机 LOC session、hardware LOC evidence 或输入通道连接。
- 完整 `G1-LOC-003`、完整 `G1-OBS-002`、完整 G1 Shadow、G1 Gate 或任何真实输入接管。

## 3. 允许与禁止范围

### 允许范围

```text
src/maple_automation_core/localization/
tests/test_localization_transform.py
tests/test_localization_platform.py
tests/test_player_localizer.py
README.md
docs/ROADMAP.md
docs/REQUIREMENTS-TRACEABILITY.md
docs/tactical/G1-LOC-003A-localization-foundation.md
```

### 明确禁止修改的路径

```text
G0 sealed packet、G1-FRM-001 Candidate、FrameSource/VC-003 实现与 B2 accepted FramePacket/CAS；
真实模型、classes、raw pixels 和外部资产；
InputSink、receiver、键盘、鼠标、游戏窗口和 Legacy 输入所有权；
本包未列出的代码、schema、Bundle、证据报告或其他测试。
```

## 4. 契约和接口

| 项目 | 当前版本/字段 | 本包变化 | 兼容策略 |
|---|---|---|---|
| Frame/Observation | B2 accepted `FramePacket` → `ObservationResult` | none；resolver 只消费现有 ObservationResult | candidate 必须与 session/source/frame/time/pixel/calibration/working size 对齐 |
| Localization transform | `LocalizationTransform` / versioned 2×3 affine + map fingerprint | add code foundation | 通过 `map_fingerprint_sha256`、`transform_version`、calibration hash、working size 与 digest 绑定 |
| Platform graph | `PlatformGraph` / `PlatformSegment` / `PlatformMatch` | add code foundation | map fingerprint、graph version、vertical/horizontal 双阈值、确定性 candidate order；任一阈值超限 unknown，ambiguous/unknown fail-closed |
| Player anchor | `PlayerCandidate` / `PlayerAnchorSource` / `pixel_digest` | add independent anonymous input contract | candidate pixel digest 必须等于 Observation pixel digest；固定 session 内 transform/graph version 与 as-of 谱系失配 fail-closed；marker extractor 延后到 003B；来源标签不等价于提取结果 |
| WorldState | existing `PlayerState` projection | no reducer change | located 结果可投影；unknown/degraded/fault 抑制计划 |
| Action/Input | existing Action/Input contracts | none | 不连接 InputSink/receiver；`input_owner=legacy` 保持不变 |

本包不修改已有 Frame/Observation/WorldState/Action schema，不改变 Runtime Bundle，不新增真实输入调用。

## 5. 实施步骤

1. 固定并导出 `LocalizationTransform`、`PlatformGraph`、`PlayerCandidate`、`PlayerLocation`、
   `LocalizationFault`、`LocalizationResult`、`LocationState` 和 `resolve_player_location`。
2. 对每个对象执行 canonical serialization/digest 与 immutable contract 校验；对 affine 边界、矩阵
   退化、平台 vertical/horizontal 双阈值、远离平台 unknown、平台近似并列、candidate 缺失/低置信度/丢失、
   pixel/map fingerprint lineage、固定 session transform/graph version 与 as-of 时间谱系、identity/generation/freshness
   漂移建立 deterministic tests。
3. 保持 resolver 为无副作用纯函数，所有不可确定或上下文失配分支进入显式 fault/unknown/degraded
   结果，并保持 plan suppressed。

## 6. 测试计划

| 层级 | 命令/fixture | 预期结果 | 实际证据 |
|---|---|---|---|
| Transform contract | `python -m pytest tests/test_localization_transform.py` | affine/inverse、context/hash、边界、退化矩阵与 immutable contract 可重复 | `tests/test_localization_transform.py` |
| Platform contract | `python -m pytest tests/test_localization_platform.py` | 平台排序、vertical/horizontal 双阈值、远离平台 `unknown`、shared endpoint/near-tie ambiguity、serialization/digest 可重复 | `tests/test_localization_platform.py` |
| Resolver contract | `python -m pytest tests/test_player_localizer.py` | 独立匿名 anchor、pixel/map fingerprint lineage、固定 session transform/graph version 与 as-of 谱系、freshness/generation/identity、unknown/degraded/fault、plan suppression 可重复 | `tests/test_player_localizer.py` |
| Combined deterministic set | `python -m pytest tests/test_localization_transform.py tests/test_localization_platform.py tests/test_player_localizer.py` | 46 个 localization tests 通过，重复调用摘要保持一致 | 本地 L1 code foundation |
| Related contract set | 上述三组测试加 `tests/test_contract_coordinates.py tests/test_contract_player_world.py tests/test_contract_observation.py` | 80 个相关契约 tests 通过 | 本地 L1 code foundation |
| Hardware / marker smoke | `G1-LOC-003B` 后续命令 | 先完成 B2 accepted FramePacket/CAS 的离线 marker 3-run，再复用 VC-003 只读入口 | 未执行；不属于 003A |

本包没有真实 marker extraction、人工 truth、100 圈、实机 LOC 或输入通道测试。

## 7. Telemetry 与证据

- 代码入口：`src/maple_automation_core/localization/`。
- deterministic tests：`tests/test_localization_transform.py`、`tests/test_localization_platform.py`、
  `tests/test_player_localizer.py`；46 个 localization tests 与 80 个相关契约 tests 已通过。
- resolver 输出：`LocalizationResult.location` 或 `LocalizationResult.fault`；所有结果保留
  transform/platform/observation/evidence digest 与 session/frame/time/pixel lineage。
- 本包内 `digest` 仅表示 canonical content identity，不作为真实性签名或发布 PASS；进入外部 evidence/Bundle
  时必须由预期 digest、固定 manifest 或签名另行绑定。
- 状态：`Completed (code foundation)`，证据等级 `L1`；完整 `G1-LOC-003` 仍为 `In Progress`。
- 上游 CI：OBS002B PR #20 required run `33289661770=success`、merge
  `9aff755f18d3bd48c77084cfaf10ea4df6344f69`、main outer run `33290009677=success`；这些只绑定
  OBS002B code/external smoke。
- 输入审计：`input_owner=legacy`；Core v2 `real_input_call_count=0`；
  `double_write_event_count=0`。

## 8. Feature Flag 与回滚

- Flag：本包不接入 planner/action feature flag；resolver 只作为未接线的纯函数基础。
- 回滚：保持 002B backend 与 002A fake/dry-run 路径，移除或停用 LOC consumer；不触碰
  FrameSource/VC-003、InputSink、receiver、Legacy 输入所有权或 G0 sealed packet。
- 验证：重新运行三组 localization deterministic tests，并确认 Core v2 real input 与 double-write 仍为 0。

## 9. 完成定义

- [x] 版本化 affine transform（map fingerprint lineage）、platform graph（vertical/horizontal 双阈值）、独立匿名 player anchor（pixel digest lineage）和 pure fail-closed resolver 已落地；
- [x] canonical serialization/digest、边界、上下文、lineage、accepted CAS pixel digest → Observation → candidate 精确绑定、identity、generation、freshness、远离平台 unknown、pixel/map fingerprint 绑定、固定 session transform/graph version 与 as-of 时间谱系、计划抑制回归已覆盖；
- [x] 代码与测试边界保持在本包范围；
- [x] 46 个 localization tests 与 80 个相关契约 tests 已通过；
- [x] 输入审计保持 `input_owner=legacy`、Core v2 real input=0、double-write=0；
- [x] 状态只关闭 `Completed (code foundation)`，完整 `G1-LOC-003` 仍 `In Progress`；
- [x] `G1-LOC-003B` marker extraction 与离线 3-run 已由后续包完成；VC-003 只读实机入口仍待执行；
- [ ] 人工 truth、100 圈、实机 LOC、完整 LOC/OBS/G1 Gate（后续任务）。

## 10. 后续任务状态：G1-LOC-003B

1. [完成] 从 B2 accepted `FramePacket`/CAS 读取并提取 minimap yellow marker；保存 candidate 的来源、frame/CAS/pixel digest 与 lineage，不复制外部 raw asset。
2. [完成] 在固定 300-sample 输入上执行 3-run；结果与独立 evidence 见 [`G1-LOC-003B`](G1-LOC-003B-minimap-marker.md)，未覆盖 B2 report。
3. [下一项] 复用 VC-003 read-only 实机入口验证 LOC；维持 `input_owner=legacy`、Core v2
   real input=0、double-write=0，并在单独 Gate 下评估是否扩展证据范围。

## 11. 最终报告

```text
结果：Completed (code foundation)
变更：版本化 affine/platform graph（vertical/horizontal 双阈值）/独立匿名 player anchor（pixel digest lineage）/纯 fail-closed resolver 与 deterministic tests
测试：46 个 localization tests 与 80 个相关契约 tests 已通过
证据：本地 L1 code foundation；上游 OBS002B PR #20 / required run 33289661770 / merge 9aff755f18d3bd48c77084cfaf10ea4df6344f69 / main outer run 33290009677
风险：marker extraction/offline 3-run 已由 003B 完成；人工 truth、100 圈、实机 LOC、完整 LOC-003/OBS/G1 仍待后续任务
回滚：未接线，不改变 Legacy 输入所有权；三组 deterministic tests 可复验
后续任务：VC-003 read-only localization
```
