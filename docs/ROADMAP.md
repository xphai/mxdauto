# Maple Automation Core v2 战略路线图（G-1 → G6）

> **状态截止**：2026-08-29（Asia/Shanghai）
> **当前判定**：主线决策已形成，执行已进入 G0 本地建设；G-1 的可审计封存与 G0 的退出证据均未齐备。因此当前正式门禁为 **G-1 待封存 / G0 进行中**，G1 及以后均未开始。
> **战略与 Gate 负责人**：**5.6Sol Ultra**（GPT-5.6 Sol / Ultra；下文简称 **Sol-U**）
> **战术包负责人**：**5.6 Luna max**（GPT-5.6 Luna / max；下文简称 **Luna-M**）
> **现场输入边界**：G0～G2 期间，Core v2 的真实输入调用数保持为 0，Legacy 保持唯一真实输入下发权。G3 仅在独立 Gate 批准的 Canary 会话内切换单一输入所有者；任何时刻只保留一个写入者。

---

## 1. 路线图用途与唯一北极星

本路线图把 ADR、代码、测试、Runtime Bundle、回放、Shadow、干净机、现场会话和发布回退串成一条可执行的晋级链。任何阶段的“完成”只由退出门禁及其绑定证据决定，代码数量、测试数量、模型 mAP、日志跨度或 Legacy 资产中的 `certified` 字样均不单独构成阶段完成。

### 唯一北极星

> 在一个冻结的 Pilot 地图、一个匿名角色档案和一个原子 Runtime Bundle 上，以 Core v2 完成可回放、可解释、可安全停止、可整体回退的双机闭环；随后按相同证据契约扩展原始功能与支持矩阵，最终退役 Legacy 的常态输入职责。

### 阶段串行关系

```text
G-1 主线与范围封存
  → G0 可复现工程/证据基线
  → G1 确定性 Replay + 完整 Shadow
  → G2 执行内核与故障安全
  → G3 单图有界 Canary
  → G4 单图 Certified（5 × 4h）
  → G5 原始功能闭环与受控扩展
  → G6 可持续发布与 Legacy 退役
```

数据准备、工具探索和设计草案可以并行；**阶段晋级保持串行**。后续阶段的试验结果可作为研究资料保存，不用于提前授予输入权或发布状态。

---

## 2. 事实边界：截至当前真正拥有的证据

### 2.1 状态用语

| 状态 | 含义 |
|---|---|
| 已落地（L0/L1） | 文件或本地产物存在，尚未自动等价为远端、回放、干净机或现场证据 |
| 证据待绑定 | 功能或本地检查已有结果，但缺少 commit、run、Bundle、环境和报告 ID 的完整绑定 |
| 进行中 | 已有部分工作包，退出门禁仍有空项 |
| 未开始 | 尚无符合本路线图证据契约的实现或报告 |
| Quarantined | 结果被保留用于分析，但当前不参与晋级 |
| Gate Passed | Sol-U 完成证据审计，产品/QA/发布等指定签字人确认，所有强制条件均满足 |

### 2.2 Core v2 当前盘点

| 事项 | 当前事实 | 证据级别 | 路线图判定 |
|---|---|---:|---|
| 主线决策 | `ADR-001` 已接受，指定 `F:\mxd\product\maple-automation-core` 为唯一受控主线 | L0 | 决策已形成；封存证据待补 |
| 时间与状态契约 | `FramePacket`、`SourceGeometry`、`CaptureHealth`、坐标对象、`PlayerState`、`WorldObservation`、`WorldState` 已实现 | L1 | G0 契约工作包本地完成 |
| 动作契约 | `ActionSpec`、`ActionHandle`、`ActionResult` 及终止类型已实现；`ADR-006` 已接受 | L1 | 领域对象已落地；`ActionController`、`ControlArbiter`、`ResultVerifier` 尚未实现 |
| Event Tape | 哈希链、严格 JSON、顺序/会话校验与相关测试已实现 | L1 | 记录格式已落地；完整 Replay runner 尚未实现 |
| Runtime Manifest | schema、示例 fixture、校验工具与测试已存在；`ADR-007` 已接受 | L1 | 示例只验证 schema；实际 Candidate Bundle 尚不存在 |
| 静态 CI 定义 | `.github/workflows/ci.yml` 已存在，配置 Ruff、Mypy、Manifest、Pytest/coverage | L0 | 本地 workflow 定义已落地；远端运行证据为 0 |
| 本地质量结果 | 本工作会话最近一次本地验证记录为 58 passed、行覆盖率 94.68%；`coverage.xml` 的 `line-rate=0.9468` | L1 | 可作为开发反馈；缺少 commit/run/JUnit 绑定，尚非 G0 退出证据 |
| Legacy 基线快照 | `evidence/baseline/legacy-snapshot.json` 绑定 8 个关键文件、Pilot 候选和上游 commit | L1 | 静态输入清单已落地；它不是 Runtime Bundle，也不是现场认证 |
| Golden Replay | 仓库中没有冻结 golden fixture、Replay runner 或 `replay_report_id` | — | 未开始 |
| Shadow | 仓库中没有 Shadow runner、dry-run sink 审计或 `shadow_report_id` | — | 未开始 |
| 干净机 | 没有独立 Windows 环境的安装/启动/回放 smoke 报告 | — | 未开始 |
| Core v2 现场 | 没有 Core v2 现场 session；Core v2 尚未接入真实输入 | — | 未开始 |
| Git 历史 | 本地 `.git` 与 `main` 已建立，本路线图和首批契约随当前首个基线提交封存 | L1 | 本地 SCM 起点已完成；远端证据仍待绑定 |
| GitHub 远端 | Core v2 当前 `git remote -v` 为空 | — | 远端、PR、分支保护、GitHub Actions run 均未建立 |
| 本地 wheel | `dist/maple_automation_core-0.1.0-py3-none-any.whl` 存在 | L1 | 本机构建产物；尚未绑定 commit/Bundle，也没有干净机安装结论 |

### 2.3 Legacy 与旧证据的可用范围

