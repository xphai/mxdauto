# Core v2 需求—Gate—证据追踪矩阵

> **状态截止**：2026-08-29  
> **战略负责人**：5.6Sol Ultra  
> **战术包负责人**：5.6 Luna max  
> **当前阶段**：G-1 战略封存完成；G0=`CONDITIONAL PASS`，在 protected PR #1 required `quality` 成功并 squash merge 时生效；G1 Ready（未开始）

## 1. 用途与来源

本矩阵把原始功能、工程化约束、阶段 Gate 和真实证据状态连接起来。Legacy 实现或历史日志只作为迁移输入，不自动标记 Core v2 需求完成。

主要来源：

- `F:\mxd\REQUIREMENTS_CONFIRMED.md`：目标客户端、原始功能、YOLO、键位、模板、网络输入；
- `F:\mxd\COLLECTION_ANALYSIS.md`：双机、采集、内容矩形、性能、4 小时与失效保护；
- `F:\mxd\MEDIA_REVIEW.md`：当前素材覆盖和数据缺口；
- `evidence/baseline/legacy-snapshot.json`：关键 Legacy 候选的静态 hash；
- `docs/adr/*.md`、`docs/decisions/DEC-001-pilot-baseline.md`、`docs/ROADMAP.md`：Core v2 契约与 Gate。

## 2. 状态定义

| 状态 | 含义 |
|---|---|
| `DONE-L0/L1` | 设计或本地实现存在；只支持该等级结论 |
| `CANDIDATE` | 有 Legacy/离线资产可迁移，尚未通过 Core v2 对应 Gate |
| `PARTIAL-DATA` | 具备部分图片/视频，完整状态或失败样本仍缺 |
| `MISSING` | Core v2 实现、固定输入或报告尚未形成 |
| `DEFERRED-G5/G6` | 已进入路线图，当前阶段保持关闭 |
| `CERTIFIED` | 需要对应 Gate、固定 Bundle 和签字；当前矩阵没有任何功能处于此状态 |

## 3. 原始功能与非功能需求矩阵

