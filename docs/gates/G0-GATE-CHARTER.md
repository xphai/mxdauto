# G0 Gate Charter：可复现工程与最小证据流水线

**Charter ID**：`G0-CHARTER-001`  
**版本**：`1.0`  
**签发日期**：2026-08-29  
**战略 / Gate Owner**：5.6Sol Ultra  
**战术包 Owner**：5.6 Luna max  
**当前 Gate 决定**：**HOLD — G0 进行中，尚未授予 PASS**

## 1. Gate 目的

G0 证明一个 Core v2 commit 能在受控 CI 与独立 Windows 环境中重复完成静态质量、契约测试、真实 Candidate Manifest 校验、最小确定性 Replay、最小 Shadow 和 clean smoke，并把全部结果绑定到同一证据谱系。

G0 只放行进入 G1 的完整 Replay/Shadow 工作。它不证明模型已现场认证，不证明 4 小时稳定性，也不授予 Core v2 真实输入权。

## 2. 入口状态

### 2.1 G-1 战略封存

以下战略决定已形成：

- Core v2 唯一主线：ADR-001；
- G0～G2 Legacy 独占、G3 有界独占租约、双写为 0：ADR-004；
- Pilot：`map_id=100040004`、匿名 `profile_id=pilot-subject-01`、单类 640 Candidate、`attack=a`：DEC-001；
- 原始功能与 Gate/证据映射：`docs/REQUIREMENTS-TRACEABILITY.md`；
- G-1～G6 晋级路线：`docs/ROADMAP.md`。

G-1 战略结论允许 Luna-M 执行 G0 战术包。上述新增文档进入下一受控 commit 后，形成其 SCM 绑定；该绑定属于 G0 的证据工作，G0 当前结论仍为 `HOLD`。

### 2.2 签发时观察到的事实

| 项目 | 观察值 | 证据判定 |
|---|---|---|
| 本地基线 commit | `c81011d2f047bc0cf3aec258f5662416f039838c` | 已存在的本地 SCM 起点 |
| 当前评审 commit | `TBD` | 必须包含本 Charter、ADR-004、DEC-001、追踪矩阵及后续 G0 实现 |
| Core v2 remote | `origin=https://github.com/xphai/mxdauto.git`；`origin/main=c81011d...` | 远端身份与首个 push 已建立；GitHub API 显示 main `protected=false` |
| CI workflow/run | `.github/workflows/ci.yml`；run `33194720588`，attempt 1，commit `c81011d...`，conclusion=`success` | L2 范围证据；artifact 只有 `coverage-xml`，没有 JUnit/evidence metadata |
| 本地契约测试 | 最近一次工作会话记录 `58 passed` | L1 开发反馈；缺少 commit/run/JUnit 绑定 |
| 本地覆盖率 | `coverage.xml line-rate=0.9468` | L1；高于 90% 门槛但尚非远端 Gate 证据 |
| Manifest | schema、example fixture、validator 已存在 | 实际 Candidate Bundle 为空 |
| Event Tape | hash chain/严格 JSON/顺序校验已实现 | 记录契约存在；Replay runner/report 为空 |
| Golden Replay | 当前工作树出现 fixture/runner 战术草案；已审 commit、3 次 digest 结果与 report 均为空 | 阻断 |
| Shadow | 当前工作树出现战术实现草案；已审 commit、dry-run 调用审计与 report 均为空 | 阻断 |
| clean machine | 独立 Windows report 为空 | 阻断 |
| Core v2 field | session 为 0 | G0 不要求现场，但该事实禁止现场类表述 |

## 3. Gate 范围

### 3.1 包含

1. Git/remote/PR/branch protection 与可定位 commit；
2. Ruff、Mypy、Manifest、Pytest/coverage 的远端 CI；
3. JUnit、coverage、构建制品、evidence metadata 的 artifact 绑定；
4. 使用真实 commit 与真实资产 hash 的 Candidate Runtime Bundle；
5. 最小去标识 Golden fixture 与确定性 Replay；
6. 最小 Shadow/dry-run，Core v2 真实输入调用数为 0；
7. 独立 Windows clean install/test/replay/shadow smoke；
8. 依赖锁、制品 hash、evidence index 与回退说明。

### 3.2 排除

- Core v2 真实键鼠/receiver 接管；
- 现场 Canary、4 小时稳定性或 Certified 结论；
- 模型在独立人工真值集上的最终 G1 晋级；
- 登录、组队、换频道、符文、死亡/断线等 G5 workflow；
- 跨地图、动态路线或其他 P2/P3 增强能力。

## 4. 受审对象

一次 G0 评审只接受一个不可变对象集合：

```text
candidate_commit
dependency_lock_sha256
build_artifact_sha256[]
release_id
runtime_manifest_sha256
golden_fixture_id / golden_fixture_sha256
test_report_id
replay_report_id
shadow_report_id
clean_report_id
rollback_target = legacy_owner
```