1. `F:\mxd\source\MapleStoryAutoLevelUp-main` 本身没有 Git 元数据；它只作为只读迁移来源。独立上游克隆 `F:\mxd\_upstream_MapleStoryAutoLevelUp` 的 HEAD 为 `3e19173f8da5aab8405307bb9c6e3741dd3abd6b`，且其 GitHub `origin` 只属于上游克隆，**不代表 Core v2 已配置远端**。
2. `legacy-snapshot.json` 把 Pilot 标记为 `candidate`：地图 `100040004`、档案 `maple_legacy_cn`、模型 `best_forest_v3`。这只是候选引用。
3. Legacy 当前配置存在关键漂移：
   - `profiles/maple_legacy_cn/profile.yaml` 使用 `ctrl` 攻击、24 类模型、`960×960` 输入；
   - `config/config_custom.yaml` 使用 `a` 攻击、单类 `mob` 模型，并指向 `best_forest_v3.onnx`；
   - 两者尚未解析为一个冻结的 `effective_config`。
4. Legacy 路线 manifest 中的 `certification.status: certified` 只说明旧路线资产通过其自身静态校验，不等价于 Core v2 Bundle、整机闭环或现场认证。
5. Legacy 日志证明采集卡、远程输入和 YOLO 曾经接通，也同时证明闭环仍不稳定：
   - `MSBot_2026-08-28_22-44-34.log` 中有约 1.4～1.9 ms RTT、采集卡打开和 YOLO ready，同时在短会话内出现两次 10 秒 stuck；
   - `MSBot_2026-08-28_08-46-35.log` 虽跨度约 4.69 小时，但含 29 次线程终止、331 次 `Player stuck`、346 次循环路线回归和 1,011 次登录等待，因此不计为“4 小时连续稳定”证据。
6. `optimization_summary.json` 的模型指标、`new_video_runtime_smoke.json` 的推理速度可用于候选筛选；`second_video_semantic_filter_eval.json` 已记录本人/技能特效被识别为 mob，且该视频带调试叠加。模型晋级仍需会话隔离、人工真值、部署 ONNX 的独立验收。
7. `MEDIA_REVIEW.md` 说明组队完成态、完整符文、补药动态、断线/维护等素材仍有缺口。这些能力在 G5 前保持关闭状态。

---

## 3. 决策权、负责人和模型分工

模型负责把工作变成可审计的决策与执行包；产品、现场、QA、发布等人类签字人对真实范围、现场窗口和发布承担最终确认责任。

### 3.1 模型职责

| 职责 | Sol-U（GPT-5.6 Sol / Ultra） | Luna-M（GPT-5.6 Luna / max） |
|---|---|---|
| 战略与范围 | **A/R**：北极星、范围冻结、支持矩阵、优先级、停止条件 | C：将战略拆成有界战术包 |
| ADR/契约方向 | **A**：批准或否决关键边界变更 | R：按已接受 ADR 实现；发现冲突即升级 |
| Gate Charter | **A/R**：在执行前冻结指标、样本、窗口、签字人 | C：验证指标可采集、命令可执行 |
| 战术实施 | C：只审战略偏差和跨包依赖 | **A/R**：实现、测试、脚本、报告、回退演练 |
| 证据审计 | **A/R**：检查谱系、证据等级、缺口与替代关系 | R：生成并登记原始证据，保持失败结果 |
| 晋级/隔离/回退决定 | **A**：给出 `PASS / HOLD / QUARANTINE / ROLLBACK` 结论 | R：执行已批准决定，回传结果 |
| 现场输入权 | 与产品/现场负责人共同批准 Gate | 仅在批准窗口内执行战术包，不自行扩大输入范围 |

### 3.2 人类角色最小集合

| 角色 | 主要责任 |
|---|---|
| 产品负责人 | 北极星、Pilot、功能范围、支持矩阵、最终发布签字 |
| 技术负责人 | 架构、ADR、迁移边界、缺陷优先级 |
| QA/证据负责人 | 样本独立性、报告真实性、需求—证据覆盖 |
| 现场负责人 | 双机窗口、人工接管、会话标注、停止操作 |
| 发布负责人 | 远端、CI、Bundle、签名、干净机、回滚 |
| 数据/CV 负责人 | 数据拆分、隐私、人工真值、模型卡与模型晋级 |

**隔离原则**：Luna-M 作为主要实现者时，Gate 审计由 Sol-U 主持；最终现场签字至少包含 QA/现场/发布中的相应负责人。`HOLD` 表示保持当前阶段，不用降低阈值来制造通过。

### 3.3 输入所有权随阶段变化

| 阶段 | Legacy | Core v2 | 规则 |
|---|---|---|---|
| G-1～G2 | 唯一真实输入写入者 | Replay/Shadow/dry-run，真实调用数 0 | Legacy 实际动作仅作为对照事件 |
| G3 | Canary 窗口外保持写入权；窗口内停止写入 | 仅在批准的单图会话内获得独占租约 | 切换前后执行 `release_all`，双写事件数必须为 0 |
| G4～G5 | 认证范围内作为紧急回退路径 | 认证范围内为主写入者 | 未认证能力继续使用关闭状态或 Legacy 回退 |
| G6 | 归档只读；保留期结束后退出常态运行 | 认证支持矩阵的唯一主写入者 | 回退优先使用上一 Certified Bundle |

`ADR-001` 中“所有输入控制入口仅保留 Core v2 架构”按**目标架构**解释；当前运行边界以 `ADR-006`、`ADR-010` 和本表为准。Sol-U 在 G-1 封存前补充一份 ADR 澄清，明确 G3 的独占交接点。

---

## 4. 证据等级与统一 Gate 协议

### 4.1 证据等级