| ID | 需求 | 原始来源 | 目标 Gate | 退出所需证据 | 当前事实 | 状态 |
|---|---|---|---|---|---|---|
| REQ-ENV-001 | 双机 Windows 环境：控制端采集/决策，游戏端 receiver | `COLLECTION_ANALYSIS.md` §2/§4/§8 | G2、G3、G6 | 控制端 clean report、游戏端无 Python receiver clean report、租约/双写审计、双机安装矩阵 | Legacy 曾以 `10.66.0.1 → 10.66.0.2:27183` 联通；Core v2 receiver/HIL/clean 报告为空 | `CANDIDATE` |
| REQ-CAP-001 | VC-003 采集卡、`1920×1080` 输入、内容区 `1366×768` | `REQUIREMENTS_CONFIRMED.md` §1/§5；`COLLECTION_ANALYSIS.md` §5/§6 | G1、G3、G4 | FrameSource contract、geometry hash、断序/陈旧/画幅故障 Replay、现场 FPS/失败率 | Legacy 30.09 秒采集 1246 帧、41.41 FPS、读取失败 0；Core v2 只有 FramePacket/geometry 契约 | `CANDIDATE` + `DONE-L1 contract` |
| REQ-UI-001 | 目标客户端模板隔离；桌面坐标经内容矩形转为 `1366×768` 归一化坐标 | `REQUIREMENTS_CONFIRMED.md` §1/§5 | G1、G5 | Profile/模板 manifest、geometry/calibration hash、窗口偏移 Replay、每 workflow UI fixture | Core v2 有 SourceGeometry/坐标契约；目标 Profile 的 UI/rune 资产与 workflow 证据仍不完整 | `DONE-L1 contract` + `PARTIAL-DATA` |
| REQ-CV-001 | YOLO monster 检测，ONNX 优先，GPU 推理并保留 CPU 回退，类别/阈值/ROI 可追溯 | `REQUIREMENTS_CONFIRMED.md` §3 | G1、G4 | Model Card、人工真值会话隔离 split、部署 ONNX P/R、PT/ONNX 差、GPU/CPU parity、负样本、Replay/Shadow | DEC-001 选择 `best_forest_v3-candidate`、`[mob]`、`640×640`；存在本人/技能误检诊断；Core v2 GPU/CPU 报告为空 | `CANDIDATE` |
| REQ-CV-002 | 模型加载失败时抑制动作并记录原因 | `REQUIREMENTS_CONFIRMED.md` §3 | G1、G2 | 模型缺失/hash/class/input mismatch 故障 fixture；WorldState unknown；ActionSpec 数为 0；FaultEvent | Manifest schema/Action contract 已有；runtime/supervisor 尚未实现 | `MISSING` |
| REQ-LOC-001 | 本人位置、地图/平台坐标和时序一致性 | 原始自动打怪需求；`COLLECTION_ANALYSIS.md` §6 | G1、G4 | 人工 truth、坐标变换版本、4 个定位边界回归、100 圈、身份切换 0 | Core v2 有坐标/WorldState 契约；Legacy cache 最近可见 4 个投影失败节点，后续完整报告为空 | `DONE-L1 contract` / runtime `MISSING` |
| REQ-FUN-001 | 自动移动与打怪 | `REQUIREMENTS_CONFIRMED.md` §2/§4 | G1→G4 | Golden plan、Shadow diff、Action lifecycle、Canary、5×4h session、本人误攻击 0 | DEC-001 冻结方向键、`attack=a`、`jump=alt`；只有 Action 数据类型，无 Planner/Controller/现场 | `MISSING` |
| REQ-FUN-002 | 自动补 HP/MP | `REQUIREMENTS_CONFIRMED.md` §2/§4；`MEDIA_REVIEW.md` §1/§3F | G4 | 静态 ROI truth、服药前后动态 fixture、cooldown/失败谓词、Replay/Canary/field | 多档 HP/MP 静态截图已有；服药动态样本仍缺；DEC-001 冻结 Insert/Delete | `PARTIAL-DATA` |
| REQ-FUN-003 | 自动组队 | `REQUIREMENTS_CONFIRMED.md` §2；`MEDIA_REVIEW.md` §3B | G5 | 创建前/后、已组队、不可用、退出/重建 fixture；workflow deadline；Replay/Shadow/Canary | 只有部分队伍页面；完整成功态与退出/重建资料仍缺 | `DEFERRED-G5` + `PARTIAL-DATA` |
| REQ-FUN-004 | 自动换频道 | `REQUIREMENTS_CONFIRMED.md` §2；`MEDIA_REVIEW.md` §3C | G5 | 正常/受伤受限/频道满/网络失败 fixture；deadline；成功率与 P95；无限等待 0 | UI 主流程资料较完整；Legacy 一次长日志出现 1,011 次登录等待，不构成成功证据 | `DEFERRED-G5` + `CANDIDATE` |
| REQ-FUN-005 | 自动登录和选择角色 | `REQUIREMENTS_CONFIRMED.md` §2/§7；`MEDIA_REVIEW.md` §1/§3E | G5 | 去标识登录/大区/账号/选角/失败/返回登录 fixture；deadline；Replay/Canary | 登录/大区/1～3角色图片已有；Core v2 workflow/report 为空 | `DEFERRED-G5` + `PARTIAL-DATA` |
| REQ-FUN-006 | 自动解符文 | `REQUIREMENTS_CONFIRMED.md` §2/§7；`MEDIA_REVIEW.md` §3D | G5 | 符文提示/实体/触发/四方向组合/成功/失败/冷却完整 fixture 与 workflow 证据 | 目标客户端完整符文素材仍缺；Legacy profile rune 目录无目标资产 | `DEFERRED-G5` + `MISSING` |
| REQ-FUN-007 | 捡取与确认键保留 | `REQUIREMENTS_CONFIRMED.md` §4 | G4/G5 | 语义动作映射、冲突测试、后验谓词、Canary evidence | DEC-001 保留 `z`/`space`，当前 flags 关闭 | `CANDIDATE` |
| REQ-REC-001 | 死亡检测与复活 | `MEDIA_REVIEW.md` §1/§2/§3E | G5 | 死亡视频、确认/失败/复活后状态、deadline、Replay/Canary | 死亡视频和复活结果 PNG 已有；Core v2 workflow/report 为空 | `DEFERRED-G5` + `PARTIAL-DATA` |
| REQ-REC-002 | 断线、返回登录、频道满、网络/维护恢复 | `REQUIREMENTS_CONFIRMED.md` §7；`MEDIA_REVIEW.md` §3E | G5 | 每类故障 fixture、deadline、Faulted/人工确认、Replay/Canary | 返回登录/断线/维护等完整失败素材仍缺 | `DEFERRED-G5` + `MISSING` |
| REQ-INP-001 | TCP receiver 支持键盘、绝对鼠标、ACK、session/seq | `REQUIREMENTS_CONFIRMED.md` §6；`COLLECTION_ANALYSIS.md` §8 | G2、G3 | receiver protocol tests、DryRun、clean game host、ACK/TTL/session/generation、HIL | Legacy receiver hash 已入 baseline；Core v2 adapter/clean/HIL 为空 | `CANDIDATE` |
| REQ-INP-002 | 语义键位覆盖方向、攻击、跳跃、确认、捡取、HP/MP、组队及 M/W/I/E/K/Esc/Scroll Lock；瞬移/Buff/回城保持可配置 | `REQUIREMENTS_CONFIRMED.md` §4 | G2、G5 | ResolvedConfig、键冲突/互斥测试、ActionSpec 映射、功能 flag 与每 workflow 后验谓词 | DEC-001 已裁决 Pilot 的方向、`a`、`alt`、`space`、`z`、Insert/Delete、`p`；其余原始键和可配置动作尚未绑定 Bundle | `CANDIDATE` |
| REQ-INP-003 | 游戏端 receiver 保持 PowerShell/SendInput 交付，不依赖单独 EXE | `REQUIREMENTS_CONFIRMED.md` §6 | G2、G6 | 脚本 hash、签名/来源、Windows 10 LTSC 无 Python clean-host、install/upgrade/rollback | Legacy PowerShell receiver hash 已入 DEC-001；Core v2 协议与 clean-host 证据为空 | `CANDIDATE` |
| REQ-SAFE-001 | 断流、断网、超时或退出时暂停并释放全部按键 | `REQUIREMENTS_CONFIRMED.md` §6；`COLLECTION_ANALYSIS.md` §7/§8 | G2、G4 | fault matrix、`release_all ≤1.5s` trace、ActionResult 唯一终态、人工继续 | Action 类型已实现；ActionController/Supervisor/动态释放证据为空 | `DONE-L1 contract` / runtime `MISSING` |
| REQ-SAFE-002 | 所有阶段单一输入所有者，双写为 0 | ADR-004；由原始双机输入要求派生 | G0→G6 | 静态调用审计、dry-run 调用计数、owner lease/revoke/grant、receiver conflict 注入 | G0 minimal Shadow 已绑定 `7da29f4...` / `candidate-core-v2-20260829-shadow`：Core v2 真实输入与双写均为 0；G2/G3 lease/conflict/现场仍缺 | `DONE-G0-minimal` / G2+ `MISSING` |
| REQ-OBS-001 | 调试画面、路线可视化和状态解释 | `REQUIREMENTS_CONFIRMED.md` §2/§3 | G1、G5 | 类型化 telemetry、Frame/WorldState/Action provenance、headless Shadow report、UI smoke | Event Tape 与 G0 headless minimal Shadow report 已形成；G1 感知/WorldState full Shadow 与 UI 仍缺 | `DONE-G0-minimal` / G1 UI `MISSING` |
| REQ-OBS-002 | 截图、录像、日志和故障诊断 | `REQUIREMENTS_CONFIRMED.md` §2 | G0→G6 | Event Tape、artifact hash、session video、fault report、retention/privacy index | sealed packet 有 evidence index、JUnit/coverage、Replay/Shadow/clean/build/CI hash；两次 failed run 的原始材料、artifact digest、根因与修复谱系已进入统一 failure index；现场视频仍缺 | `DONE-G0-minimal` / G1+ `MISSING` |
| REQ-NFR-001 | 处理 `≥15 FPS`、端到端 P95 `<100ms` | `COLLECTION_ANALYSIS.md` §7 | G2、G3、G4 | HIL/field latency trace、帧新鲜度、P95/P99、读帧失败率 | Legacy 推理/网络有局部性能记录；Core v2 HIL/field 为空 | `CANDIDATE` |
| REQ-NFR-002 | 连续 4 小时稳定验收 | `COLLECTION_ANALYSIS.md` §7 | G4 | 固定 Certified Bundle，5 个独立 `EAOH≥4h` session，重启/异常/双写 0 | Core v2 field session 为 0；Legacy 4.69 小时日志含 29 次终止、331 stuck、1,011 登录等待 | `MISSING` |
| REQ-PRI-001 | 账号、角色名、二维码等去标识化 | `MEDIA_REVIEW.md` §1；ADR-007/ROADMAP | G0、G1、G6 | `subject_id`、脱敏审计、fixture manifest、访问/保留策略 | DEC-001 使用匿名 Profile；G0 synthetic fixture 记录 complete de-identification、internal usage/license；真实 G1 素材仍需入库审计 | `DONE-G0-minimal` / G1+ `PARTIAL-DATA` |
| REQ-REL-001 | 配置、模型、地图、路线、receiver 与证据原子绑定 | ADR-007；由旧资产漂移派生 | G0、G6 | 实际 Runtime Bundle、逐文件 hash、签名、rollback release | Candidate release 已绑定 source `7da29f4...`、Manifest `c3382e8...2007`、10 个资产条目和 evidence graph；strict metadata/full-external 均通过；尚非签名/Certified release | `DONE-G0-candidate` |
| REQ-REL-002 | 受控 Git/CI 与干净机可复现 | ADR-010；由交付要求派生 | G0、G6 | remote、protected branch、CI run/JUnit/coverage、dependency lock、clean reports | run `33204844985` 的 passed metadata 已纳入 sealed packet `04c794c...`，successor run `33205169227` 又复验最终 packet：109 tests、94.61%、27 checks；main `protected=true`、required `quality` strict、PR #1 已建立，protected merge 形成 Owner countersign | `DONE-G0-on-protected-merge` |

