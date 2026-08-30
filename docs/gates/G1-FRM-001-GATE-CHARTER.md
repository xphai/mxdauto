# G1-FRM-001 Gate Charter：FrameSource 完整审计

**Charter ID**：`G1-FRM-CHARTER-001`

**状态日期**：2026-08-30（Asia/Shanghai）

**当前决定**：`HOLD / Technical + Outer CI PASS / Organizational Countersign Pending`

**会签记录**：[GitHub Issue #13](https://github.com/xphai/mxdauto/issues/13)

## 1. 目的与结论边界

本 Charter 汇总 `G1-FRM-001A`、`G1-FRM-001B1` 与 `G1-FRM-001B2` 的不可变证据，
用于单独裁决完整 `G1-FRM-001`。当前技术证据、protected packaging PR 与 main outer CI
均已通过；六类组织会签尚未登记，因此本 Charter 保持 `HOLD`。

最终 `PASS` 只允许产生以下状态变化：

- `G1-FRM-001B2=Completed`；
- `G1-FRM-001=Completed`；
- `G1-OBS-002=Unlocked`。

它不产生完整 G1 PASS，不认证 detector/model、WorldState、Planner 或输入执行，不授予 Core v2
真实输入权。`input_owner=legacy`、Core v2 real input calls `=0` 与 double-write `=0` 必须保持。

## 2. 冻结身份

| 对象 | 冻结事实 | 当前审计结果 |
|---|---|---|
| G0 baseline | source `7da29f4cfae0bd984b00c394b78e637088a7e452`；sealed packet `04c794c59eb98af6e739415e1ecb72a335795bb9` | 不重写，strict G0 verifier 独立保持有效 |
| G1-FRM-001A | feature `7cca4154a38e8bca29b917aa3c5abcc43a51391d`；merge `b30ddedb1f05945e68fb348b221cdfa123e83c59`；PR #3 | `Completed` |
| G1-FRM-001B1 | protected source `37e57b9662fa3d061e840d4b9c86ab89efe24f2f`；main run `33256230132` | `Completed` |
| B1 wheel | `maple_automation_core-0.1.0-py3-none-any.whl`；SHA-256 `62b3b2f362a60087dffadb1d5529c4d7a27440adf61a28d30b685c7cda3b273f` | hash 已绑定 |
| B2 Candidate | `evidence/g1-frame-candidate-20260829/g1-frame-candidate-packet.json`；digest `a23b60094330cdd57b81cb0426017b2bb318e6e07dd4ef7b1e5d08ffcdcb1ea1` | metadata/full-root 技术验证通过；会签前版本 |
| B2 packaging | PR #11；commit `72c3ad081db33d083fdcd5a5e0f62e73f886c233` | protected merge 完成 |
| B2 outer seal | main run `33258468278`，attempt 1，`success` | Candidate conditional verifier 实际执行并通过 |
| 当前 main | `8510595f5b1513b446b198073905461bbaef022e`；run `33259524250`，`quality=success` | 文档 outer seal 后主线全绿 |

`37e57b9..72c3ad0` 的 B2 packaging 不修改 `src/`、`configs/`、`schemas/`、`tools/`、
dependency lock 或 B1 wheel；`72c3ad0..8510595` 仅记录 outer CI 文档事实。组织会签若只更新
evidence/docs，不触发 B1 source、300 秒 hardware 或 300-sample corpus 重跑。

## 3. 已通过的技术证据

| Gate 项 | 绑定证据 | 结果 |
|---|---|---|
| Frame admission | 3 runs / 15 scenarios / 32 events；main digest `1c4948afc636ffba45b1f4a769ec7ee3d6d5ea15f09b2b1f9596faa43f837a7d` | PASS |
| VC-003 hardware | 300.000 秒；8,999 successful / 4,499 admitted；29.996666 / 14.996666 FPS | PASS |
| freshness/gap | max accepted age `110 ms`；max inter-frame gap `110 ms` | PASS |
| raw latest | depth `1`；produced `9,897`；delivered `4,950`；superseded `4,947`；pending `0` | PASS |
| cleanup | stop `0.094 s`；residual thread/child `0`；final drain=last produced | PASS |
| corpus/truth | 4 independent sessions；300 samples；300 unique pixels；6 categories；100 wrong-size negatives | PASS |
| independent review | 60/300 samples（20%） | 技术复核完成；不替代组织会签 |
| deterministic replay | 3 次 run digest 均为 `7bbf5758615f9456a88e93e8802c0e973f67bf05e3b72eb7a680e2b393ab9133` | PASS |
| CAS/Event Tape | 300 CAS objects；4 tapes / 300 events；orphan/mismatch/missing `0` | PASS |
| provenance/privacy | source→pixel→truth/corpus→FramePacket→Event Tape→hardware→packet 可追溯；公开扫描通过 | Technical PASS |
| zero input | owner=legacy；Core receiver/window/keyboard/mouse/double-write counters 均为 `0` | PASS |
| Candidate verifier | metadata-only 与受控 full-root | PASS |
| protected CI | PR #11 与 main run `33258468278` | PASS |

## 4. 组织会签门禁

完成状态必须由 Issue #13 中的真实审阅记录证明，并在 Candidate packet 中逐项登记以下精确角色：

| role | 必须确认的范围 | 当前状态 |
|---|---|---|
| `qa_evidence` | 报告、hash、重复性、失败保留与证据完整性 | `pending` |
| `technical` | source/wheel/config/schema/tool 绑定及实现契约 | `pending` |
| `field` | VC-003 现场窗口、设备身份、300 秒运行与 cleanup | `pending` |
| `privacy` | restricted/public 边界、去标识与公开扫描 | `pending` |
| `release` | protected PR、outer run、rollback 与发布边界 | `pending` |
| `sol_u` | Gate 范围、晋级条件与输入所有权 | `pending` |

允许一名实际负责人兼任多个角色，但 Issue 必须明确声明兼任，并为每个角色留下独立条目。
每个条目必须包含稳定的去标识 `reviewer_id`、上述精确 `role` 和最终 `approved` 或
`rejected`。空数组、未知角色、重复角色、`pending` 或仅有自动化 reviewer 均不构成完成。

## 5. 完成检查表

- [x] `G1-FRM-001A=Completed`，其 source、PR、main CI 与 frame-admission evidence 已绑定；
- [x] `G1-FRM-001B1=Completed`，protected source、CI wheel 与 dependency/config 契约已绑定；
- [x] 真实 VC-003 300 秒 smoke 的 rate、freshness、accounting、final drain 与 cleanup 通过；
- [x] 4-session/300-sample corpus/truth、独立技术复核与 3-run deterministic replay 通过；
- [x] Pixel CAS、Event Tape、provenance、privacy、zero-input 与 Candidate strict verifier 通过；
- [x] B2 packaging PR #11 与 main outer run `33258468278` 成功；
- [ ] Issue #13 中六个精确角色全部留下真实 `approved` 决定；
- [ ] privacy report 与 Candidate packet 回填会签，并重算 artifact hash 与 `packet_digest`；
- [ ] 会签版 packet 再次通过 metadata-only 与受控 full-root verification；
- [ ] 会签版 evidence-only PR 通过 required `quality` 并 protected merge；
- [ ] 新 main outer run 成功，commit/run/attempt/artifact hash 回填；
- [ ] Owner 与 Sol-U 在本 Charter 上记录最终 `PASS`。

任一未勾选项存在时，`G1-FRM-001B2` 与完整 `G1-FRM-001` 均保持 `In Progress`。

## 6. 会签版 evidence-only 变更边界

真实会签到达后，第一份 protected PR 只允许更新：

1. `evidence/g1-frame-candidate-20260829/privacy-audit-report.json`：登记组织会签状态并重算报告 digest；
2. `evidence/g1-frame-candidate-20260829/g1-frame-candidate-packet.json`：同步 privacy artifact
   hash/size、填入六角色 signoffs、更新准确 limitation 并重算 `packet_digest`。

不得修改 B1 `src/`、capture config、schema、verifier、dependency lock、wheel、hardware report、
corpus/truth 或私有 Pixel CAS。验证范围为一次 metadata-only 和一次受控 full-root；required
GitHub `quality` 承担完整远端门禁。

main outer run 成功后，第二份 docs-only seal PR 才更新本 Charter、Roadmap、Traceability 与 B2
战术包，将 B2 和完整 FrameSource 状态置为 `Completed`。

## 7. 失败、漂移与回退

- 任一角色 `rejected`：保持 `HOLD`，记录原因与处置，不修改完成状态；
- source/config/schema/tool/dependency/wheel 漂移：当前 B2 evidence 标为 `SUPERSEDED`，从新 B1
  source/main CI wheel 与完整 B2 hardware/corpus 重新开始；
- artifact hash、CAS、Event Tape 或 provenance 不一致：`QUARANTINE`，保留失败证据；
- 会签版 PR 或 outer CI 失败：保留失败 run，修复后重新走 protected PR；
- 回退始终为停止 Core v2 read-only runner、保持 Legacy 为唯一输入 owner；不触碰游戏端控制状态。

## 8. 当前决定与下一步

当前决定为 `HOLD`，唯一外部依赖是 Issue #13 的真实六角色会签。会签、evidence-only PR、
会签版 outer run 与最终审计全部完成后，才允许记录：

```text
G1-FRM-001B2 = Completed
G1-FRM-001   = Completed
G1-OBS-002   = Unlocked
overall G1   = In Progress
input_owner  = legacy
Core v2 real input calls = 0
```
