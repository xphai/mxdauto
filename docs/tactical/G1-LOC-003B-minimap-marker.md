# 战术包：G1-LOC-003B 小地图本人标记提取与离线回放

## 1. 元数据

| 字段 | 内容 |
|---|---|
| package_id | `G1-LOC-003B` |
| title | B2 accepted FramePacket/CAS 上的 fail-closed minimap yellow marker 提取与三次离线回放 |
| parent | `G1-LOC-003A`、`G1-FRM-001B2` |
| stage / type | `G1 / localization extraction + fixed-corpus replay` |
| owner / reviewer | `5.6 Luna max / 5.6Sol Ultra` |
| branch | `feat/g1-loc-003b-minimap-marker-20260830` |
| replay source | `58f20f314733b2c1791665f37fc0fe7c80e009a7` |
| created_at | `2026-08-30` |
| status | `Completed (marker extraction + offline 3-run)`；完整 `G1-LOC-003` 仍为 `In Progress` |
| evidence level | 固定 B2 输入上的本地离线 replay-valid；远端合并/CI 另行绑定 |
| input boundary | `input_owner=legacy`；Core v2 real input=0；double-write=0 |

## 2. 已完成范围

1. **冻结标记配置**：固定 `1920×1080` BGR8、ROI `[309,238,97,113]`、最大帧龄
   `250 ms`、匿名 subject 与校准摘要。配置语义摘要为
   `47936cf77e46ebc62fd3d6dae241237307ebb370fd81a197745486812c58f22a`。
2. **fail-closed 提取器**：只读取调用方提供的 B2 accepted pixel；校验画幅、像素规格、
   session/source/frame/timestamp/generation、calibration、pixel digest 与 CAS lineage。配置、像素或
   时序不一致时返回明确拒绝/故障，不猜测坐标。
3. **固定亮黄核心规则**：同时使用 HSV/BGR 色域、连通域面积与尺寸边界以及固定 bright-core
   阈值；候选按确定性顺序选择并生成 canonical digest。
4. **回放与验证器**：回放报告绑定 extractor 源文件、冻结配置、B2 corpus manifest、Event Tape
   index、accepted ledger、calibration、zero-input audit、样本顺序、generation、时间策略与 source
   commit；验证器重算语义摘要并拒绝路径、哈希、谱系或三次结果漂移。
5. **只读命令入口**：命令要求精确 HEAD、干净 tracked tree、外部私有 CAS root 与全部预期摘要；
   PixelStore 通过只读包装访问，报告在全部验证通过后才原子写入。

## 3. 真实离线结果

固定输入是已封存的 B2 300-sample corpus；本次不复制 raw pixels 或外部资产入仓。

| 指标 | 结果 |
|---|---:|
| sample_count | 300 |
| repeat_count | 3 |
| detected | 194 |
| no_marker | 6 |
| rejected | 100 |
| fault | 0 |
| 三次 run digest | `479bce453813472bab54ae110e9014bba0103f6fb97f6f0337dd2cfc7146f66e` |
| deterministic / execution_valid | `true / true` |
| status | `PASS` |

报告：`evidence/g1-loc-003b/g1-loc-003b-marker-replay-20260830.json`

- report semantic digest：`9528f117200bfcb24d3723a081e83e4889f273322c798fef6fd62cfc14a361ff`
- report artifact SHA-256：`37076a1937fa10ce317c4899a43470dfcce9dd7c155f6a0efa8ef089f0efc4d5`
- extractor artifact SHA-256：`508b309fce0988a2b0c1e7f4b2ab13a4702a969be5f0175950cb9f779c18a651`
- evidence index：`evidence/g1-loc-003b/g1-loc-003b-evidence-index.json`

## 4. 上游输入绑定

| Artifact | SHA-256 |
|---|---|
| G1 Frame Candidate packet | `4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73` |
| corpus manifest raw artifact | `11bcb481e3c683a44ce41e4dcef9ee98ad3172eea8f434c30f1b4d36e0464b91` |
| Event Tape index raw artifact | `244620029379430ac43a4b7a1cc2da03888011937abbf4c62e9b275609d6d8cc` |
| accepted ledger raw artifact | `5628357d9d1889c76bc9c8c1f938ba331df56ca8f83f0a693453cf347ea3b269` |
| calibration raw artifact | `379962db0397326bd021a1a0c361952dcad75ba9e23324d8926aa560ded3dff0` |
| zero-input audit raw artifact | `284a3371742de5ce185f6ca5197be6a252d70819b7ba1c1ed3bd26cfc2ff8ed9` |

accepted ledger 中的 `source_provenance_id` / pixel artifact 描述原始采集 CAS occurrence；corpus
truth 中同名字段描述后续 import CAS occurrence。验证器分别校验两层来源，不把二者错误地要求为同一摘要。

## 5. 验证范围

| 验证 | 结果 |
|---|---|
| marker/localizer 专项 | 31 passed；Ruff、mypy 通过 |
| replay/CLI 专项 | 21 passed；Ruff、mypy 通过 |
| 实际 B2 300-sample × 3 replay | PASS；三次 run digest 完全一致 |
| report schema / canonical JSON+LF | PASS |
| zero input | Legacy owner；Core v2 real input=0；double-write=0 |

按快速开发设置，本包不重复执行与改动无关的全量测试、coverage 或构建；合并前以精确 PR commit 的
required CI 作为完整质量门禁。

## 6. 结论边界

本报告的 `truth_scope` 固定为 `frame_ingestion_only`。它证明：在固定 B2 输入、冻结配置与精确代码
版本下，提取契约的 3-run 输出可重复，且失败分支保持 fail-closed。

它不证明：

- marker 坐标准确率、召回率或人工真值指标；
- affine/world/platform 定位质量；
- 100 圈、身份切换 0 或完整 `G1-LOC-003`；
- 当前 VC-003 实时输入上的定位表现；
- Planner、Action、receiver 或任何真实输入接管。

因此本包只关闭 `G1-LOC-003B marker extraction + offline 3-run`，并允许进入下一项
**VC-003 只读定位验证**；完整 LOC Gate 继续保持 `In Progress`。

## 7. 回滚与后续

- 回滚：移除/停用 marker consumer，保留 `G1-LOC-003A` 纯 resolver 与 B2 FrameSource；不触碰
  Legacy 输入所有权、InputSink、receiver、键盘、鼠标或窗口写入。
- 下一步：在 VC-003 只读入口运行独立 LOC session，冻结 session/source/config/commit，记录
  candidate/no-marker/reject/fault 与时序；随后加入人工 marker truth，才评估准确率与完整 LOC Gate。
- 任一画幅、ROI、校准、来源、generation、时间、CAS 或配置漂移均停止晋级并保留失败证据。

## 8. 完成定义

- [x] 冻结匿名 marker 配置与真实 extractor artifact；
- [x] B2 accepted FramePacket/CAS lineage、时序、身份和配置 fail-closed；
- [x] 实际 300-sample corpus 连续 3-run 确定性通过；
- [x] portable report、schema 与 evidence index 已生成；
- [x] 保持 Legacy owner / Core v2 real input=0 / double-write=0；
- [x] 明确无 marker accuracy、实机 LOC、100 圈或完整 LOC Gate 主张；
- [ ] 精确 PR commit 的 required CI 与 protected-main 合并绑定；
- [ ] VC-003 只读定位 session、人工 marker truth 与完整 `G1-LOC-003`。