任一代码、依赖、模型、classes、阈值、地图、路线、Profile、receiver 或 fixture 字节变化都产生新的 candidate commit/release/evidence ID，并重新执行受影响检查。

## 5. 战术包与依赖

| ID | 输出 | 前置 | 负责人 | 当前状态 |
|---|---|---|---|---|
| G0-SCM-004 | 受控 commit、Core v2 remote、protected main、PR required checks | G-1 seal | Luna-M + 发布 | remote/基线 push 已有；本轮 commit、PR/protection 待补 |
| G0-CI-005 | 远端 CI、JUnit、coverage、evidence metadata、稳定 artifact 名 | SCM | Luna-M + QA | run `33194720588` 成功且有 coverage；其余 artifacts 待补 |
| G0-DEP-006 | dependency lock、wheel/sdist 与 hash | SCM | Luna-M + 发布 | 工作树有 lock 草案；review/CI/制品 hash 待补 |
| G0-MAN-003 | 使用 DEC-001 实际值的 Candidate Bundle/Manifest/hash | SCM、DEP、DEC-001 | Luna-M | schema/tool 已有；Candidate 待补 |
| G0-RPL-007 | 去标识 Golden fixture、Replay runner、3 次相同 digest | MAN、Event Tape | Luna-M；Sol-U 冻结样本 | 工作树有 fixture/runner 草案；结果、报告、commit 待补 |
| G0-SHD-008 | dry-run Shadow、计划/实际 diff、真实输入调用数 0 | RPL、ADR-004 | Luna-M + QA | 工作树有 runner 草案；diff/调用审计、报告、commit 待补 |
| G0-CLN-009 | 独立 Windows clean install/test/manifest/replay/shadow report | CI、DEP、MAN、RPL、SHD | Luna-M + 发布 | 待补 |
| G0-EVD-010 | evidence index、报告元数据、retention/hash 验证 | CI | Luna-M + QA | 工作树有 schema 草案；实际 index/报告、retention/CI 绑定待补 |

## 6. 强制门禁

### G0-A：SCM 与远端身份

- [ ] `candidate_commit` 为 40 位真实 commit，工作树清洁；
- [x] Core v2 remote URL 可查，且与独立上游克隆的 GitHub origin 明确区分；
- [ ] `main` 受保护，PR 与本 Charter 的 required checks 已启用；
- [ ] Gate packet 记录 remote、repository ID、branch、commit 和评审 PR。

### G0-B：CI 与静态质量

- [ ] 同一远端 CI run/attempt 完成 Ruff lint、Ruff format、Mypy、Manifest 和 Pytest；
- [ ] 所有必需步骤退出码为 0；
- [ ] 覆盖率 `≥90%`；
- [ ] JUnit、coverage XML、check summary 和 evidence metadata 均上传，artifact hash 可查；
- [ ] CI run 记录 runner OS、Python 版本、依赖安装结果、开始/结束时间；
- [ ] 本地输出只作复现补充，不替代远端 run。

### G0-C：实际 Candidate Bundle

- [ ] `runtime-manifest.json` 使用真实 `source_commit`、`upstream_commit`、Profile/model/classes/map/route/receiver hash；
- [ ] `profile_id=pilot-subject-01`，不含真实账号或角色标识；
- [ ] 模型/类别/输入尺寸符合 DEC-001：`best_forest_v3-candidate`、`[mob]`、`640×640`；
- [ ] Bundle 内使用相对路径，不依赖 `F:\mxd` 绝对路径；
- [ ] validator 校验 manifest schema 和每个实际资产字节；
- [ ] `runtime-manifest.example.json` 继续只作为 schema fixture；
- [ ] Candidate status 不提升为 Replay-valid/Shadow/Certified，直至对应证据完成。

### G0-D：最小 Golden Replay

- [ ] fixture 具有唯一 ID、SHA-256、来源会话、geometry、时间范围、许可/用途和去标识记录；
- [ ] 原始调试叠加视频只进入 diagnostic 集，不作为独立 raw-model truth；
- [ ] runner 使用固定 Bundle、注入 clock/randomness，并记录 Event Tape；
- [ ] 同一 fixture + Bundle 连续 3 次输出相同事件序列与最终 digest；
- [ ] `replay_report_id` 绑定 candidate commit、release、fixture、环境和命令；
- [ ] 任何差异保留报告并将 Candidate 标记 `Quarantined`。

### G0-E：最小 Shadow 与输入所有权

- [ ] Core v2 只产生 `WorldState/ActionSpec` 与模拟 `ActionResult`；
- [ ] Legacy 实际动作和 Core v2 计划动作使用不同事件字段；
- [ ] Core v2 真实 `InputSink`、键盘、鼠标、receiver 和游戏窗口调用数均为 0；
- [ ] Legacy 保持唯一 owner；双写事件为 0；
- [ ] `shadow_report_id` 记录 diff taxonomy、未分类差异和输入调用审计；
- [ ] Shadow 不使用现场成功类措辞。