| 等级 | 证据 | 可支持的结论 |
|---:|---|---|
| L0 | ADR、设计、路线图、schema 文本 | 方向与契约已定义 |
| L1 | 本地源码、本地测试输出、静态快照、本地 wheel | 本地实现存在，适合开发反馈 |
| L2 | 不可变 commit/tag、远端 CI run、JUnit/coverage、artifact hash | 某个 commit 的工程基线可复现 |
| L3 | 固定输入 + 固定 Bundle 的 Replay/Shadow 报告 | 离线确定性与计划链可复现 |
| L4 | 干净机、receiver dry-run、故障注入、回退演练 | 部署/执行边界和失效保护可复现 |
| L5 | 绑定 Bundle 的有界现场 session、录像和 Event Tape | 指定现场窗口内的实际行为 |
| L6 | 多次独立 L5 会话 + Gate 签字 + 可用回退 Bundle | 指定支持范围的 Certified 结论 |

高等级结论需要对应等级的证据。Legacy L5 日志、模型训练报告或路线静态认证不会自动转化为 Core v2 的 L5/L6。

### 4.2 每次 Gate 的固定流程

1. **Sol-U 发布 Gate Charter**：冻结范围、Bundle、数据 split、阈值、会话数、签字人和回退目标。
2. **Luna-M 拆分并执行战术包**：每包使用 `docs/templates/tactical-package.md`，一个包只负责一个可独立回退的结果。
3. **证据生成**：所有报告记录 `evidence_id`、`source_commit`、`release_id`、manifest hash、环境、命令、开始/结束时间、结果与原始 artifact hash。
4. **失败保留**：失败报告进入 evidence index，Bundle 进入 `Quarantined`；随后创建新 package/release ID。
5. **Sol-U 审计**：逐项检查门禁，结论限定为 `PASS / HOLD / QUARANTINE / ROLLBACK`。
6. **人类签字**：涉及范围、现场或发布的 Gate 由对应人类负责人签字。
7. **原子晋级**：只切换完整 Bundle 状态或指针；单文件热替换不参与认证。

### 4.3 统一证据索引最小字段

```text
evidence_id
stage / gate
package_id
source_commit / upstream_commit
release_id / runtime_manifest_sha256
input_fixture_ids / field_session_ids
runner_os / python_version / dependency_lock_sha256
commands[] / check_results[]
artifact_paths[] / artifact_sha256[]
started_at / completed_at
owner / reviewer / gate_decision
rollback_release_id
```

---

## 5. 总览：阶段状态与输入权限

| Gate | 阶段主题 | 当前状态 | 阶段结束时获得的权限 |
|---|---|---|---|
| G-1 | 主线、范围、Pilot、所有权封存 | **部分完成；待封存** | 允许在唯一主线执行 G0 战术包 |
| G0 | Git/CI/契约/Bundle/最小证据流水线 | **进行中** | 允许进入完整 Replay/Shadow；真实输入仍为 0 |
| G1 | 确定性 Replay、感知/WorldState、完整 Shadow | **未开始** | 允许构建执行内核；真实输入仍为 0 |
| G2 | ActionController、Supervisor、receiver dry-run、故障安全 | **未开始** | 具备提交 Canary Gate 的资格；真实输入默认仍为 0 |
| G3 | 单图、单档案、单 Bundle 的有界 Canary | **未开始** | 仅认证窗口内的 Core v2 独占输入权 |
| G4 | 单图 Certified，5 次独立 4 小时会话 | **未开始** | Pilot 范围内的常态 Core v2 输入权 |
| G5 | 原始功能闭环与逐项认证扩展 | **未开始** | 已认证功能/地图/Profile 的受控扩展 |
| G6 | 可持续发布、支持矩阵、Legacy 退役 | **未开始** | 认证支持矩阵的正式发布与运维状态 |

---

## 6. G-1 — 主线与范围封存

### 目标

把“Core v2 为唯一主线”从设计决定变成可审计、可执行的范围契约；冻结 Pilot 和 Legacy 例外清单，消除输入归属与配置来源歧义。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 | 当前状态 |
|---|---|---|---|---|
| G-1-DEC-001 | 封存 ADR-001：主线、非目标、阶段链、签字人、决策日期 | 无 | Sol-U A/R；Luna-M C | ADR 已接受；签字/commit 待补 |
| G-1-PIL-002 | 冻结 Pilot：`map_id`、匿名 `profile_id`、攻击/跳跃键、模型、类别、输入尺寸、阈值、路线、MovementProfile、receiver | DEC-001 | Sol-U A；Luna-M R | 只有 candidate；配置冲突待解 |
| G-1-FRZ-003 | 建立 Legacy 变更白名单：只含迁移 adapter 与阻塞缺陷；新增功能进入 Core v2 | DEC-001 | Sol-U A；Luna-M R | 政策已写；执行审计待补 |
| G-1-OWN-004 | 补充输入所有权 ADR：G0～G2、G3 Canary、G4+ 的单写入者切换 | DEC-001 | Sol-U A/R；Luna-M C | 待开始 |
| G-1-TRC-005 | 建立需求—战术包—测试—fixture—session—release 追踪矩阵 | DEC-001 | Sol-U A；Luna-M R | 待开始 |

### 退出门禁

- [ ] 主线、Pilot、非目标、Legacy 例外与输入交接均由 ADR/范围记录覆盖；
- [ ] Pilot 中每个资产只有一个逻辑 ID 和 SHA-256；Legacy 的 `profile.yaml` 与 `config_custom.yaml` 冲突已形成明确决策；
- [ ] 角色/账号标识已替换为匿名 `subject_id`；
- [ ] Legacy 新功能冻结有可审计差异基线和例外审批流程；
- [ ] 跟踪矩阵覆盖 `REQUIREMENTS_CONFIRMED.md` 的全部原始功能，并把 G5 数据缺口标记为阻塞；
- [ ] Sol-U 给出 `PASS`，产品与技术负责人签字；
- [ ] Gate packet 进入首个受控 commit。若首个 commit 归入 G0-SCM-004，本 Gate 保持 `HOLD` 直至该提交存在。

### 必需证据

`ADR-001`、输入所有权澄清 ADR、Pilot 决策记录、Legacy 冻结清单、资产冲突决策、追踪矩阵、签字记录、首个 commit hash。

### 回退

主线或 Pilot 再次出现争议时，冻结 Core v2 与 Legacy 的新功能，只保留只读审计和数据整理；回到 G-1 重新签署，不让两条主线同时增长。

