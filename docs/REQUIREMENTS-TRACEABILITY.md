# Core v2 需求—Gate—证据追踪矩阵

> **状态截止**：2026-08-30
> **战略负责人**：5.6Sol Ultra  
> **战术包负责人**：5.6 Luna max  
> **当前阶段**：G-1 战略封存完成；G0=`PASS`；G1=`In Progress`（完整 `G1-FRM-001`=`Completed`；`G1-OBS-002A`=`Completed`；`G1-OBS-002B` 代码/外部资产烟测=`Completed`；完整 `G1-OBS-002`=`In Progress`）

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
| `DONE-L2-CODE-SMOKE` | 代码与仓库外受控资产 smoke 已通过；不等价于现场捕获、模型质量或对应 Gate |
| `CANDIDATE` | 有 Legacy/离线资产可迁移，尚未通过 Core v2 对应 Gate |
| `PARTIAL-DATA` | 具备部分图片/视频，完整状态或失败样本仍缺 |
| `PARTIAL-G1-FRM` | G1 frame admission synthetic 子包已完成并绑定，完整硬件/corpus Gate 证据仍有缺项 |
| `MISSING` | Core v2 实现、固定输入或报告尚未形成 |
| `DEFERRED-G5/G6` | 已进入路线图，当前阶段保持关闭 |
| `CERTIFIED` | 需要对应 Gate、固定 Bundle 和签字；当前矩阵没有任何功能处于此状态 |

## 3. 原始功能与非功能需求矩阵

| ID | 需求 | 原始来源 | 目标 Gate | 退出所需证据 | 当前事实 | 状态 |
|---|---|---|---|---|---|---|
| REQ-ENV-001 | 双机 Windows 环境：控制端采集/决策，游戏端 receiver | `COLLECTION_ANALYSIS.md` §2/§4/§8 | G2、G3、G6 | 控制端 clean report、游戏端无 Python receiver clean report、租约/双写审计、双机安装矩阵 | Legacy 曾以 `10.66.0.1 → 10.66.0.2:27183` 联通；Core v2 receiver/HIL/clean 报告为空 | `CANDIDATE` |
| REQ-CAP-001 | VC-003 采集卡、`1920×1080` 输入、内容区 `1366×768` | `REQUIREMENTS_CONFIRMED.md` §1/§5；`COLLECTION_ANALYSIS.md` §5/§6 | G1、G3、G4 | FrameSource contract、geometry hash、断序/陈旧/画幅故障 Replay、现场 FPS/失败率 | B2 canonical source `37e57b9662fa3d061e840d4b9c86ab89efe24f2f`、wheel `62b3b2f3...b273f`；300 秒 hardware PASS（8,999 capture / 4,499 admitted，29.996666 / 14.996666 FPS），300 unique corpus、full CAS/provenance/privacy/zero-input 与 Candidate verifier PASS。Issue #13 六角色批准；PR #15 / merge `fe29a4c...` / PR run `33283195258` / main outer run `33283646596` 已绑定，完整 FrameSource 已完成；G3/G4 现场要求仍待后续 Gate | `PARTIAL-G1-FRM` |
| REQ-UI-001 | 目标客户端模板隔离；桌面坐标经内容矩形转为 `1366×768` 归一化坐标 | `REQUIREMENTS_CONFIRMED.md` §1/§5 | G1、G5 | Profile/模板 manifest、geometry/calibration hash、窗口偏移 Replay、每 workflow UI fixture | 已合并的 `G1-FRM-001A` 固定 `1920×1080 → [277,167,1366,768] → 1296×700` 并生成 geometry/calibration identity；真实窗口偏移与 UI/rune workflow 仍待后续包 | `PARTIAL-G1-FRM` + `PARTIAL-DATA` |
| REQ-CV-001 | YOLO monster 检测，ONNX 优先，GPU 推理并保留 CPU 回退，类别/阈值/ROI 可追溯 | `REQUIREMENTS_CONFIRMED.md` §3 | G1、G4 | Model Card、人工真值会话隔离 split、部署 ONNX P/R、PT/ONNX 差、GPU/CPU parity、负样本、Replay/Shadow | `G1-OBS-002A=Completed`；`G1-OBS-002B` 已完成 fail-closed ONNX backend 与外部 model/classes hash 绑定，CPU `3` 次 smoke 的 raw output/result digest 一致；model relative id=`weights/best_forest_v3.onnx`，model/classes SHA 已记录于 `docs/tactical/G1-OBS-002B-onnx-backend.md`；人工 truth、NMS、P/R、GPU/CPU parity 与完整 Replay/Shadow 仍为空 | `DONE-L2-CODE-SMOKE` / runtime `CANDIDATE` |
| REQ-CV-002 | 模型加载失败时抑制动作并记录原因 | `REQUIREMENTS_CONFIRMED.md` §3 | G1、G2 | 模型缺失/hash/class/input mismatch 故障 fixture；WorldState unknown；ActionSpec 数为 0；FaultEvent | `G1-OBS-002A=Completed` 的有限 fault 与 `plan_suppressed=true` 保持；002B 对外部 model/classes/provider/shape 继续 fail-closed；WorldState/Event Tape/Action 下游证据仍待后继包 | `DONE-L2-CODE-SMOKE` / downstream `MISSING` |
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
| REQ-PRI-001 | 账号、角色名、二维码等去标识化 | `MEDIA_REVIEW.md` §1；ADR-007/ROADMAP | G0、G1、G6 | `subject_id`、脱敏审计、fixture manifest、访问/保留策略 | DEC-001 使用匿名 Profile；G0 synthetic fixture 记录 complete de-identification；G1 FrameSource 真实素材已通过 restricted/public、去标识与公开扫描会签；G6 长期数据治理仍待完成 | `DONE-G0/G1` / G6 `PARTIAL-DATA` |
| REQ-REL-001 | 配置、模型、地图、路线、receiver 与证据原子绑定 | ADR-007；由旧资产漂移派生 | G0、G6 | 实际 Runtime Bundle、逐文件 hash、签名、rollback release | Candidate release 已绑定 source `7da29f4...`、Manifest `c3382e8...2007`、10 个资产条目和 evidence graph；strict metadata/full-external 均通过；尚非签名/Certified release | `DONE-G0-candidate` |
| REQ-REL-002 | 受控 Git/CI 与干净机可复现 | ADR-010；由交付要求派生 | G0、G6 | remote、protected branch、CI run/JUnit/coverage、dependency lock、clean reports | run `33204844985` 的 passed metadata 已纳入 sealed packet `04c794c...`，successor run `33205169227` 又复验最终 packet：109 tests、94.61%、27 checks；main `protected=true`，PR #1 required `quality`、protected merge、Owner countersign 与 main post-merge run 已完成 | `DONE-G0-governance` |

