# G0 Gate Charter：可复现工程与最小证据流水线

**Charter ID**：`G0-CHARTER-001`
**版本**：`1.2`
**签发日期**：2026-08-29
**证据快照**：2026-08-29 03:46（Asia/Shanghai）
**战略 / Gate Owner**：5.6Sol Ultra
**战术包 Owner**：5.6 Luna max
**当前 Gate 决定**：**HOLD — 工程证据包已远端复验，治理门禁尚未闭环**

## 1. Gate 目的与阶段边界

G0 证明一个 Core v2 **source commit** 能在受控 CI 与隔离 Windows 环境中重复完成静态质量、契约测试、实际 Candidate Manifest 校验、最小确定性 Replay、最小 Shadow 和 clean smoke，并由一个只含 packaging/evidence/docs 的 **sealed packet commit** 固化结果。

G0 只放行进入 G1 的完整感知、WorldState、Replay/Shadow 工作。它不证明模型已通过独立人工真值验收，不证明现场或 4 小时稳定性，也不授予 Core v2 真实输入权。G0～G2 仍由 Legacy 独占真实输入，Core v2 真实输入调用数必须为 0。

## 2. G-1 入口与受审对象

G-1 已封存以下战略边界：

- Core v2 唯一主线：ADR-001；
- G0～G2 Legacy 独占、G3 有界独占租约、双写为 0：ADR-004；
- Pilot：`map_id=100040004`、匿名 `profile_id=pilot-subject-01`、单类 640 Candidate、`attack=a`：DEC-001；
- 原始功能与 Gate/证据映射：`docs/REQUIREMENTS-TRACEABILITY.md`；
- G-1～G6 晋级路线：`docs/ROADMAP.md`。

本轮 G0 使用双 commit 语义，二者不得混写：