---

## 7. G0 — 可复现工程与证据基线

### 目标

让某个 Core v2 commit 可以在远端 CI 和独立 Windows 环境中重复构建、测试、校验 Candidate Bundle，并首次跑通最小 Replay、最小 Shadow 和 clean smoke 证据链。G0 只证明“工程基线和证据管道可工作”，不授予真实输入权。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 | 当前状态 |
|---|---|---|---|---|
| G0-CON-001 | Frame/坐标/Player/WorldState/Action 不可变契约及 contract tests | G-1-DEC | Sol-U A；Luna-M R | **本地已落地，证据待绑定** |
| G0-EVT-002 | Event Tape 严格序列化、hash chain、篡改/顺序/会话检测 | G0-CON | Sol-U A；Luna-M R | **本地已落地，证据待绑定** |
| G0-MAN-003 | Manifest schema、validator、示例 fixture；生成第一个使用真实 hash 的 Candidate manifest | G-1-PIL、G0-CON | Sol-U A；Luna-M R | schema/tool 已落地；实际 Candidate 待补 |
| G0-SCM-004 | 创建首个 commit/tag；配置 Core v2 远端、PR、main 保护和必需检查 | G-1 Gate | Sol-U A；Luna-M R，发布负责人 R | **首个本地基线提交纳入本轮；0 remote** |
| G0-CI-005 | CI 生成 JUnit、coverage、manifest 结果和 evidence metadata；上传稳定命名 artifact | G0-SCM | Sol-U A；Luna-M R | workflow 定义已有；远端 run/报告待补 |
| G0-DEP-006 | 锁定依赖与构建工具；wheel/sdist 绑定 commit、锁文件和 SHA-256 | G0-SCM | Sol-U A；Luna-M R | 版本范围已有；锁与可追溯制品待补 |
| G0-RPL-007 | 冻结最小去标识 golden fixture；实现 Replay runner，重复 3 次输出同一事件 digest | G0-EVT、G0-MAN | Sol-U 定义样本/阈值；Luna-M 实现 | 待开始 |
| G0-SHD-008 | 实现最小 Shadow runner/dry-run sink；记录计划与 Legacy 实际动作；动态证明 Core v2 真实输入调用数为 0 | G0-RPL | Sol-U A；Luna-M R | 待开始 |
| G0-CLN-009 | 在无项目缓存、无 `F:\mxd` 隐式资产的 Windows 机器/VM 中完成 checkout、install、test、manifest、replay、shadow smoke | G0-CI、DEP、RPL、SHD | Sol-U 定义 Gate；Luna-M + 发布负责人 R | 待开始 |
| G0-EVD-010 | 建立 evidence index、报告 schema、artifact retention 与 hash 校验 | G0-CI | Sol-U A；Luna-M R | 待开始 |

### 退出门禁

- [ ] Core v2 至少有一个不可变 commit，Core v2 远端已配置；默认分支、PR 和必需检查状态可查；
- [ ] 远端 CI 在同一 commit 上全部绿灯：Ruff lint/format、Mypy、Manifest、Pytest，覆盖率 `≥90%`；
- [ ] JUnit、coverage、evidence metadata 和构建制品绑定同一 commit；
- [ ] 一个实际 Candidate Bundle 通过 schema 与逐文件 hash 校验，`source_commit` 为真实 commit，示例占位值未进入 Candidate；
- [ ] Golden fixture 具有 ID、SHA-256、来源、画幅、去标识记录、人工 truth/split 说明；
- [ ] Replay runner 对同一 fixture + Bundle 连续 3 次产生相同事件序列与 digest；
- [ ] Shadow smoke 生成实际报告，Core v2 真实输入调用为 0，计划动作与实际动作字段明确区分；
- [ ] 独立 Windows clean smoke 成功，报告中不引用本机缓存或未入 Bundle 的绝对路径；
- [ ] 首个 rollback 目标明确为“停止 Core v2 runner，继续 Legacy 独占输入”；
- [ ] Sol-U 给出 `PASS`，QA 与发布负责人签字。

### 必需证据

远端 URL、commit/tag、CI run/attempt、JUnit、coverage、dependency lock hash、wheel hash、Candidate manifest/hash、Replay report、Shadow report、clean-machine report、evidence index、Gate packet。

### 回退

停止 Replay/Shadow runner，删除或关闭 Core v2 启动 flag，保留 Legacy 的输入所有权；Candidate Bundle 标记 `Quarantined`。G0 尚无 Core v2 现场输入，因此回退过程不触碰游戏端控制状态。

---

## 8. G1 — 确定性 Replay、感知/WorldState 与完整 Shadow

### 目标

让 Core v2 从固定录像或 Frame fixture 构造可追溯的 `Observation → WorldState → ActionSpec`，在完整 Pilot 数据集上确定性回放，并与 Legacy 实际行为进行只读 Shadow 对照。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G1-FRM-001 | `FrameSource` adapter、最新帧策略、内容区/ROI 校准、陈旧/断序/画幅变化检测 | G0 PASS | Sol-U 契约；Luna-M 实现 |
| G1-OBS-002 | 采集→标准化→检测 adapter；统一部署 ONNX、classes、input size、thresholds | G1-FRM、Pilot Bundle | Sol-U 晋级规则；Luna-M 实现 |
| G1-LOC-003 | 玩家身份、地图/平台坐标、置信度和未知态；所有变换携带版本 | G1-OBS | Sol-U 不变量；Luna-M 实现 |
| G1-WST-004 | 纯函数式 WorldState reducer、clock/randomness 注入、状态版本与 provenance | G1-OBS、LOC | Sol-U 契约；Luna-M 实现 |
| G1-PLN-005 | Pilot 静态路线 Planner，仅输出 `ActionSpec`；无输入 adapter 依赖 | G1-WST | Sol-U 范围；Luna-M 实现 |
| G1-RPL-006 | Golden corpus 扩展、人工真值、负样本、会话隔离 split、确定性报告 | G1-* | Sol-U 样本/阈值；Luna-M + QA/CV 执行 |
| G1-SHD-007 | Legacy Event adapter、时间对齐、diff taxonomy、原因码、覆盖率报告 | G1-PLN | Sol-U 定义风险差异；Luna-M 实现 |
| G1-MDL-008 | Model Card、PT→ONNX 一致性、独立真实 holdout、已知失败与回滚模型 | G1-OBS | Sol-U Gate；Luna-M + CV 执行 |