## 4. Gate 视图

| Gate | 本矩阵要求的需求集合 | 当前结论 |
|---|---|---|
| G-1 | Pilot、匿名 Profile、输入所有权、原始范围映射 | **战略封存完成**：ADR-004、DEC-001 和本矩阵已形成 |
| G0 | REQ-SAFE-002、OBS-002、PRI-001、REL-001、REL-002 的最小证据链 | **PASS**：工程/失败链、branch protection、required `quality`、PR #1、Owner countersign 与 main post-merge run 已闭环 |
| G1 | CAP-001、UI-001、CV-001/002、LOC-001、FUN-001 的 Replay/Shadow | **In Progress**：完整 `G1-FRM-001`、`G1-OBS-002A` 已 Completed；`G1-OBS-002B` 代码/外部资产 CPU smoke 已完成；人工 truth/evaluation、LOC/WST/Planner/完整 Shadow 仍待完成 |
| G2 | INP-001/002/003、SAFE-001/002、NFR-001 的 simulator/HIL | 未开始 |
| G3 | FUN-001 + 输入租约的单图有界现场 | 未开始；Core v2 现场 session 为 0 |
| G4 | Pilot 打怪、HP/MP、安全、4 小时 Certified | 未开始 |
| G5 | 登录、组队、频道、符文、死亡/断线及受控扩展 | 未开始；多项素材仍缺 |
| G6 | 双机 clean release、支持矩阵、数据治理、Legacy 退役 | 未开始 |

## 5. 当前 G0 收口索引