| 对象 | 不可变身份 | 作用 |
|---|---|---|
| Candidate source | [`7da29f4cfae0bd984b00c394b78e637088a7e452`](https://github.com/xphai/mxdauto/commit/7da29f4cfae0bd984b00c394b78e637088a7e452) | 代码、测试、工具和 Candidate 运行语义；Manifest、报告及 CI metadata 的 `source_commit` 均绑定它 |
| Sealed packet | [`04c794c59eb98af6e739415e1ecb72a335795bb9`](https://github.com/xphai/mxdauto/commit/04c794c59eb98af6e739415e1ecb72a335795bb9) | source 的 packaging/evidence 后继提交；固化 Bundle、clean artifacts、下载的远端 CI metadata 与报告；由 successor run 复验 |

`7da29f4...` 是 `04c794c...` 的祖先；二者之间仅变更 `bundles/` 与 `evidence/`。因此运行代码身份仍是 source commit，而 checkout/packet 身份由 sealed commit 单独记录。后续代码字节变化必须生成新的 source commit；后续证据字节变化必须生成新的 packet commit 和远端 run。

一次 G0 评审只接受以下同一谱系对象：

```text
source_commit / sealed_packet_commit
dependency_lock_sha256
build_artifact_sha256[]
release_id / runtime_manifest_sha256
golden_fixture_id / golden_fixture_sha256
test_report_id / replay_report_id / shadow_report_id / clean_report_id
ci_run / run_attempt / ci_evidence_sha256
rollback_target = legacy_owner
```

## 3. 当前事实快照

### 3.1 SCM、Bundle 与本地结果

| 项目 | 当前事实 | 判定 |
|---|---|---|
| Remote | [`xphai/mxdauto`](https://github.com/xphai/mxdauto)，repository ID `1349864993`，`main=04c794c5...` | 远端身份已建立 |
| Branch / PR | [main API](https://api.github.com/repos/xphai/mxdauto/branches/main) 返回 `protected=false`；[Pull Requests](https://github.com/xphai/mxdauto/pulls?q=is%3Apr) 当前为 0 | **阻断：未建立 protected main、required checks 和评审 PR** |
| Release | `candidate-core-v2-20260829-shadow`；lifecycle=`candidate`；execution_mode=`shadow` | 只用于 G0 离线证据 |
| Manifest | `runtime-manifest.json` SHA-256 `c3382e839c978d564ed3c48e9b29d70d86e678d07b2815d7864e5d5646682007` | 绑定 source commit 与实际资产 hash |
| Bundle / indexes | `bundle.json` `7f52f1e3838c17d6a87032669b52e32d2ac153a455bd563dad360c400e88767e`；asset index `d12d5aef62d29d8dcb8e8e5f0e55abc467a442a3c0703b13ffbd75a89b892d81`；evidence index `3edab63f9730015ef97650c982e704937026b09caca5a17842066f2ead606fe2` | committed packet 可遍历 |
| Dependency / build | lock `00bbe87dc673c8065603bd584c464638113dbf921f96220c154ce545816155fa`；wheel `6c8148f05d1cec96416fb00c0187f0623483b8e8f8dabc4e1a70877563fddab3`；sdist `fad8441aeac2e953d4cf96c3e383e64371a46d4b26833335af195302e4d9da08` | 本地、packet 和成功 CI 一致 |
| 本地质量 | 109 passed；coverage `94.61%`（1684/1780）；Ruff lint/format、Mypy 均通过 | L1 复现补充，不替代远端 run |
| Bundle verification | `--metadata-only --strict-g0` 与加载全部 configured external roots 的 `--strict-g0` 均通过 | metadata 图与 8 个外置资产字节 hash 均已核验 |

### 3.2 可绑定的远端 run

[`core-v2-ci run 33204844985`](https://github.com/xphai/mxdauto/actions/runs/33204844985)，attempt 1，event=`push`，head/`checkout_commit=4317c478d70422815162b7ca29d1e074fca188f0`，已于 2026-08-29 03:42:31（Asia/Shanghai）完成，workflow conclusion=`success`。该 run 的原始 `ci-evidence.json` 已按 SHA-256 下载到 sealed packet：

- Windows runner、Python `3.12.10`；27 个记录检查和最终 workflow result 均为 `passed`；
- JUnit：109 tests、0 failures、0 errors、0 skipped；coverage `94.61%`；
- `ci-evidence-push-33204844985-1` 的 `status=passed`、`source_commit=7da29f4...`、`checkout_commit=4317c478...`；payload SHA-256 为 `9828d92bc01166db3f7e3ee9775e3596b2e9258a77e3451b40b20a4b89ac9fd1`；
- artifact `g0-ci-evidence-33204844985`（ID `9699292429`）下载包 SHA-256 为 `186b73d641337c91a395889a21ef4cf6d556d2d840c1f499fab03c486add3d96`；另有 `quality-reports-33204844985`、`build-artifacts-33204844985`、`g0-clean-smoke-33204844985`；
- Replay fixture SHA-256 `22dd58eeaee16cb72eea529f177aad86747e162e7d9e7458a284a0dad4e6eb34`，3 次 deterministic，report digest `3ab52ec8767c846339e856f550aebfe044cd200a0fd17d1a404bd061b280ed3c`；
- Shadow report digest `1391db6fb7ad2ec37d418c5619c102c4191ac41de7bc3b2a0673a1e365411521`，Core v2 真实输入、键盘、鼠标、receiver、窗口和双写事件均为 0；
- cacheless Windows clean smoke 从 checkout-attested packet 创建新 venv、构建并安装 wheel，完成测试、Manifest、Replay、Shadow 与 rollback 检查，failed checks 为 0。

### 3.3 Sealed packet 的 successor 复验

[`core-v2-ci run 33205169227`](https://github.com/xphai/mxdauto/actions/runs/33205169227) 以最终 sealed packet `04c794c59...` 为 `head/checkout_commit`，于 2026-08-29 03:45:48（Asia/Shanghai）完成，workflow conclusion=`success`。其 27 个 checks 全部 `passed`，payload SHA-256 为 `a0c1fd23233050be72645aca39aec8414885d35814992c0bf1e147218dc659b1`；artifact `g0-ci-evidence-33205169227`（ID `9699387349`）下载包 SHA-256 为 `b7e2a63b4d329b2f6095e4cf6caf0029f0a6a99b91dda43e95df7986f728c55f`。该 successor run 证明“纳入远端 CI 证据后的最终 packet”仍通过同一质量门禁；为避免无限自引用，packet 绑定前一 run，successor run 作为外层封印。

### 3.4 被隔离的前序 run 与失败索引

[`run 33202897083`](https://github.com/xphai/mxdauto/actions/runs/33202897083) 的 workflow conclusion 虽为 `success`，其 `ci-evidence-push-33202897083-1` 明确为 `status=failed`，payload SHA-256 `b279e8d522d8e8e0c78798a7e4c2c1ca1c8de372830d9a114d7b23c1fbd249f5`。该运行只保留为失败回归证据，**不参与 G0 绑定**。失败来自 collector 两处语义错误：

1. `_parse_check_results` 把依赖安装结果字符串 `passed` 当作 parse error，生成失败的 `ci-step-outcomes`；
2. clean-smoke 报告被错误套用仅属于 Replay/Shadow 的 fixture SHA 与 canonical `report_digest` 要求。

修复已包含在 source commit `7da29f4...`：按 report kind 校验、正常化 GitHub step outcome、分离绑定 candidate source 与实际 checkout，并在 metadata 总状态不是 `passed` 时让 collector 以非零退出，形成 fail-closed 行为。后继 run `33204844985` 是修复后的首个可绑定远端事实。run `33201956865` 与 `33202897083` 的原始失败材料、artifact digest、根因和关闭谱系已登记在 `evidence/failures/failure-index.json`。

## 4. 战术包状态

| ID | 输出 | 当前状态 |
|---|---|---|
| G0-SCM-004 | source/packet commit、remote、protected main、PR required checks | source/packet/remote 已完成；**PR/protection 未完成** |
| G0-CI-005 | 远端 CI、JUnit、coverage、evidence metadata、稳定 artifact 名 | run `33204844985` 与四组 artifact 已完成 |
| G0-DEP-006 | dependency lock、wheel/sdist 与 hash | 已绑定并由成功 CI 复验 |
| G0-MAN-003 | DEC-001 Candidate Bundle/Manifest/hash | 已生成；严格 metadata 与 full-external 校验通过 |
| G0-RPL-007 | 去标识 Golden fixture、3 次相同 digest | 最小 synthetic fixture smoke 已完成；不替代 G1 完整 corpus |
| G0-SHD-008 | dry-run Shadow、计划/实际 diff、真实输入调用数 0 | 最小离线 Shadow 已完成；不构成现场结论 |
| G0-CLN-009 | 隔离 Windows install/test/manifest/replay/shadow | 本地 cacheless 与 GitHub Windows runner 均通过 |
| G0-EVD-010 | evidence index、报告元数据、retention/hash | 成功谱系已绑定；失败 run 已纳入统一 failure index |

## 5. 强制门禁审计

### G0-A：SCM 与远端身份

- [x] `source_commit` 与 `sealed_packet_commit` 均为 40 位真实 commit，谱系和职责已记录；
- [x] Core v2 remote URL、repository ID、branch 与独立 Legacy/upstream origin 明确区分；
- [ ] `main` 受保护，PR 与本 Charter 的 required checks 已启用；
- [ ] 评审 packet 记录实际 PR 与 required-check 配置；当前 PR 为 0。

### G0-B：CI 与静态质量

- [x] 同一远端 run/attempt 完成依赖锁、Ruff lint/format、Mypy、Manifest、strict Candidate metadata、Pytest、clean smoke、Replay、Shadow 和 build；
- [x] 所有必需步骤、fail-closed collector 与 workflow result 均通过；
- [x] JUnit 为 109/0/0/0，coverage `94.61% ≥ 90%`；
- [x] JUnit、coverage、build、clean 与 evidence metadata 均上传，artifact/payload hash 可查；
- [x] run 记录 runner OS、Python、依赖结果、开始/结束时间与 `source_commit`/`checkout_commit` 双绑定；
- [x] 本地结果仅作为复现补充，不替代 run `33204844985`。

### G0-C：实际 Candidate Bundle

- [x] Manifest 使用真实 source/upstream commit 与 Profile/model/classes/map/route/receiver hash；
- [x] `profile_id=pilot-subject-01`，无真实账号或角色标识；
- [x] 模型/类别/输入尺寸符合 DEC-001；
- [x] Bundle 使用 repository-relative path 或命名 external root，不把 `F:\mxd` 写成运行契约；
- [x] schema、逐文件本地 hash、strict evidence graph 和全部 configured external asset bytes 均验证通过；
- [x] example manifest 仍只作为 schema fixture；Candidate lifecycle 未提升为 Certified。

### G0-D：最小 Golden Replay

- [x] fixture 具有唯一 ID、SHA-256、synthetic source、geometry、时间范围、用途/许可、truth/split 和去标识记录；
- [x] runner 使用固定 Candidate Manifest，连续 3 次事件序列与 digest 相同；
- [x] report 绑定 source commit、release、fixture、Manifest hash、环境和命令；
- [x] 该结果明确限于最小 synthetic engineering smoke，不冒充 G1 完整感知/录像 corpus。

### G0-E：最小 Shadow 与输入所有权

- [x] Core v2 只产生计划与模拟结果；Legacy observed action 使用独立 provenance；
- [x] Core v2 真实 InputSink/键鼠/receiver/窗口调用均为 0；Legacy 仍为 owner，双写为 0；
- [x] diff count 为 2、unclassified diff 为 0，并记录输入边界 receipts；
- [x] 报告只声明离线 Shadow，不使用现场成功类措辞。

### G0-F：隔离 Windows clean smoke

- [x] GitHub Windows runner 从 checkout-attested packet 开始，并使用无项目 venv、无 pip cache 的隔离环境；
- [x] 构建并安装 wheel，验证导入来自临时 venv 而非 repository；
- [x] 完成测试、coverage、Manifest、Replay、Shadow 与 rollback 检查；
- [x] 报告记录 OS/Python、checkout/source、命令、耗时、exit code 和 artifact hash；
- [x] 此结论只属于控制端工程 smoke，不等价于 G2 游戏端 receiver clean-host 认证。

### G0-G：证据、失败保留、回退与签字

- [x] 成功谱系可从 release 追到 source/packet、run、Manifest、报告和 artifact hash；
- [x] 失败 run `33201956865`、`33202897083` 的原始材料、payload/artifact hash、隔离理由与关闭谱系已提交到统一 failure index；
- [x] 回退检查已验证停止 Core v2、sink 断开、真实输入/双写为 0、Legacy owner 不变；
- [x] 实施侧已下载 run `33204844985` 与 successor run `33205169227` 原始 artifact，并逐一核对 GitHub archive digest、payload hash、27 个 checks 与 source/checkout 绑定；
- [ ] 仓库 Owner 通过受保护 PR 合并完成发布 countersign；
- [ ] Sol-U 在 PR/protection 与 Owner countersign 闭环后给出 `PASS`。

## 6. Gate 指标

| 指标 | 门槛 | 当前 |
|---|---:|---:|
| 远端必需检查 | 100% 通过 | 100%（run `33204844985`） |
| Pytest failure/error | 0 | 0 / 0 |
| 覆盖率 | `≥90%` | `94.61%` |
| Manifest/schema/strict hash error | 0 | 0 |
| Replay 重复次数 / digest 差异 | 3 / 0 | 3 / 0 |
| Shadow 真实输入 / 双写 | 0 / 0 | 0 / 0 |
| clean smoke 必需步骤失败 | 0 | 0 |
| 未绑定工程 artifact | 0 | 0（当前成功 packet） |
| Core v2 现场 session | G0 不要求 | 0 |

## 7. 当前差距与 Gate 决定

| Open finding | 当前事实 | 关闭条件 |
|---|---|---|
| `G0-OPEN-SCM-001` | main `protected=false`，PR=0 | 启用 branch protection/required checks，并以实际 PR 完成评审链 |
| `G0-CLOSED-EVD-002` | `evidence/failures/failure-index.json` 已绑定两次失败、原始材料、artifact digest、根因和修复谱系 | 已关闭；后续失败继续追加，不覆盖历史 |
| `G0-OPEN-SIG-003` | 原始 artifact 机器复核已完成；Owner 合并与 Sol-U 最终签字待记录 | 受保护 PR 合并形成 Owner countersign，随后固化 Gate decision |
| `G0-OPEN-DOC-004` | 本次状态收口文档在工作树中 | 纳入后续受控 docs/packet commit；在 commit 前不声称已远端绑定 |

**评审决定：`HOLD`。** run `33204844985` 已被 sealed packet 绑定，successor run `33205169227` 又复验了该 packet；失败索引与原始 artifact 复核也已闭环。当前只剩 G0-A 的 PR/branch protection 与 G0-G 的 Owner/Sol-U countersign。不得以 CI 绿灯覆盖治理缺口，G1 及以后保持未开始。

## 8. 回退计划

```text
停止 Replay/Shadow runner
→ 终止 dry-run session并断开 sink
→ 保留 Event Tape、成功与失败报告
→ 必要时标记 Candidate Quarantined
→ 验证 Core v2 真实输入调用数和双写仍为 0
→ Legacy 继续保持唯一输入 owner
```

只有 `decision=PASS` 且全部强制项闭环后，ROADMAP 才更新为 G0 Passed / G1 In Progress。当前 Charter 明确保持 G0 `HOLD`。