### 退出门禁

- [ ] 每个 `ActionSpec` 可回溯到 `release_id → WorldState version → observation → frame_id → fixture hash`；
- [ ] 同一 Golden corpus + Bundle 连续 3 次产生相同 WorldState/Action/Event digest；
- [ ] 陈旧、重复、断序、失配画幅和模型加载失败均产生显式事件，计划输出被抑制；
- [ ] Golden split 以完整会话/地图隔离，truth 由人工复核；调试叠加视频只保留为诊断 fixture；
- [ ] 部署 ONNX 在独立人工真值集上达到 Precision `≥0.95`、Recall `≥0.95`；本人被识别为 monster 为 `0`；PT/ONNX 的 Precision/Recall 差值各 `≤1` 个百分点；
- [ ] Shadow 中 Core v2 真实输入调用持续为 0；所有 Legacy/Core 差异均有分类，未分类差异为 0，任何安全否决差异为 0；
- [ ] 需求—证据追踪覆盖 Pilot 的感知、定位、路线计划和安全停止场景 100%；
- [ ] Sol-U `PASS`，QA/CV/技术负责人签字。

### 必需证据

Golden corpus manifest、人工 truth/split hash、Model Card、ONNX 报告、Replay reports、Shadow diff reports、Frame/WorldState provenance audit、异常 fixture 报告、Gate packet。

### 回退

关闭新增 adapter/Planner flag，恢复上一个 Replay-valid Bundle；Shadow 停止后继续由 Legacy 独占现场输入。未通过的模型或数据 split 进入 `Quarantined`，旧报告保持可查。

---

## 9. G2 — 执行内核、监督器与故障安全

### 目标

在 simulator、dry-run receiver 和硬件在环诊断模式中实现动作生命周期、唯一执行仲裁、后验结果验证、运行监督和故障安全。G2 结束只代表“具备申请 Canary 的条件”。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G2-ARB-001 | `ControlArbiter`：优先级、租约、session/generation、TTL、取消、互斥 | G1 PASS | Sol-U 策略；Luna-M 实现 |
| G2-ACT-002 | `ActionController`：唯一 `InputSink` 入口、状态守卫、单次终态化、`release_all` | G2-ARB | Sol-U 契约；Luna-M 实现 |
| G2-VER-003 | `ResultVerifier`：以后验 WorldState 谓词判定成功，ACK 只作为送达证据 | G2-ACT、G1-WST | Sol-U 语义；Luna-M 实现 |
| G2-SUP-004 | `RuntimeSupervisor`：Stopped/Starting/Running/Paused/Recovering/Faulted，deadline 工作流 | G2-ACT | Sol-U 状态模型；Luna-M 实现 |
| G2-RCV-005 | receiver v2 adapter：version/seq/session/nonce/TTL/token/HMAC/allowlist/receipt/dead-man；DryRun | G2-ACT | Sol-U 协议 Gate；Luna-M 实现 |
| G2-FLT-006 | 故障注入：断帧、陈旧帧、ACK timeout、断网、旧 generation、receiver 重启、进程异常 | G2-* | Sol-U 故障矩阵；Luna-M + QA 执行 |
| G2-HIL-007 | 游戏端 clean receiver smoke：Windows 10 LTSC、无 Python 前提、PowerShell receiver、结构化日志 | G2-RCV | Sol-U Gate；Luna-M + 现场/发布执行 |
| G2-RBK-008 | Kill switch、输入租约交接、上一 Bundle/Legacy 回退演练 | G2-* | Sol-U 批准；Luna-M 执行 |

### 退出门禁

- [ ] 每个已签发 `ActionHandle` 有且只有一个 `ActionResult`；超时、取消、失联、陈旧、前置丢失均终态化；
- [ ] 静态调用图与动态审计均显示 `ActionController` 之外真实 `InputSink` 写入数为 0；
- [ ] 过期、旧 session、旧 generation、陈旧 WorldState 的动作到达 receiver 数为 0；
- [ ] receiver 断连、心跳超时或进程退出后 `release_all ≤1.5s`；
- [ ] simulator/HIL 中端到端动作延迟 P95 `<100ms`、P99 `<150ms`；
- [ ] 未捕获异常为 0；Faulted 状态会停止新动作并保留完整 Event Tape；
- [ ] 游戏端 clean smoke 证明 receiver 在目标 OS 与无 Python 前提下可启动、DryRun、停止和释放按键；
- [ ] Kill switch 与回退演练成功，双写事件为 0；
- [ ] Sol-U `PASS` 只授予“提交 G3 Canary 申请”的资格，现场/QA/技术负责人签字。

### 必需证据

Action lifecycle report、调用点审计、fault matrix/JUnit、receiver protocol report、HIL/clean-game-host report、latency trace、release_all trace、kill-switch/rollback drill、Canary proposal。

### 回退

执行 `release_all → 撤销 Core v2 输入租约 → 停止 Core v2 runner → 验证 Legacy 单写入 → 保存 Event Tape/receiver log`。任何 G2 故障都保持 Core v2 真实输入关闭。

---

## 10. G3 — 单图有界 Canary

### 目标