| 缺口 ID | 对应需求 | 需要生成的首个证据 | 负责人 |
|---|---|---|---|
| CLOSED-G0-001 | REQ-REL-002 | `main protected=true`、required `quality` strict、PR review/管理员约束/linear history/conversation resolution 已启用；PR #1 与 main post-merge run 均成功 | 发布负责人（完成） |
| CLOSED-G0-002 | REQ-OBS-002 | `evidence/failures/failure-index.json` 已交叉链接两次失败、原始材料、hash、根因和关闭谱系 | Luna-M / QA（机器复核完成） |
| CLOSED-G0-003 | REQ-REL-002 | run `33204844985` 与 successor run `33205169227` 的 archive/payload digest、27 checks 和 source/checkout 已复核 | Luna-M / QA（机器复核完成） |
| CLOSED-G0-004 | Gate governance | PR #1 required `quality` 成功并 protected squash merge，形成 Owner countersign 与 G0 `PASS`；main post-merge run 成功 | Owner / Sol-U（完成） |
| CLOSED-G0-REL-001 | REQ-REL-001 | Candidate Bundle、strict metadata/full-external、lock/build hash 已由 source/packet/run 绑定 | 已形成工程证据 |
| CLOSED-G0-RPL-SHD-CLN | REQ-SAFE/OBS/REL | 最小 synthetic Replay、离线 Shadow zero-input、隔离 Windows clean smoke 已完成 | 已形成工程证据；不外推至 G1/G2/现场 |

G0 的完整决策以 `docs/gates/G0-GATE-CHARTER.md` 为准。关闭缺口时必须回填实际 `evidence_id`、commit、Bundle、artifact hash 和评审结论；Markdown 中的计划 ID不替代实际报告。

## 6. 当前 G1 工作索引

| 工作包 | 已落地范围 | 本包后仍待完成 | 当前结论 |
|---|---|---|---|
| G1-FRM-001A | Frame admission、latest slot、DEC-001 geometry/calibration hash、freshness/fault matrix、session reset、三次 synthetic deterministic replay、G0 seal/current checkout CI 分轨；PR #3 已合并 | 001B1、001B2 与完整 FrameSource 审计已随后完成 | `Completed`，真实输入 0 |
| G1-FRM-001 | 001A synthetic admission、001B1 software foundation、001B2 hardware/corpus/Candidate、Issue #13 六角色会签与 Gate Charter 已闭环 | PR #15 / merge `fe29a4c...` / PR run `33283195258` / main outer run `33283646596` 已绑定；下一包为 `G1-OBS-002` | `Completed`，真实输入 0 |
| G1-FRM-001B1 | Pixel V1/CAS、raw capacity=1、VC-003 adapter/fake backend、Legacy local snapshot provenance、corpus/truth 工具、Event Tape、stress、schemas/verifiers、Python 3.12 CI wheel；PR #5 原始实现，PR #7～#10 hardening | B2 使用 source `37e57b9...` 的精确 wheel；本包本身不产生 hardware PASS | `Completed`，真实输入 0 |
| G1-FRM-001B2 | source `37e57b9...` 的 300 秒 VC-003 smoke、4-session/300-sample corpus、3-run replay、4 Event Tapes、CAS/provenance/privacy/zero-input 与会签版 G1 Frame Candidate packet | packaging PR #11 / P=`72c3ad0...` / outer run `33258468278`；Issue #13 六角色批准；PR #15 / merge `fe29a4c...` / main outer run `33283646596` success；`ci-evidence` digest `sha256:9e51d97d858e7432fe85be36fdaeefe7859dd2f4dc5f36ac6e81513d6885fb1c` | `Completed`，真实输入 0 |
| G1-OBS-002A | Observation/Detection/ModelBinding/Fault 契约；Pixel V1→crop/resize→ROI/letterbox；fake detector、provider/shape/hash fail-closed 与 working-space 逆投影 | 真实 ONNX runtime、NMS/temporal、人工 truth、P/R、GPU/CPU parity、Replay/Shadow 与完整 OBS Gate | `Completed`，真实输入 0 |
| G1-OBS-002B | fail-closed ONNX backend、外部 model/classes/runtime hash 绑定、CPU observation smoke | 人工 truth/Model Card、NMS/temporal、P/R、GPU/CPU parity、完整 Replay/Shadow、WorldState/Planner 与实机捕获 | `Completed`（代码/外部资产烟测），真实输入 0 |

