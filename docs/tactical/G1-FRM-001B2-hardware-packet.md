# 战术包：G1-FRM-001B2 VC-003 硬件证据与 Candidate packet

| 字段 | 值 |
|---|---|
| package_id | `G1-FRM-001B2` |
| parent | `G1-FRM-001B` / `G1-FRM-001` |
| requirement | `REQ-CAP-001`、`REQ-UI-001`、`REQ-SAFE-002`、`REQ-OBS-002`、`REQ-PRI-001`、`REQ-REL-001`、`REQ-REL-002` |
| gate | G1 FrameSource 子门禁 |
| decision | 技术证据、组织会签、会签版 protected PR 与 outer CI 全部 PASS |
| implementation | 5.6 Luna max + QA/evidence + 现场负责人 |
| status | `Completed / Technical + Organizational Countersign + Outer CI PASS` |
| baseline | protected `main@37e57b9662fa3d061e840d4b9c86ab89efe24f2f` |
| required wheel | `maple_automation_core-0.1.0-py3-none-any.whl` / SHA-256 `62b3b2f362a60087dffadb1d5529c4d7a27440adf61a28d30b685c7cda3b273f` |
| baseline CI | main run [`33256230132`](https://github.com/xphai/mxdauto/actions/runs/33256230132) / attempt 1 / `success` |
| ADR | ADR-002、ADR-004、ADR-007、ADR-010、ADR-011、ADR-012 |
| unlocks | `G1-FRM-001=Completed`；`G1-OBS-002=Unlocked`（implementation not started） |

完整 FrameSource 审计框架见
[`docs/gates/G1-FRM-001-GATE-CHARTER.md`](../gates/G1-FRM-001-GATE-CHARTER.md)；六类真实组织会签
已统一登记于 [GitHub Issue #13](https://github.com/xphai/mxdauto/issues/13)，并以 reviewer
`owner-xphai` 的六个独立 `approved` 条目写入 Candidate。会签版 evidence-only PR #15、required
`quality` 与 protected merge 已完成。

## 1. 目标

本包在真实 `VC-003 Video` 上运行 B1 的精确软件产物，形成一次有范围、可审计、read-only 的
300 秒硬件证据；同时封存 frame-ingestion corpus/truth、source/pixel/Event Tape provenance、
privacy audit 和新的 G1 Frame Candidate packet。

本包回答的问题仅包括：

- 指定主机在指定 300 秒窗口内能否以 Core adapter 连续读取 VC-003；
- host-side measured capture/admission 是否达到冻结门槛；
- Core-owned raw slot 是否保持 capacity=1 与精确 accounting；
- accepted FramePacket 是否能追到 canonical pixel、来源、session 和 Event Tape；
- 整个现场过程是否保持 Core input=0、Legacy input owner 不变。

## 2. 结论边界与非目标

- 当前 PnP/FFmpeg 枚举 `VC-003 Video` 只作为 preflight 事实，不算 hardware PASS；
- 本包不声称 sensor exposure timestamp、glass-to-glass latency、采集卡/驱动/USB queue depth；
- 本包不证明四小时稳定性、多主机/多设备普适性、OBS/model/WorldState、动作控制或现场接管；
- 本包不运行 Core receiver，不发送键鼠或窗口输入；Legacy 始终是唯一真实输入 owner；
- 本包保持 B1 source、capture config、schemas、tools、dependency lock 冻结，并保持 G0 sealed
  Bundle/evidence 字节不变；
- 新 packet 的生命周期是 `candidate`/`shadow`，不是 Certified，也不产生完整 G1 PASS。

## 3. 开跑前置条件

现场负责人和 QA 必须在报告中确认以下绑定均存在且 hash 匹配：

1. `G1-FRM-001B1=Completed`，protected merge 与 main post-merge CI 成功；
2. B1 source commit `S`、CI run ID/attempt、CI metadata status、wheel 名称和 SHA-256；
3. clean Python 3.12 venv 从该 wheel 与精确 dependency lock 安装；
4. effective capture config、ADR-011 calibration、Pixel V1、adapter/backend/tool hashes；
5. logical source=`capture-card-primary`，selector=`VC-003 Video`，设备实例 ID 转换为公开匿名
   fingerprint；原始硬件 ID 仅留受控映射；
6. requested format=`1920×1080@30`，backend 已显式选择，fallback list 为空；
7. Core capture package 的 static import/call audit 通过；receiver、keyboard、mouse、window writer
   均未连接；
8. 私有 CAS、临时 retention、公开 evidence 输出目录及磁盘余量已检查；
9. 系统 monotonic clock 可用，report wall clock 只用于审计时间，不参与 freshness/rate 计算；
10. 现场 witness、QA reviewer、privacy reviewer 使用匿名 reviewer ID 登记。

设备在 preflight 时缺席、被其他进程占用或磁盘/现场窗口不合格，记录 `HOLD/NOT-RUN`，不启动
计量窗口。设备已确认可用后 adapter open 失败属于执行失败，不转换为 preflight HOLD。

## 4. 现场执行协议

### 4.1 启动与预热

1. 保存 PnP/DirectShow 枚举、匿名设备 fingerprint、backend/version、requested properties；
2. 启动一个全新的 backend instance、`session_id` 和 raw counter epoch；
3. 读取并记录 negotiated width/height/FourCC/FPS/backend；任何静默 fallback 或属性漂移立即
   终止当前 session 并形成 FAIL report；
4. 预热 30 秒，预热数据单独计数，不进入 300 秒门槛；
5. 预热完成后以一次 host monotonic snapshot 记录 measurement start marker 与 counter baseline，
   保持同一 capture session、同一 counter epoch，不清零 counters，也不重开设备。

### 4.2 300 秒计量窗口

计量窗口必须连续覆盖至少 `300.000s`，开始/结束均使用 host monotonic clock。producer 连续
retrieve 并 publish 到 raw latest slot；adapter consumer 以目标处理速率 take/admit。每个成功
produced frame 写入 hash ledger；每个 accepted FramePacket 在发出前完成 Pixel CAS put/verify。
`successful_frames` 只统计 retrieval timestamp 位于 `[measurement_start, measurement_end)` 的
produced ledger entries；`admitted_frames` 只统计 observation timestamp 位于同一半开区间的 accepted
entries。warmup 与 stop 后 final drain 均排除在 rate/count 门槛之外。

运行中采集：

- produced/delivered/superseded/pending/discarded、raw high-watermark；
- source sequence、host post-retrieve timestamp、adapter observation time、host age；
- backend no-frame poll/timeout、相邻成功 retrieval gap 与最长 source silence；
- requested/negotiated properties 与 property drift；
- read/decode/copy/hash/CAS/admission 状态与首次 fatal root cause；
- FramePacket、PixelArtifact、EventRecord cross-link；
- process/thread/child 状态与 input-call spies；
- CAS bytes、ledger/event/report bytes 与 retention/privacy class。

运行中禁止 reconnect、backend fallback、counter reset、session replacement 和设备 reopen。后台无新帧
或错误进入 ADR-011 fatal latch，报告保留已完成窗口，不继续拼接第二 session。

### 4.3 Stop 与 final drain

计量到期后先请求 producer stop，等待 backend read 退出，再由 controller 在同一
绝对 stop deadline 内 drain raw slot 的最后 pending sample，且不重绑 logical consumer。
报告必须证明 final delivered sequence 等于 last produced sequence，随后关闭 resolver/backend 并确认：

```text
capture_thread_alive = false
backend_child_alive = false
pending = 0
stop_elapsed_seconds <= 2.0
```

若平台 backend 的正常关闭合同另有更小上限，使用更严格值；超过 2 秒即为 cleanup failure。

## 5. 五分钟硬性验收

### 5.1 身份、格式与时长

| 指标 | 通过条件 |
|---|---|
| measured window | `measured_seconds >= 300.000`；无拼接、暂停、reset/reopen |
| logical/device identity | `capture-card-primary` + 预检匿名 fingerprint，全程不变 |
| requested format | `1920×1080@30`，原样记录 |
| negotiated format | width/height/FourCC/FPS/backend 均有实际值；width=1920、height=1080 |
| canonical pixels | 每个成功 frame 为 BGR8/HWC/stride 5760/length 6,220,800 |
| timestamp | `host_monotonic_post_retrieve`；clock domain 全程一致 |
| upstream queue | `unknown`；报告不含 vendor/driver capacity 结论 |

### 5.2 吞吐、新鲜度与质量

| 指标 | 通过条件 |
|---|---|
| successful capture | `successful_frames >= 8970` |
| measured capture rate | `successful_frames / measured_seconds >= 29.9 FPS`；不以 nominal property 代替实测 |
| admitted frames | `admitted_frames >= 4470` |
| processing/admission rate | `admitted_frames / measured_seconds >= 14.9 FPS` |
| freshness | 每个 accepted frame `0 <= age_ns <= 250,000,000` |
| source continuity | `max_inter_frame_gap_ns <= 250,000,000`；no-frame poll/timeout 单独计数 |
| source health | read/decode/copy/hash failure=0；fatal=0；reconnect/fallback=0 |
| admission health | stale、duplicate/out-of-order sequence、timestamp rollback/future、source/session/clock/size/geometry mismatch 均为 0 |

全部 rate 使用未舍入的整数计数与纳秒窗口计算，展示值可以格式化，但 PASS 判定使用原始分数。
requested FPS 固定为 `30.0`；DirectShow property 允许 `±0.001` 的表示误差，但 measured throughput
不使用该属性替代。`29.97` property 不计作 `30.0`。29.9/14.9 门槛为 300 秒半开窗口保留不足
一个调度周期的余量，不允许通过四舍五入越线。五分钟窗口采用 source failure=0，严格于路线图长期门槛
`read failure <=0.1%`。

### 5.3 Raw latest、CAS、Event Tape 与 input audit

Candidate full-root verifier 只接受 `verification_profile=b2_gate`的 provenance audit；
`b1_fixture`、metadata-only 或 3-frame synthetic PASS 均不得升级。`b2_gate`
必须重算 private CAS，并硬性证明 300 samples/300 unique pixels、3 independent
sessions、六类 category 覆盖、wrong-size negative 与至少 20% 独立复核；其中同一
hardware smoke `live_session` 至少贡献 100 个 samples。
硬件 source provenance、frame ledger 与 smoke report 由 Candidate 的受限 external
roots 另行重算与交叉绑定。full verifier 还必须接收显式 private CAS root、truth root 和全部
Event Tape 路径，调用 provenance semantic verifier 重新打开实际 bytes；缺少任一受控根时不得
形成 full PASS。

| 指标 | 通过条件 |
|---|---|
| raw slot | `max_depth=1`；`pending∈{0,1}`；至少产生一帧 |
| accounting | 完整 session epoch 满足 `produced=delivered+superseded+pending+in_flight+discarded_on_reset+discarded_on_error`；计量窗口内 reset=0、discarded=0 |
| sequence | delivered 单调唯一；允许 producer 覆盖造成 gap；final drain=last produced |
| torn sample | sequence/spec/digest/bytes mismatch=0 |
| pixels | accepted FramePacket 的 CAS resolve/recompute mismatch=0 |
| provenance | source/frame/pixel/truth/Event Tape orphan=0、mismatch=0 |
| Event Tape | sequence/hash chain valid；额外顶层键=0 |
| cleanup | thread/child residual=0；stop `<=2.0s` |
| input | owner=`legacy`；real input、receiver connect、window write、double write 全为 0 |

同一 pixel digest 在不同递增 frame ID 重复出现属于合法静止画面，报告只把重复 sequence 计为
duplicate。

## 6. 真实 frame-ingestion corpus/truth

B2 必须封存至少 300 个唯一 Pixel V1 raw samples，覆盖至少 3 个独立 source sessions：

- 新 300 秒 Core VC-003 session 至少 100 个 samples；
- 其余来源可使用已审核的历史 VC capture 或用户提供视频；每个 session 至少 50 个 samples；
- 包含 static、motion、transition、dark/low-contrast、crop-edge；
- 加入真实 wrong-size/geometry negative，例如 1280×720 source，negative 与 positive 分开标记；
- 以完整 source session 分配 split，任何 session 只进入一个 split，跨 split Pixel digest overlap=0；
- 100% 样本完成主审，至少 20% 由独立 reviewer 复核；分歧形成 adjudication record 后再封存；
- truth 只描述 source/pixel/geometry/admission/derivation/privacy/license，不含 detector/model/action
  标签。

当前 B1 verifier 对 `disputed_then_adjudicated` 采取 fail-closed：仅有 `adjudication_id` 不构成
已裁决证据，`b2_gate` 会拒绝该 truth。B2 开跑前若实际出现分歧，须先补齐 adjudication record
的 schema、受控相对路径、SHA-256 与 semantic verifier，并由 corpus/Candidate full verifier 打开
artifact 重算后方可封存；无分歧且独立 reviewer 确认的 corpus 不受此阻塞项影响。

已盘点的候选 source 只在通过 rights/privacy review 后进入 corpus；manifest 使用 logical ID 与受控
locator，公开材料不暴露主机目录：

| logical source | 已核 SHA-256 | 允许用途与限制 |
|---|---|---|
| `legacy-vc003-capture-sample-20260821` (`capture_sample.avi`) | `b3c3a5059651e342360f6cf3cf4ca835c4734cb1a3398d9f00e147c4285c9770` | 1920×1080 MJPEG pixel/geometry seed；不作 hardware timing truth |
| `legacy-vc003-capture-metadata-20260821` (`capture_metadata.json`) | `3b4602a3f545a0ea003cdcf9f927f4dd8a47a2d804026d80023df574e8dedc32` | 采集声明 provenance；与 AVI container duration 的差异必须保留 |
| `user-video-training-ground-i` | `e72d6e77711c8e3977d83a677eab30f859fdefa1700eb50f0efc73b8f9361075` | 1920×1080/30 source seed；需完成 license/privacy review |
| `user-video-wrong-size-1280x720` | `663c90623903436c657b4a633e9d29e19ff529734d641886993e57d826aa1229` | 真实 wrong-size negative；不得转码后冒充 baseline positive |

历史 `capture_sample.avi` 的 container duration/FPS 与采集元数据存在差异，因此只作 pixel/geometry
source；其 PTS 是 extraction locator，不进入 capture-rate truth。每个来源先完成权利、隐私和去标识
审核。raw originals 与身份映射进入 restricted/private storage；公开 packet 仅纳入 hash、去标识
derivative 和 `privacy_audit=PASS`。

真实硬件运行本身不要求多次产生相同 digest。完成抽帧和 manifest 封存后，对固定 corpus 使用 B1
reader/verifier 连续运行 3 次，要求 sample order、admission expectation、Event Tape 与 canonical
report digest 完全一致。

## 7. 必需证据集

| 证据 | 关键绑定 |
|---|---|
| `capture-source-provenance` | source S/wheel/deps/config/calibration、匿名设备、requested/negotiated、timestamp/upstream buffer |
| `vc003-hardware-smoke-report` | 300 秒 window、counts/rates、failures、slot、freshness、cleanup、input、limitations |
| frame hash ledger | 每个 produced/accepted frame 的 session/sequence/timestamp/Pixel digest/Event Tape locator |
| restricted Pixel CAS index | retained corpus objects、encoded/uncompressed hashes、privacy/retention |
| `frame-corpus-manifest` + truths | 3 sessions/300 samples、split、review/adjudication、derivation/license/privacy |
| deterministic corpus replay | 3 次相同 digest，绑定冻结 manifest |
| Event Tape | accepted/suppressed/fatal 事件链及 hash-chain audit |
| `frame-provenance-audit-report` | source→pixel→truth/corpus→FramePacket→Event Tape→hardware→packet 全链重算 |
| zero-input audit | static + runtime spies；owner=legacy；所有 Core write/connect counters=0 |
| privacy audit | private/public artifact 清单、去标识规则、公开扫描结果 |
| CI/build evidence | B1 protected source/main run/wheel SHA 与 B2 packet outer verification |

报告必须保留原始整数计数、monotonic nanoseconds、失败数组和 limitations；只上传汇总截图不构成
Gate evidence。

## 8. G1 Frame Candidate packet

### 8.1 Packet 内容

新 packet 至少绑定：

```text
packet_id / schema_version / lifecycle=candidate|shadow
scope=G1-FRM / overall_g1_state=In Progress
B1 source commit S / CI run / wheel sha256 / dependency lock sha256
immutable G0 baseline source+packet reference
effective capture config / calibration / Pixel V1 contract
source provenance / hardware smoke / frame ledger
corpus manifest / truth set / deterministic replay
Event Tape / provenance audit / privacy audit / zero-input audit
artifact IDs + sha256 + size + privacy class
failure index / limitations / reviewer signoffs
input_owner=legacy / real_input_call_count=0
```

packet verifier 必须重算所有本地 artifact hashes、schema/semantic invariants、source/wheel/config
binding 和 cross-links。restricted artifacts 使用受控 external locator + hash + access class；verifier 在
具备私有 root 时执行 full verification，在公开 CI 执行 metadata/privacy-safe verification。

### 8.2 Source/packet 双身份

- `S` 是运行硬件 smoke 的 B1 protected source commit；wheel 由 `S` 的 main CI 生成；
- hardware/corpus reports 全部显式绑定 `S + wheel_sha256`；
- `P` 是仅加入 evidence/docs/packet 的 descendant packaging commit；
- packet 本体绑定 `S` 与输入 artifacts，不嵌入包含自身的 `P` hash；
- outer main CI 在 `P` 上运行 strict G1-frame verification，并在 Gate record 中登记 `P`、run ID、
  attempt、artifact hashes 与 metadata status；
- `P` 中出现 source/config/schema/tool/dependency 变化时，本次 B2 证据失效并回到 B1。

现有 G0 source `7da29f4cfae0bd984b00c394b78e637088a7e452`、sealed packet
`04c794c59eb98af6e739415e1ecb72a335795bb9`、Bundle、reports 和 failure index 保持字节不变。
G1 verifier 与 `--strict-g0` 分轨运行。

## 9. 状态与失败语义

| 状态 | 触发条件 | 后续动作 |
|---|---|---|
| `HOLD/NOT-RUN` | preflight 设备缺席/占用、现场窗口或磁盘不合格 | 保留 preflight；B1 Completed；B2 Queued/Hold |
| `FAIL` | 可用设备进入 open/run 后，open、300 秒门槛、counter、freshness、cleanup 或 zero-input 任一失败 | 保留原始 report/ledger；不封 packet |
| `QUARANTINE` | hash/source/wheel/config 谱系不一致、CAS corruption、Event Tape 断链、公开 PII | 隔离全部相关 artifacts，完成根因与数据处置 |
| `SUPERSEDED` | hardware run 后 source/config/schema/tool/dependency 有变化 | 新 B1 commit/wheel，重新执行 B1 CI 与完整 B2 |
| `PASS` | hardware、corpus、provenance、privacy、zero-input、packet verifier 和会签全部成功 | 提交完整 `G1-FRM-001` Gate 审计 |

以下表述在 verifier/评审中按 FAIL 处理：静默 fallback/reconnect；用 container/header FPS 替代
host measured rate；把 post-retrieve timestamp 称为 device/sensor timestamp；把 Core raw slot
capacity=1 外推到 DirectShow/driver/vendor；把设备枚举或历史日志写成五分钟 hardware PASS。

## 10. 会签与完成定义

`G1-FRM-001B2=Completed` 需要：

1. B1 source、main CI wheel、dependency/config/calibration 全部绑定且无漂移；
2. 300 秒 hardware smoke 所有硬门槛通过；
3. 3-session/300-sample frame-ingestion corpus/truth、3-run deterministic replay 通过；
4. provenance/Event Tape/CAS/privacy/zero-input audit 均通过；
5. G1 Frame Candidate packet strict local/full 与 public metadata verification 通过；
6. QA/evidence、技术、现场、privacy/release 和 Sol-U 完成 countersign；
7. protected packaging PR 合并，main outer run 的 `P`、run ID/attempt、metadata status 和 artifact
   hashes 回填。

B2 完成后，`G1-FRM-001B` 才可完成；随后单独评审 `G1-FRM-001=Completed` 并解锁
`G1-OBS-002`。G1 的 OBS/LOC/WST/Planner/Replay/Shadow 等后续工作仍未完成，因此整体 G1
继续保持 `In Progress`，Core v2 真实输入继续为 0。

## 11. 实际执行与组织会签收口记录

### 11.1 冻结身份

- B1 source：`37e57b9662fa3d061e840d4b9c86ab89efe24f2f`；
- baseline main CI：[`33256230132`](https://github.com/xphai/mxdauto/actions/runs/33256230132)，attempt 1，`success`；
- exact wheel：131,432 bytes，SHA-256 `62b3b2f362a60087dffadb1d5529c4d7a27440adf61a28d30b685c7cda3b273f`；
- dependency lock：SHA-256 `1aa30d122b50bb938545bcfc2f50e4d3ba789c473c30e3b6806a73cad38957a9`。

### 11.2 Hardware PASS

`vc003-live-20260829T140846Z` 在同一 session/counter epoch 上完成 30 秒 warmup + 300.000 秒连续
measurement：

| 指标 | 实际结果 | 判定 |
|---|---:|---|
| successful frames / rate | 8,999 / 29.996666 FPS | PASS |
| admitted frames / rate | 4,499 / 14.996666 FPS | PASS |
| max accepted age | 110 ms | PASS |
| max inter-frame gap | 110 ms | PASS |
| raw latest | depth=1；produced=9,897；delivered=4,950；superseded=4,947；pending=0；final drain=last produced | PASS |
| cleanup | 0 residual threads/children；stop=0.094 s | PASS |
| failures/input | 所有 failure counters=0；Core real input/receiver/window/double-write=0；owner=legacy | PASS |

最终 hardware report digest 为 `99cebe1aabe46b185fb7667861702fa07eb0e082018ecfb910e8a13a0a3432b2`。

### 11.3 Corpus、Replay 与审计

- corpus digest：`e36863e24ea95295e8e6e9858283ab34706e463b6f675a6e7c856fa51b1e616b`；manifest SHA-256：`11bcb481e3c683a44ce41e4dcef9ee98ad3172eea8f434c30f1b4d36e0464b91`；
- 4 independent sessions / 300 samples / 300 unique pixels / 6 categories / 100 wrong-size negatives；live session 贡献 100 samples；
- primary review=300，independent review=60（20%）；organizational human countersign=`approved`，reviewer=`owner-xphai`；
- full CAS verified objects=300；4 Event Tapes / 300 events；orphan/mismatch/missing=0；
- 3 次 deterministic replay 的 run digest 均为 `7bbf5758615f9456a88e93e8802c0e973f67bf05e3b72eb7a680e2b393ab9133`；
- `b2_gate` provenance、privacy 与 zero-input audits 全部 PASS；raw videos、Pixel CAS 与 review contact sheet 保持 repository 外的 restricted storage。

### 11.4 Candidate packet

- packet：`evidence/g1-frame-candidate-20260829/g1-frame-candidate-packet.json`；
- packet digest：`4e21973f66fd5c4480c1417d1509a0e21069551d728bf02607319008cbf74f73`；
- privacy report digest：`fcecc2aa5c1879fd32aad9f9274da0685d1f57e7e4358fff2254d0a6d5ab7141`；privacy artifact SHA-256：`1cd232f10406a9357cce8d7be1416c5f26bace81d141fbbc5f0354306b295f25`；
- lifecycle=`candidate`，overall G1 state=`In Progress`；六项 signoff 均为 `approved`（`qa_evidence`、`technical`、`field`、`privacy`、`release`、`sol_u`）；
- metadata-only verification PASS；在显式 hardware/corpus roots、private CAS、truth root 和 4 条 Event Tape
  下执行 full-root verification PASS；
- 仓库 packet 共 318 个 metadata/hash-only/受限 artifacts（含 packet），不包含 6,220,800-byte raw Pixel 对象或原始视频。

### 11.5 Outer CI fail-closed 回归

PR #11 首个 outer run
[`33257717820`](https://github.com/xphai/mxdauto/actions/runs/33257717820) 在 Candidate conditional
step fail-closed：该 step 位于项目安装前，verifier 导入 `maple_automation_core` 时缺少 checkout
`src` bootstrap。失败 run 保留，不参与晋级；workflow 已在该 step 显式绑定 checkout `src`，并新增
顺序回归测试，后续成功 run 才可作为 outer seal。

### 11.6 组织会签与最终关闭记录

本节证明技术、组织与 SCM/CI outer seal 已闭环：

- packaging PR：[#11](https://github.com/xphai/mxdauto/pull/11)；PR run
  [`33258100541`](https://github.com/xphai/mxdauto/actions/runs/33258100541) `success`；
- packaging commit `P`：`72c3ad081db33d083fdcd5a5e0f62e73f886c233`；
- outer main run：[`33258468278`](https://github.com/xphai/mxdauto/actions/runs/33258468278)，attempt 1，
  `success`；conditional Candidate metadata verifier 实际执行并通过。
- 组织会签：[Issue #13](https://github.com/xphai/mxdauto/issues/13)，reviewer=`owner-xphai`；
  `qa_evidence`、`technical`、`field`、`privacy`、`release`、`sol_u` 全部 `approved`；
- 会签版 evidence-only PR：[#15](https://github.com/xphai/mxdauto/pull/15)；head
  `67b9848077b381a514d7504a91eab05a22baffb7`；PR run
  [`33283195258`](https://github.com/xphai/mxdauto/actions/runs/33283195258) `success`；
- protected squash merge：`fe29a4ce5a8a98c49c85382f083d8429bfee2c38`；合并后 main outer run
  [`33283646596`](https://github.com/xphai/mxdauto/actions/runs/33283646596)，attempt 1，`success`；
  `ci-evidence` artifact digest `sha256:9e51d97d858e7432fe85be36fdaeefe7859dd2f4dc5f36ac6e81513d6885fb1c`。

最终状态：`G1-FRM-001B2=Completed`、`G1-FRM-001=Completed`、`G1-OBS-002=Unlocked`。
整体 G1 仍为 `In Progress`；Candidate 仍为 `candidate`，`input_owner=legacy`，Core v2 真实输入与
double-write 均为 0。OBS/LOC/WST/Planner/Replay/Shadow 等后续工作不因本包完成而自动通过。