首次在 `map_id=100040004`、单一匿名 Profile、单一 Candidate Bundle 上授予 Core v2 有时限的独占真实输入租约；通过分级、有人监护的现场会话验证闭环，同时随时可切回 Legacy。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G3-GCH-001 | Canary Gate Charter：场地、Bundle、窗口、阈值、值守、停止按钮、回退目标 | G2 PASS | Sol-U A/R；Luna-M C |
| G3-HOF-002 | 单写入者交接：Legacy stop/observe、`release_all`、Core lease、结束再释放 | G3-GCH | Sol-U 批准；Luna-M 实现/执行 |
| G3-C0-003 | 3 × 10 分钟有人监护 Canary | G3-HOF | Luna-M + 现场 R；Sol-U 审计 |
| G3-C1-004 | 3 × 30 分钟有人监护 Canary | C0 PASS | Luna-M + 现场 R；Sol-U 审计 |
| G3-C2-005 | 3 × 2 小时有人监护 Canary | C1 PASS | Luna-M + 现场 R；Sol-U 审计 |
| G3-RCA-006 | 每级后的 diff、stuck、误攻击、恢复、延迟和人工接管复盘 | 每级会话 | Sol-U A；Luna-M + QA R |

### 退出门禁

- [ ] 所有会话绑定同一 `release_id`；任何代码/模型/阈值/路线变化都生成新 Bundle 并从 C0 重新开始；
- [ ] Legacy 与 Core v2 同时写入事件数为 0；租约交接和结束均有 `release_all` 证据；
- [ ] 进程重启、未捕获异常、陈旧动作下发、本人误攻击、失控按键为 0；
- [ ] 采集持续 `≥30 FPS`、控制循环 `≥15 FPS`、读取失败率 `≤0.1%`；
- [ ] 动作延迟保持 P95 `<100ms`、P99 `<150ms`；
- [ ] 不出现连续 3 次 10 秒 stuck；自动恢复成功率 `≥99%`、恢复 P95 `≤5s`；
- [ ] 每次人工介入、暂停和等待均从有效自动运行时长中扣除；
- [ ] 每级均有 session video、Event Tape、receiver log、指标报告和人工介入记录；
- [ ] Sol-U `PASS`，产品/QA/现场/技术负责人签字。

### 必需证据

Canary Charter、3+3+3 个 session IDs、每会话 Bundle/commit/hash、视频、Event Tape、receiver log、输入所有权 trace、指标与人工介入报告、RCA、回退验证。

### 回退

任一停止条件触发即执行 G2-RBK-008。当前 Bundle 进入 `Quarantined`，回到最后一个 Replay/Shadow-valid Bundle分析；Legacy 恢复单写入。Canary 时长不会继承到新 Bundle。

---

## 11. G4 — 单图 Certified（5 × 4 小时）

### 目标

在 Pilot 单图完成最小产品竖切：采集、本人定位、怪物识别、认证静态路线、移动/攻击、HP/MP、状态监督、`release_all`、遥测和整体回退；形成首个 `Certified` Runtime Bundle。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G4-CRT-001 | Certified Gate Charter 与冻结 support statement | G3 PASS | Sol-U A/R |
| G4-RTE-002 | 100 圈路线/定位预验收；消除 route1 无限回归和身份切换 | G3 | Sol-U 阈值；Luna-M 实现/验证 |
| G4-CBT-003 | 攻击目标、补 HP/MP、技能/本人负样本和终态谓词闭环 | G3 | Sol-U 范围；Luna-M 实现 |
| G4-FLT-004 | 断流、断网、receiver、模型、画幅、stuck 的现场前故障演练 | G2/G3 | Sol-U 矩阵；Luna-M + QA 执行 |
| G4-FLD-005 | 5 次独立、每次有效自动运行 `≥4h` 的认证会话 | 预验收 PASS | Luna-M + 现场执行；Sol-U 审计 |
| G4-RLS-006 | Certified Bundle、上一 Bundle 回退、发布说明、限制和 Model Card | 5 sessions PASS | Sol-U A；Luna-M + 发布 R |

### 退出门禁

- [ ] 5 次会话来自至少 3 个独立启动/日期窗口；每次 **EAOH ≥4h**；
- [ ] EAOH 排除暂停、等待登录、人工输入、持续 stuck、采集失效和无限恢复时间；
- [ ] 5 次会话的进程/线程异常重启、未捕获异常、并发输入写入、陈旧动作、失控按键均为 0；
- [ ] 100 圈路线完成率 `≥99%`，身份切换为 0，进入 route1 无限回归为 0；
- [ ] 自动恢复成功率 `≥99%`、P95 `≤5s`，不出现连续 3 次 10 秒 stuck；
- [ ] 独立人工真值与现场审计中，本人/技能特效触发攻击意图为 0；
- [ ] 性能持续满足采集、控制和延迟门槛；
- [ ] 每次故障注入都进入明确 Paused/Faulted，`release_all ≤1.5s`；
- [ ] 需求—测试—Replay—现场 session—Bundle 覆盖 Pilot 竖切 100%；
- [ ] 原子回退到上一可用 Bundle 已演练；
- [ ] Sol-U `PASS`，产品/QA/现场/技术/发布负责人共同签字。

### 必需证据

100 圈报告、故障注入报告、5 个四小时 session 包、EAOH 计算明细、性能/错误预算、模型现场审计、Certified manifest、制品 hash、回退演练、发布说明和 Gate packet。

### 回退

优先原子切回上一 Canary/Certified Bundle；若 Core v2 执行层故障影响恢复，则执行 `release_all` 并切换到批准的 Legacy 紧急路径。发生 Sev-1/Sev-2 后撤销当前认证，修复版本从 G3-C0 重新晋级。

---

## 12. G5 — 原始功能闭环与受控扩展

### 目标

在不削弱 G4 竖切的前提下，逐项完成已确认的原始功能：登录/选角、组队、换频道、符文、死亡复活、断线恢复；再把认证方法扩展到批准的地图/Profile。Legacy 中的跨地图、自动循环生成等增强能力保持 feature flag 关闭，直到单独收益 Gate 批准。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G5-DAT-001 | 补齐并去标识化组队、符文、补药动态、断线/维护、频道失败素材 | G4 PASS | Sol-U 数据范围；Luna-M + 数据/QA R |
| G5-LOG-002 | 登录/大区/频道/选角 deadline workflow | DAT | Sol-U 状态 Gate；Luna-M 实现 |
| G5-PTY-003 | 组队创建/已创建/服务受限/退出状态机 | DAT | Sol-U Gate；Luna-M 实现 |
| G5-RUN-004 | 符文触发/接近/方向/成功/失败/冷却状态机 | DAT | Sol-U Gate；Luna-M 实现 |
| G5-REC-005 | 死亡复活、断线、返回登录、维护/频道满和人工确认恢复 | DAT | Sol-U Gate；Luna-M 实现 |
| G5-SCL-006 | 每个新增地图/Profile 的 registry、graph、route、model、movement profile、独立 Bundle | G4 | Sol-U 支持矩阵；Luna-M 实现 |
| G5-OPS-007 | 中文运维 UI/ViewModel，只消费类型化 telemetry；明确状态、倒计时、停止/回退 | 上述工作流 | Sol-U 范围；Luna-M 实现 |

