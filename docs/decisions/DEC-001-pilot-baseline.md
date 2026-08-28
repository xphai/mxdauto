# DEC-001: Core v2 Pilot 基线决策

**状态**：批准用于 G0/G1 Candidate 与 Shadow（Approved for Candidate/Shadow）  
**日期**：2026-08-29  
**战略负责人**：5.6Sol Ultra  
**实施负责人**：5.6 Luna max  
**适用范围**：单图 Pilot；真实输入仍由 Legacy 独占

## 1. 决策目的

Legacy 的 `profile.yaml`、`config_custom.yaml`、模型 preset 和历史报告对同一运行环境给出了不同的模型、类别、输入尺寸、按键和帧率。DEC-001 冻结一套用于 Core v2 G0/G1 工程与 Shadow 的候选输入，避免战术包继续自行选择配置。

本决策只确定 Candidate 的逻辑身份和来源。它不生成 `runtime-manifest.json`，不代表模型已通过独立验收，也不授予 Core v2 真实输入权。

## 2. Pilot 冻结值

### 2.1 身份与地图

| 字段 | 决策值 | 说明 |
|---|---|---|
| `map_id` | `100040004` | 唯一 Pilot 地图 |
| Legacy 地图目录 | `iv_20260823_073124` | 只读迁移来源，不作为 Core v2 路径契约 |
| `profile_id` | `pilot-subject-01` | 匿名逻辑 ID；不含账号、角色名或设备用户名 |
| `subject_id` | `subject-01` | 仅用于证据关联；映射表保存在受控私有范围 |
| Pilot 能力 | 采集、本人定位、monster 感知、静态路线计划、移动/攻击计划、HP/MP 计划、安全停止、遥测 | G0/G1 只产生回放/Shadow 计划 |
| 暂停能力 | 登录/选角、组队、换频道、符文、死亡/断线恢复、跨图、动态目标路线 | 进入各自 Gate 前保持 flag 关闭 |

### 2.2 候选模型与类别

| 字段 | 决策值 |
|---|---|
| `model_id` | `best_forest_v3-candidate` |
| 只读来源 | `F:\mxd\mob_synth_v2\weights\best_forest_v3.onnx` |
| SHA-256 | `b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4` |
| `classes` | `[mob]` |
| 类别来源 | `F:\mxd\source\MapleStoryAutoLevelUp-main\profiles\maple_legacy_cn\models\classes_v14_mob_only.yaml` |
| 类别文件 SHA-256 | `07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884` |
| `input_size` | `640 × 640` |
| detection confidence | `0.25` |
| IoU | `0.45` |
| combat acquire / refresh | `0.50 / 0.35` |
| ROI | `[0.04, 0.00, 0.98, 0.84]` |
| temporal confirmation | `2` frames；high confidence `0.65`；match IoU `0.15` |
| `player_classes` | `[]`；本人身份必须来自独立定位/名牌链，不从此单类模型推断 |

输入尺寸由 `yolo_presets.py` 的 `mob_synth_v2` preset、训练导出脚本和相关 runtime smoke 共同支持。实际 Candidate Bundle 需要把上述逻辑 ID 映射到 Bundle 内相对路径，并重新计算 Bundle 内文件 hash；运行时禁止依赖 `F:\mxd` 绝对路径。

### 2.3 按键决策

| 语义动作 | Pilot 值 | 决策依据/阶段边界 |
|---|---|---|
| left/right/up/down | Windows 方向键 | `REQUIREMENTS_CONFIRMED.md` 已确认 |
| attack | `a` | 采用最新 Legacy `config_custom.yaml` 的活跃值；旧 Profile 的 `ctrl` 从 Pilot 候选中移除 |
| jump | `alt` | Legacy Profile 与 Custom 一致 |
| HP | `insert` | 原始需求与两份配置一致（`ins` 归一化） |
| MP | `delete` | 原始需求与两份配置一致（`del` 归一化） |
| pickup | `z` | 原始需求保留；G4 前按 flag 管理 |
| confirm | `space` | 原始需求保留；登录等 G5 workflow 启用前关闭 |
| party | `p` | 原始需求保留；G5-P1 证据齐全后启用 |
| aoe/teleport/buff/return_home | unset | 当前原始材料未形成统一键位决策 |

`attack=a` 是 G0/G1 Shadow 比对值。G3 前必须通过动作标定与 Canary Charter 复核；若实测选择变化，发布新的 DEC 修订、Profile hash 和 `release_id`，不在运行时临时覆盖。

### 2.4 捕获与双机边界

| 字段 | Pilot 值 |
|---|---|
| source | `VC-003 Video` / capture card |
| source frame | `1920 × 1080` |
| game content rect | `[277, 167, 1366, 768]`（left, top, width, height） |
| working size | `1296 × 700` |
| requested capture FPS | `30`（G3 门槛以持续实测为准） |
| processing target | `≥15 FPS` |
| Legacy peer | 控制端 `10.66.0.1` → 游戏端 `10.66.0.2:27183` |
| 当前 owner | `legacy` |

IP、设备名和内容矩形是当前实验环境事实；它们仅通过环境/profile adapter 解析，不进入通用领域对象，并记录最终 effective config hash。

### 2.5 路线与 receiver 候选来源