### G0-F：独立 Windows clean smoke

- [ ] 环境为新 VM/主机或等价清洁快照，未复用项目 venv、pip cache、build/dist 或 `F:\mxd` 外置资产；
- [ ] 从受审 remote/tag/commit 或签名 source artifact 开始；
- [ ] 完成依赖安装、wheel 安装、静态检查、测试、Manifest、Replay 与 Shadow smoke；
- [ ] 报告记录 OS build、Python、命令、耗时、exit code、artifact/hash；
- [ ] 所有资源都从受审 checkout/Bundle 解析；
- [ ] clean report 不等价于游戏端 receiver clean-host 认证，后者属于 G2。

### G0-G：证据、回退与签字

- [ ] evidence index 可从 `release_id` 追到 commit、run、reports 和全部 artifact hash；
- [ ] 失败报告得到保留，证据索引不只登记成功项；
- [ ] G0 回退已验证：停止 Core v2 Replay/Shadow runner，Legacy 输入所有权保持不变；
- [ ] Luna-M 提交战术完成报告；
- [ ] QA 与发布负责人确认原始证据；
- [ ] Sol-U 审计全部强制项并给出 `PASS`。

## 7. Gate 指标

| 指标 | G0 门槛 |
|---|---:|
| 远端必需检查 | 100% 通过 |
| Pytest failure | 0 |
| 覆盖率 | `≥90%` |
| Manifest schema/hash error | 0 |
| Replay 重复次数 | 3 |
| Replay digest 差异 | 0 |
| Shadow Core v2 真实输入调用 | 0 |
| Shadow 双写事件 | 0 |
| clean smoke 必需步骤失败 | 0 |
| 未绑定 commit/Bundle 的 Gate artifact | 0 |
| Core v2 现场 session | G0 不要求；当前为 0 |

## 8. 阻断与失效条件

任一条件出现时，Gate 保持 `HOLD` 或转为 `QUARANTINE`：

- Core v2 remote/branch protection/required checks 或绑定候选 commit 的完整远端 run 缺失；
- 仅有本地聊天输出、截图、缓存或 Markdown 数字；
- example manifest 被当成实际 Candidate；
- fixture 缺少 hash/来源/去标识记录；
- Replay digest 非确定；
- Shadow 到达真实输入边界；
- clean smoke 读取开发机绝对路径或缓存；
- 模型/classes/input size/按键与 DEC-001 漂移；
- 工作树或 artifact 与 candidate commit 不一致；
- 任一强制检查被跳过或阈值在运行后调整。

## 9. 当前差距与 Gate 决定

| 差距 | 当前状态 | 关闭条件 |
|---|---|---|
| remote/PR/protection | remote 已有；PR/protection 缺失 | 提供 main protection、required checks 与 PR 证据 |
| 远端 CI/JUnit | 首个 run/coverage 已有；JUnit/evidence metadata 缺失 | 新 candidate commit 的 green run + 完整 artifacts |
| dependency lock/可追溯制品 | 工作树有 lock 草案；已审绑定与制品仍缺 | lock commit、CI 安装结果与 wheel/sdist hash |
| 实际 Candidate Bundle | 缺失 | 真实 manifest + assets + hash report |
| Golden Replay | fixture/runner 草案存在；Gate 结果缺失 | 已审固定 fixture/runner + 3 次相同 digest + report |
| Shadow | runner 草案存在；Gate 结果缺失 | 已审 diff report + Core v2 input calls 0 |
| clean machine | 缺失 | 独立 Windows report |
| evidence index | schema 草案存在；实际 index 缺失 | 可遍历索引、稳定 IDs 与 artifact hash |

**签发决定：`HOLD`。** 当前本地契约、Event Tape、schema、validator、58 个本地测试、94.68% 本地覆盖率、Core v2 remote 和首个成功 CI run 说明 G0 已有良好工程起点；它们覆盖不了上表的 branch protection、完整 CI artifacts、Bundle、Replay、Shadow 和 clean 缺口。

## 10. 回退计划

G0 只运行无真实副作用的工具链。回退步骤为：

```text
停止 Replay/Shadow runner
→ 终止 dry-run session
→ 保留 Event Tape 与失败报告
→ 标记 Candidate Quarantined
→ 验证 Core v2 真实输入调用数仍为 0
→ Legacy 继续保持唯一输入 owner
```

## 11. 评审输出

最终 Gate packet 至少包含：

```text
gate_id = G0
decision = PASS | HOLD | QUARANTINE | ROLLBACK
candidate_commit
remote / pr / ci_run / run_attempt
release_id / runtime_manifest_sha256
test_report_id / replay_report_id / shadow_report_id / clean_report_id
artifact_sha256[]
open_findings[]
rollback_result
Sol-U decision
QA / release countersign
```

只有 `decision=PASS` 且全部强制项闭环后，ROADMAP 才更新为 G0 Passed / G1 In Progress。当前 Charter 明确保持 G0 `HOLD`。