## 4. Gate 视图

| Gate | 本矩阵要求的需求集合 | 当前结论 |
|---|---|---|
| G-1 | Pilot、匿名 Profile、输入所有权、原始范围映射 | **战略封存完成**：ADR-004、DEC-001 和本矩阵已形成 |
| G0 | REQ-SAFE-002、OBS-002、PRI-001、REL-001、REL-002 的最小证据链 | **CONDITIONAL PASS**：工程/失败链、branch protection、required `quality` 与 PR #1 已闭环；protected squash merge 同时形成 Owner countersign 与 PASS 生效事件 |
| G1 | CAP-001、UI-001、CV-001/002、LOC-001、FUN-001 的 Replay/Shadow | 未开始 |
| G2 | INP-001/002/003、SAFE-001/002、NFR-001 的 simulator/HIL | 未开始 |
| G3 | FUN-001 + 输入租约的单图有界现场 | 未开始；Core v2 现场 session 为 0 |
| G4 | Pilot 打怪、HP/MP、安全、4 小时 Certified | 未开始 |
| G5 | 登录、组队、频道、符文、死亡/断线及受控扩展 | 未开始；多项素材仍缺 |
| G6 | 双机 clean release、支持矩阵、数据治理、Legacy 退役 | 未开始 |

## 5. 当前 G0 收口索引