| 资产 | 只读来源 | SHA-256 | 当前资格 |
|---|---|---|---|
| route manifest | `F:\mxd\source\MapleStoryAutoLevelUp-main\minimaps\iv_20260823_073124\spawn_route_manifest.yaml` | `33f36d34ab233a86fa2cdc3227f5bd8511a7a507a9e7bc46c161d8b8738ff19f` | Legacy 静态资产候选；其 `certified` 字段不代表 Core v2 认证 |
| receiver | `F:\mxd\source\MapleStoryAutoLevelUp-main\receiver\input_receiver.ps1` | `b148643588a3a5d38f427d246ab3cea033b946edfe2852e01e416fac544865a9` | G0/G1 只读引用；G2 协议/clean-host Gate 前不参与 Core v2 真实输入 |

首个 G0 Candidate 已把这些来源纳入 `asset-index.json`：MovementProfile `3c7f6b209dae079973cb88b727b6a6b686bdfafc9539f64921bbe605d7568ae8`、PlatformGraph `834892b1d6feca47c3e79f0f2433ed032a61b45c5939a5158590072da264539c`、map fingerprint `8b01434dfcd064b96dc4a9e7a6f3d653e5837324ce2721d89185a962c7a818c2`、data split `6861ee5b4e8417c5e2a8f0853e270a2cb0befb67144da70d82ea874512c210c5`。它们是 `candidate-core-v2-20260829-shadow` 的 G0 content-addressed 候选绑定，不等价于 G1 数据/模型晋级或 Certified Bundle。

## 3. 冲突裁决

| 冲突 | Legacy 候选 A | Legacy 候选 B | DEC-001 裁决 |
|---|---|---|---|
| 模型 | 24 类 `entity_merge_24cls/best.onnx` | 单类 `best_forest_v3.onnx` | 选择单类 `best_forest_v3-candidate` 作为 Shadow 候选 |
| 类别 | 24 类、包含 player/monster 多类 | 单类 `[mob]` | 选择 `[mob]`；本人定位走独立链 |
| 输入尺寸 | `960×960` | `640×640` | 选择 `640×640` |
| attack | `ctrl` | `a` | 选择 `a`；G3 前重新标定 |
| capture FPS | Profile `60` | Custom `30` | Candidate 使用 `30`；Gate 看实测新鲜度和处理率 |
| Profile 身份 | `maple_legacy_cn` 且可能混有运行身份 | Custom 含直接角色标识 | 新建匿名 `pilot-subject-01`；不复制身份字段 |
| 配置来源 | Profile 全量 | Custom 全量 | 两者都只作证据源；Core v2 生成独立 `ResolvedConfig` |

## 4. 已知限制与晋级阻断

1. `second_video_semantic_filter_eval.json` 已记录本人/技能闪光被单类模型识别为 mob；该模型保持 Candidate，G1 独立人工真值验收前不进入 Canary。
2. Forest 评估部分 truth 来自与标签生成相同的启发式流程；它适合诊断，不构成独立泛化结论。
3. G0 已冻结最小 synthetic fixture 并生成 Replay/Shadow/clean engineering smoke；这些报告只验证证据管道，不满足 G1 完整录像 corpus、人工 truth、独立 holdout 或现场验收。
4. Core v2 source commit 为 `7da29f4cfae0bd984b00c394b78e637088a7e452`，sealed packet 为 `04c794c59eb98af6e739415e1ecb72a335795bb9`。远端 run [`33204844985`](https://github.com/xphai/mxdauto/actions/runs/33204844985) 以 `checkout=4317c478...` 生成 passed metadata，并被 sealed packet 纳入；successor run [`33205169227`](https://github.com/xphai/mxdauto/actions/runs/33205169227) 又以 `checkout=04c794c59...` 复验最终 packet。前序失败已进入 `evidence/failures/failure-index.json`，collector 修复在 source commit 中并 fail-closed。
5. GitHub `main protected=true`，required `quality` strict 与 [PR #1](https://github.com/xphai/mxdauto/pull/1) 已建立；Sol-U 已条件签发，PR #1 protected squash merge 时形成 Owner countersign 与 G0 `PASS`。G1 在后继战术包中启动，真实输入边界保持不变。
6. Legacy 长日志包含多次重启、stuck、路线回归和登录等待，不计作 Core v2 现场证据。

## 5. 变更控制

以下任一变化都需要更新本决策或创建后继决策，并生成新的 Profile/Bundle hash：

- `map_id`、profile/subject 语义；
- 模型字节、classes、input size、thresholds、ROI；
- attack/jump/HP/MP 或输入 provider；
- route、MovementProfile、PlatformGraph；
- receiver 协议或输入 owner；
- 捕获 geometry、内容区变换或处理帧率策略。

Luna-M 在战术包中实现冻结值并生成证据。Sol-U 审计变更是否保持 G-1 范围；G0 Gate 仍按 `docs/gates/G0-GATE-CHARTER.md` 独立评审。

## 6. G-1 决策结论

DEC-001 解决 Pilot 的选择歧义并完成战略封存：`map_id=100040004`、匿名 `pilot-subject-01`、`best_forest_v3-candidate`、单类 `[mob]`、`640×640`、`attack=a`。此结论只授权 G0/G1 Candidate、Replay 和 Shadow 战术包；真实输入边界遵循 ADR-004。