### 单功能晋级规则

每个功能使用独立 flag 和 Bundle，严格经过：

```text
Fixture/Contract → Golden Replay → Shadow → bounded Canary → Certified
```

默认样本门槛为每个正常流程至少 30 次独立成功尝试、每类关键失败至少 3 次故障注入；Sol-U 可在 Gate Charter 中提高门槛，执行开始后保持已冻结阈值。

### 退出门禁

- [ ] `MEDIA_REVIEW.md` 中与首发功能相关的数据缺口全部闭环并去标识化；
- [ ] 所有 workflow 都有 deadline、显式失败态、人工接管点和唯一 ActionResult，不出现无限等待；
- [ ] 登录/选角/组队/频道/符文/死亡/断线各自完成 Replay、Shadow、Canary、Certified 证据链；
- [ ] 换频道后回到可控状态成功率 `≥99%`、P95 `≤60s`；超时进入明确 Faulted，不持续轮询；
- [ ] 每个新增地图/Profile 都有独立人工真值、100 圈预验收和 G4 等级的现场证据；Legacy 的 52 张地图资产不自动计入支持矩阵；
- [ ] 任一功能关闭后 G4 Pilot 主循环仍满足认证门槛；
- [ ] P2/P3 增强能力一次只允许一个进入研究性 Canary，且有业务收益指标和回退；
- [ ] Sol-U `PASS`，产品/QA/现场/技术/发布负责人签字。

### 必需证据

数据 manifest、隐私审计、每功能 contract/replay/shadow/canary/certification 报告、状态超时报告、支持矩阵、每地图/Profile Bundle、回退报告、中文 UI smoke、Gate packet。

### 回退

按功能 flag 关闭单一 workflow，并原子切回上一 Certified Bundle；保留 G4 Pilot 竖切。出现无限恢复、素材不足或收益不成立时，该功能进入 `Quarantined/Retired`，其余认证能力保持不变。

---

## 13. G6 — 可持续发布、支持矩阵与 Legacy 退役

### 目标

把 Core v2 从单个认证 Pilot 提升为可持续维护的产品：受保护远端、可复现构建、签名 Bundle、双机 clean install、自动回滚、SLO/事故流程、数据保留策略和明确支持矩阵；Legacy 从常态输入链退出。

### 工作包

| ID | 工作与输出 | 依赖 | 模型分工 |
|---|---|---|---|
| G6-RLS-001 | tag→build→SBOM→签名→artifact registry→发布说明的 release pipeline | G5 PASS | Sol-U 发布策略；Luna-M + 发布 R |
| G6-CLN-002 | 控制端与游戏端双机 clean install/upgrade/rollback 测试矩阵 | G6-RLS | Sol-U Gate；Luna-M + 发布/现场 R |
| G6-SLO-003 | EAOH、误攻击、stuck、恢复、延迟、断流、回退、数据质量 SLO 与错误预算 | G5 | Sol-U A/R；Luna-M telemetry 实现 |
| G6-INC-004 | 事故分级、证据保全、24h 时间线、Bundle 撤销和复盘模板 | G6-SLO | Sol-U A；Luna-M + QA/发布 R |
| G6-DAT-005 | 数据/模型/视频/日志分仓、访问控制、去标识化、保留与删除策略 | G5 | Sol-U 治理；Luna-M + 数据 R |
| G6-LEG-006 | Legacy 只读归档、输入入口撤除、应急回退观察期和最终退役清单 | 连续稳定发布 | Sol-U A；Luna-M 实施 |
| G6-SUP-007 | 支持矩阵及每行认证索引；新增行复用 G1～G5 证据链 | G5 | Sol-U A；Luna-M R |

### 退出门禁

- [ ] 从受保护 tag 可以生成字节可追溯的签名 Bundle、SBOM、测试/回放报告和发布说明；
- [ ] 控制端和游戏端均在独立 clean 环境完成 fresh install、upgrade、rollback；游戏端接收器满足无 Python 部署约束；
- [ ] 两个连续正式发布周期满足错误预算，期间 Sev-1/Sev-2 为 0；
- [ ] 任一发布可在批准时限内原子切回上一 Certified Bundle，回退演练有 L4/L5 证据；
- [ ] 支持矩阵的每一行都能追溯到独立 Bundle 和 G4/G5 认证证据；未列入矩阵的地图/Profile 标记为 unsupported/research；
- [ ] 监控指标自动来源于 Event Tape/session，不手工维护“稳定时长”或“测试数”；
- [ ] 数据、模型、日志和录像的访问/保留/去标识化审计通过；
- [ ] Legacy 常态输入入口已撤除，只读归档可查；观察期结束后不再把 Legacy 作为默认回退；
- [ ] Sol-U `PASS`，产品/QA/现场/技术/发布负责人共同签字。

### 必需证据

受保护 tag/remote/CI、签名与 SBOM、artifact registry、双机 clean reports、upgrade/rollback reports、两个发布周期 SLO、事故演练、支持矩阵证据索引、Legacy 退役审计、最终 Gate packet。

### 回退

优先回退到上一 Certified Bundle；发布基础设施异常时冻结新发布并保持当前 Certified 版本。Legacy 应急路径只在退役观察期内保留，使用时生成事故记录与恢复计划。

---

## 14. 全局指标、停止条件与回退阶梯

### 14.1 指标定义