| 缺口 ID | 对应需求 | 需要生成的首个证据 | 负责人 |
|---|---|---|---|
| CLOSED-G0-001 | REQ-REL-002 | `main protected=true`、required `quality` strict、PR review/管理员约束/linear history/conversation resolution 已启用，PR #1 已创建 | 发布负责人（完成） |
| CLOSED-G0-002 | REQ-OBS-002 | `evidence/failures/failure-index.json` 已交叉链接两次失败、原始材料、hash、根因和关闭谱系 | Luna-M / QA（机器复核完成） |
| CLOSED-G0-003 | REQ-REL-002 | run `33204844985` 与 successor run `33205169227` 的 archive/payload digest、27 checks 和 source/checkout 已复核 | Luna-M / QA（机器复核完成） |
| CLOSING-G0-004 | Gate governance | Sol-U 已条件签发；PR #1 required `quality` 成功并 protected squash merge 时形成 Owner countersign 与 G0 `PASS` | Owner / Sol-U |
| CLOSED-G0-REL-001 | REQ-REL-001 | Candidate Bundle、strict metadata/full-external、lock/build hash 已由 source/packet/run 绑定 | 已形成工程证据 |
| CLOSED-G0-RPL-SHD-CLN | REQ-SAFE/OBS/REL | 最小 synthetic Replay、离线 Shadow zero-input、隔离 Windows clean smoke 已完成 | 已形成工程证据；不外推至 G1/G2/现场 |

G0 的完整决策以 `docs/gates/G0-GATE-CHARTER.md` 为准。关闭缺口时必须回填实际 `evidence_id`、commit、Bundle、artifact hash 和评审结论；Markdown 中的计划 ID不替代实际报告。

## 6. 维护规则

1. Luna-M 每个战术包引用至少一个 `REQ-*` 和一个 Gate；
2. 新需求由 Sol-U 分配 ID、范围、Gate 和证据等级后进入实现；
3. 任何状态变更必须链接实际报告，不以聊天结论、文件时长或 Legacy 测试数替代；
4. Bundle/模型/Profile/route 变化后，相关需求状态回到待重验；
5. QA 在每次 Gate 前检查 100% 需求—证据链接和数据缺口；
6. `CERTIFIED` 必须写明 `map_id/profile_id/capability/release_id`，当前没有任何矩阵行满足该状态。