`G1-FRM-001` 的完整审计矩阵见
[`docs/gates/G1-FRM-001-GATE-CHARTER.md`](gates/G1-FRM-001-GATE-CHARTER.md)。组织会签入口
[GitHub Issue #13](https://github.com/xphai/mxdauto/issues/13) 已记录 reviewer=`owner-xphai` 对六个
精确角色的独立 `approved` 决定；会签版 Candidate 的 `packet_digest` 为
`4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73`。

### G1-FRM-001A 合并证据

- 实现 PR：[#3](https://github.com/xphai/mxdauto/pull/3)；feature source commit `7cca4154a38e8bca29b917aa3c5abcc43a51391d`；merge commit `b30ddedb1f05945e68fb348b221cdfa123e83c59`。
- CI 绑定：PR run `33225384485`；main run `33225488599`。
- 质量与报告：149 tests、91.38% coverage；Frame Admission `PASS`（3 runs / 15 scenarios / 32 events / zero input）；main frame digest `1c4948afc636ffba45b1f4a769ec7ee3d6d5ea15f09b2b1f9596faa43f837a7d`；checkout smoke 20/20；5 artifact groups。
- 该证据在当时只关闭 `G1-FRM-001A`，不改变 G0 sealed packet 的既有 source/packet 事实；后续 B1/B2 与完整 FrameSource 审计现已完成。

### G1-FRM-001B1 合并证据

- 实现 PR：[#5](https://github.com/xphai/mxdauto/pull/5)；feature source commit `c93f1de9878675722642e5aeba07cc54fbd71752`；protected-main merge commit `3d2f74c21bfb475482a28172018a71740a991aae`。
- CI 绑定：PR run `33244563086`；main run [`33248781581`](https://github.com/xphai/mxdauto/actions/runs/33248781581)，结论 `success`。
- 质量与审计：488 tests、0 failures/errors/skips；94.02% coverage（PixelStore 99.44%）；checkout smoke 23/23；36/36 evidence checks；privacy gate 与 zero-input audit 通过。
- canonical main wheel：`maple_automation_core-0.1.0-py3-none-any.whl`，130,883 bytes，SHA-256 `2c05ab058abfe863165e80e0b635a7608536144147723f7d660e1f6c9ba0e365`；sdist SHA-256 `9bbdac46eed57a7828259ff71def9d74e8a54e4c16d706e1d3447648b96c503a`。
- 该证据在当时只关闭 `G1-FRM-001B1` 并解锁 B2 现场执行；后续 B2 与完整 FrameSource 审计现已完成，Core v2 真实输入仍为 0。


### G1-FRM-001B2 技术证据与组织会签收口

- B1 source：`37e57b9662fa3d061e840d4b9c86ab89efe24f2f`；main CI [`33256230132`](https://github.com/xphai/mxdauto/actions/runs/33256230132) success；wheel 131,432 bytes / SHA-256 `62b3b2f362a60087dffadb1d5529c4d7a27440adf61a28d30b685c7cda3b273f`。
- hardware smoke：300.000 秒连续窗口；8,999 successful / 4,499 admitted；29.996666 / 14.996666 FPS；max accepted age=110 ms，max gap=110 ms，raw slot max depth=1，stop=0.094 s，全部 failure counters=0。
- corpus/audit：4 sessions、300 samples/300 unique pixels、6 categories、100 wrong-size negatives、60 independent reviews；300 CAS objects 全量重算；4 tapes/300 events，无 orphan/mismatch/missing；3 次 replay digest 相同。
- Candidate：`evidence/g1-frame-candidate-20260829/g1-frame-candidate-packet.json`，会签版 packet digest `4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73`；metadata-only、clean-checkout 与受控 full-root verification 均 PASS。
- Outer seal：PR [#11](https://github.com/xphai/mxdauto/pull/11)；PR run [`33258100541`](https://github.com/xphai/mxdauto/actions/runs/33258100541) success；P=`72c3ad081db33d083fdcd5a5e0f62e73f886c233`；outer main run [`33258468278`](https://github.com/xphai/mxdauto/actions/runs/33258468278) success，Candidate conditional verifier 实际执行并通过。
- 会签/SCM：Issue #13 六角色均由 `owner-xphai` 批准；PR [#15](https://github.com/xphai/mxdauto/pull/15) / PR run [`33283195258`](https://github.com/xphai/mxdauto/actions/runs/33283195258) success / merge `fe29a4ce5a8a98c49c85382f083d8429bfee2c38` / main outer run [`33283646596`](https://github.com/xphai/mxdauto/actions/runs/33283646596) attempt 1 success / `ci-evidence` digest `sha256:9e51d97d858e7432fe85be36fdaeefe7859dd2f4dc5f36ac6e81513d6885fb1c`。
- 边界：raw Pixel CAS/视觉 review sheet 保持私有；overall G1 仍 `In Progress`，`G1-OBS-002A` 与 `G1-OBS-002B` 的代码/外部资产 smoke 已完成但完整 OBS 尚未完成，`input_owner=legacy`，Core v2 真实输入为 0。

### G1-OBS-002A 合并封存证据

- 实现 PR：[#17](https://github.com/xphai/mxdauto/pull/17)；source commit `645d3a52d8e2e1364054ad4149f7815feeee733d`；PR run [`33286071567`](https://github.com/xphai/mxdauto/actions/runs/33286071567) `success`；merge commit `1ccbceb79113a0322112b08d1a42a33dcacccad6`。
- PR artifacts（SHA-256 前缀）：`g1-frame-source-b1=0cc18e...`、`frame-admission=6eee1f...`、`checkout=6a27b8...`、`ci-evidence=b7ca02...`、`build=ca6724...`、`quality=ee798f...`。
- main outer run [`33286521402`](https://github.com/xphai/mxdauto/actions/runs/33286521402) attempt 1 在 cacheless checkout regression 中暴露两项 capture-stress 时序偶发失败（541 passed / 2 failed，coverage 93.47%），已隔离；attempt 2 对同一 merge commit 完整重跑并 `success`。attempt 2 artifacts（SHA-256）：`g1-frame-source-b1=43d4f7a4735c6c151876a0d668aea4309679baa75ea2a8dab02e601194f0c922`、`frame-admission=82f8199ad80e0eab8c2f8ca04e225376f683152be983b23d31dac6d4c310c9ea`、`checkout=68db7194d87a072b9559364b204791dcbd9804be6073d45a83fee0204549f2d3`、`ci-evidence=6d1147807a1600069b1a7731803f39b9777ef97772132ac172e09e7314469471`、`build=d6b92239401aefe5625addcd028788831f36a546b225d8471d41f3dbe787e7b3`、`quality=e369a9e70bf3475170cc086fbd308e19c40cc41b4b89ce998cfaa4f4581fa421`。
- 本包已 `Completed`；完整 `G1-OBS-002`、整体 G1 与 G1 Gate 仍为 `In Progress`；`input_owner=legacy`，Core v2 real input calls=0。

### G1-OBS-002B 代码与外部资产烟测证据

- backend：fail-closed ONNX backend；请求与实际 provider 均为 `CPUExecutionProvider`；输入 `images` float32 NCHW `[1,3,640,640]`，输出 `output0` float32 `[1,5,8400]`。
- 外部绑定：model relative id=`weights/best_forest_v3.onnx`，SHA-256=`b279fc566c3d6f1411adedafcadb33fa48d7f2ef1a5289452bf9d5c9607004b4`；classes SHA-256=`07d524938046cff5c328f2b1b4c5b67847aae461172a954f6da19d6bf8954884`；Windows CPython 3.12 ORT 1.23.2 wheel SHA-256=`25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435`；模型与 classes 字节不入仓。
- smoke：真实 CPU 连续 3 次运行；三次 raw ONNX output digest 均为 `2c6a6f02f1c2c3b59179097a6590194c3f130ca309c979b7bde8ee07b9de830e`，三次 Observation `result_digest` 均为 `fb25433072da9ca88989427d977c873e7166d6e47bac6e737962d04225a0bf20`。
- portable report：`evidence/g1-obs-002b/g1-obs-002b-20260830-cpu.json`，source `672ec53327ea79f6ef3bd530f97a3006bd668aff`，report digest `4379951ca0272bdf2e23ea37ec2a7602b92af8ac077450340924fa50582b64c6`；严格 verifier 绑定 tool/schema/lock/ModelBinding。
- 输入审计与边界：`input_owner=legacy`、`real_input_call_count=0`、`double_write_event_count=0`；该证据只关闭代码/外部资产烟测，不构成实机捕获、模型 P/R、完整 `G1-OBS-002` 或整体 G1 PASS。

## 7. 维护规则

1. Luna-M 每个战术包引用至少一个 `REQ-*` 和一个 Gate；
2. 新需求由 Sol-U 分配 ID、范围、Gate 和证据等级后进入实现；
3. 任何状态变更必须链接实际报告，不以聊天结论、文件时长或 Legacy 测试数替代；
4. Bundle/模型/Profile/route 变化后，相关需求状态回到待重验；
5. QA 在每次 Gate 前检查 100% 需求—证据链接和数据缺口；
6. `CERTIFIED` 必须写明 `map_id/profile_id/capability/release_id`，当前没有任何矩阵行满足该状态。