| 指标 | 定义/门槛 |
|---|---|
| EAOH | 有效自动运行小时；排除暂停、登录等待、人工输入、持续 stuck、采集失效、无限恢复 |
| 需求—证据覆盖 | Pilot/G5 功能均为 100%，每条需求至少链接 contract/replay/field 中适用证据 |
| 采集 | 持续 `≥30 FPS`；读取失败率 `≤0.1%` |
| 控制 | 控制循环 `≥15 FPS`；动作 P95 `<100ms`、P99 `<150ms` |
| 失效保护 | 断流/断网/退出后 `release_all ≤1.5s`；陈旧动作下发为 0 |
| stuck | 不出现连续 3 次 10 秒 stuck；自动恢复成功率 `≥99%`、P95 `≤5s` |
| 模型 | 独立人工真值 ONNX P/R 各 `≥0.95`；本人误识别为 monster 为 0；PT/ONNX 差各 `≤1pp` |
| 路线 | 100 圈完成率 `≥99%`；身份切换 0；route1 无限回归 0 |
| 工程质量 | 远端 CI 必需检查全绿；覆盖率 `≥90%`；报告绑定 commit/Bundle |
| 发布质量 | 发布资产、配置、模型、数据 split、路线、receiver、报告全部有 SHA-256 |

### 14.2 立即停止条件

任一条件触发，当前动作进入终态并执行 `release_all`，现场会话停止，Bundle 转入 `Quarantined`：

- 两个输入写入者同时存在；
- 本人/其他玩家/技能特效触发高置信攻击意图；
- 旧 session/generation 或过期动作到达 receiver；
- 5 分钟内两次以上 stuck/rejoin 循环，或状态超过 deadline；
- 无新帧、画幅变化、心跳超过 1500 ms、模型/类别/输入尺寸/hash 失配；
- 未捕获异常、Event Tape 断链、Bundle 内单文件漂移；
- 样本、日志或报告出现未去标识的账号/角色标识；
- Gate 阈值、数据 split 或 Bundle 在执行中被改动。

### 14.3 统一回退阶梯

```text
新增功能 flag off
  → 上一 Certified Runtime Bundle
  → G4 Pilot Certified Bundle
  → Legacy 紧急路径（仅批准的观察期）
  → release_all + 安全停止 + 人工接管
```

---

## 15. 立即执行队列（按顺序）

以下是当前最短关键路径。Luna-M 每次只领取一个有界战术包；Sol-U 在指定节点审计。

1. **Sol-U / G-1-DEC-001**：形成 G-1 Gate packet，列出签字人和仍缺证据。
2. **Luna-M / G-1-PIL-002**：对 Pilot 配置漂移做只读解析，提交唯一有效配置决策包；不直接把 Legacy 配置复制为真值。
3. **Sol-U / G-1-OWN-004**：批准输入所有权澄清 ADR，固定 G3 为首次真实接管点。
4. **Luna-M / G0-SCM-004**：以本轮首个可审计 commit 为起点，配置 Core v2 远端与 PR/保护；远端坐标待绑定期间，先维护本地 commit 与远端设置清单，Gate 保持 `HOLD`。
5. **Luna-M / G0-EVD-010 + G0-CI-005**：加入 JUnit/evidence metadata/artifact index，并取得首个真实远端 CI run。
6. **Luna-M / G0-DEP-006**：锁定 Python/build 依赖，构建绑定 commit 的 wheel/sdist。
7. **Luna-M / G0-MAN-003**：基于真实 commit 与真实 Pilot hash 生成 Candidate Bundle；示例 fixture 继续只用于 schema test。
8. **Sol-U**：冻结最小 Golden fixture 的来源、隐私、truth 和 split；明确调试叠加视频只做诊断。
9. **Luna-M / G0-RPL-007**：实现 Replay runner，生成 3 次确定性报告。
10. **Luna-M / G0-SHD-008**：实现 dry-run Shadow，生成“真实输入调用数 0”的动态证据。
11. **Luna-M / G0-CLN-009**：在独立 Windows 环境执行 clean smoke 并归档报告。
12. **Sol-U**：审计完整 G0 packet，只有全部空项闭环后给出 G0 `PASS`。

---

## 16. 文档一致性与表述规则

1. `README.md`、ADR、`CONTRIBUTING.md` 与本路线图统一使用：**G0 工程基线建设 / Shadow 准备；Legacy 当前独占真实输入；G3 才是首次有界接管**。
2. `runtime-manifest.example.json` 始终称为 schema fixture；只有绑定真实资产 hash、真实 commit 和真实报告 ID 的 manifest 才称 Candidate Bundle。
3. “CI 已配置”仅描述 workflow 文件存在；“CI passed”需同时给出 remote、run ID、attempt、commit 和 artifact。
4. “Replay ready”需有固定 fixture、runner 和报告；只有 Event Tape 类型时，Golden Replay 状态仍为未开始。
5. “clean-machine passed”需给出独立环境身份、安装来源、命令和报告；当前开发机本地运行不使用该称谓。
6. “field passed / 4h passed”需使用 Core v2 session、EAOH 和固定 Bundle；Legacy 长日志、多个重启合并日志或文件跨度不计入。
7. “Certified”总是带范围，例如 `map_id/profile_id/release_id/capability`；旧路线 manifest 的认证范围保持为路线资产本身。
8. 任何状态数字由 CI/report/session 索引生成；Markdown 只引用，不手工累加。

---

## 17. 本路线图的完成定义

- [x] 覆盖 G-1、G0、G1、G2、G3、G4、G5、G6；
- [x] 每阶段包含目标、工作包、依赖、退出门禁、证据和回退；
- [x] 明确 Sol-U 负责战略/Gate，Luna-M 负责战术包；
- [x] 明确当前完成、证据待绑定和未开始项；
- [x] 明确 Golden Replay、Shadow、干净机、现场和 GitHub 远端当前均没有可用于晋级的完整证据；
- [x] Legacy/upstream GitHub 远端与 Core v2 远端状态分开表述；
- [x] 输入所有权从 Shadow 到 Canary/Certified 的切换点唯一；
- [x] 门禁阈值、证据等级、停止条件和回退阶梯可直接执行。
