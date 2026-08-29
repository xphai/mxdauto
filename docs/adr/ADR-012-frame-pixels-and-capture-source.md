# ADR-012：Pixel V1、raw latest producer 与采集来源证据契约

**状态**：接受（Accepted）

**日期**：2026-08-29

**战略负责人**：5.6Sol Ultra

**实施负责人**：5.6 Luna max

**对应工作包**：`G1-FRM-001B1`、`G1-FRM-001B2`

## 1. 背景

ADR-011 已冻结 `RawFrame → FrameSourceAdapter → FramePacket` 的 admission、freshness、
sequence、geometry 与故障锁存，但 `G1-FRM-001A` 的单槽只位于 admission 后。当前
`RawFrame.content_hash` 和 `image_ref` 仍是调用方提供的文本，Core 尚未拥有并重算 pixel
bytes，也尚未证明 VC-003 producer 在消费落后时持续 drain 并只保留一个未读 raw sample。

Legacy 捕获实现具有连续 producer 与单个 `_packet` 覆盖槽，可作为迁移语义输入；其 FFmpeg、
DirectShow 或 OpenCV 内部缓冲、自动重连、backend fallback 和读取后赋时逻辑不属于 Core v2
证据。设备枚举只证明某时刻系统可见 `VC-003 Video`，不证明设备已成功打开、协商格式、产帧、
持续吞吐、延迟或五分钟稳定性。

本 ADR 在保持 G0 sealed packet 字节不变的前提下，冻结 G1 FrameSource 的 pixels、raw producer、时间、
来源和硬件证据边界。

## 2. 决策与管线边界

G1 FrameSource 使用以下两级、彼此独立的 latest 语义：

```text
read-only backend
  → owned immutable RawCaptureSample
  → Core-owned raw latest slot（capacity=1，take/consume）
  → FrameSourceAdapter admission
  → Pixel CAS put/verify
  → accepted FramePacket | explicit suppressed event
  → admitted latest FramePacket slot（ADR-011，capacity=1）
```

raw slot 解决 backend producer 与 adapter consumer 的速率差；ADR-011 slot 解决 adapter 与后继
Observation 的速率差。报告必须分别命名并分别计数，禁止用 admission 后 slot 代替 raw slot
证据。

## 3. Pixel V1

### 3.1 Canonical bytes 与 PixelSpec

每个进入 raw slot 的 sample 在 publish 前复制为 Core-owned immutable bytes。数组只作为暂态
decode view；冻结 dataclass 并不代表可变数组已经获得不可变所有权。

`PixelSpec V1` 的必填字段与 digest preimage 固定为以下对象；artifact envelope 的
`schema_version` 不进入 PixelSpec：

```json
{"channels":3,"dtype":"uint8","height":1080,"length":6220800,"pixel_format":"BGR8","stride":5760,"width":1920}
```

Canonical JSON 使用 UTF-8、字段名升序、`,`/`:` 分隔、无空白；整数使用十进制且禁止浮点。
bytes 为 packed、C-contiguous、row-major HWC、无行填充的 BGR8 `uint8`。Pilot raw source 固定为
`1920×1080×3`、`stride=5760`、`length=6,220,800`。若生成 `1296×700`
working pixels，它是独立 PixelArtifact，必须记录 raw parent digest、calibration hash、transform
version 和自己的 PixelSpec/digest，不覆盖 raw artifact。

### 3.2 Pixel V1 digest 与引用

Pixel digest 冻结为：

```text
pixel_digest = sha256(
    ASCII("MAPLE_PIXEL_V1") || 0x00 ||
    canonical_pixel_spec_json_utf8 || 0x00 ||
    exact_uncompressed_pixel_bytes
)
```

输出为 64 位小写十六进制。001B 中 `RawFrame.content_hash` 与其 accepted
`FramePacket.content_hash` 必须等于 raw source `pixel_digest`；`image_ref` 唯一派生为：

```text
cas://sha256/<pixel_digest>
```

跨实现 known-answer vector：上述 Pilot PixelSpec 配 `6,220,800` 个 `0x00` bytes 的
`pixel_digest` 必须为：

```text
c23a85d7fe7002f426293d40fb9a02a8795c41f7ef7ea801b082a969793ab4bc
```

CAS object envelope 只携带由 CAS key/backing bytes 可重算的 immutable PixelSpec、storage
encoding/hash 与 payload length；不承载 session、privacy、source-container 或 derivation
断言。这些字段放在独立 occurrence envelope，其 `artifact_sha256` 必须被
corpus/Candidate 外层证据冻结。resolver
读取对象后必须无损解码到 exact uncompressed bytes，再按上述公式重算。raw、Zstd
或 lossless PNG 等 backing encoding 另记 `storage_encoding`、`encoded_sha256` 和
`encoded_size`，不得替代 Pixel V1 digest。JPEG 等有损编码只可作为 source/diagnostic artifact，
不得作为 Pixel V1 CAS backing object。digest/spec/长度不匹配、路径穿越、symlink 越界或 CAS
对象漂移均 fail closed 并进入 evidence failure。相同 pixels 配合法递增 frame ID 表示静止画面，
不算 duplicate。

CAS mutation 由 storage-root scoped OS 文件锁串行化。derived occurrence 的 parent 复验、完整
child→parent 图扫描、object/occurrence 原子发布与写后 DAG 校验必须处于同一跨进程事务；manifest
同时拒绝不在自身 pixel 集合中的 parent 与任意长度的派生环。

### 3.3 CAS 生命周期与隐私

FramePacket 发出前，所引用的 CAS 对象必须已可解析且通过一次重算验证。每个 live frame 都进入
hash ledger；硬件 smoke 期间的完整对象可使用明确的受限临时 retention class，入选 corpus 的
对象进入持久私有 CAS。公开 evidence 只保留 hashes、计数、去标识衍生物和隐私审计结果。

每个 occurrence PixelArtifact 记录 `privacy_class`、`retention_class`、
`source_provenance_id`、`session_id`、`source_sequence`。raw artifact 的 parent/
transform/calibration 三字段全为 null；derived artifact 三字段必须全部存在、
parent 已验证存在且不得 self-reference。真实账号、角色标识、设备用户名
和私有路径映射只保存在受控范围。

## 4. Raw capacity=1 与 drain-to-latest

### 4.1 槽位语义

1. backend reader 是唯一 producer，并连续 retrieve/decode；adapter 是唯一逻辑 consumer；
2. raw slot 最多包含一个尚未 take 的 immutable sample；
3. publish 在槽已满时原子覆盖旧 pending sample，并增加 `superseded`；
4. take 原子取出并清空 pending，同时增加 `delivered`；
5. 多线程调用 consumer API 时必须序列化为同一逻辑 consumer，或以稳定错误码拒绝额外 consumer；
6. source sequence 按每次成功 backend retrieval 单调递增；Core 保留 sequence gap，不重编号；
7. backend EOF、read/decode/copy/hash error、设备身份或协商格式漂移均锁存当前 session 为 fatal；
8. 自动 reconnect 与静默 backend fallback 关闭。恢复创建新 backend 实例、新 `session_id` 和新
   counter epoch。

### 4.2 计数不变量

每个 counter epoch 从零开始，并在同一个原子 metrics snapshot 中报告：

```text
produced = delivered + superseded + pending + in_flight
           + discarded_on_reset + discarded_on_error
pending ∈ {0, 1}
in_flight ∈ {0, 1}
max_depth ≤ 1
```

定义如下：

- `produced`：成功形成并 publish 到 raw slot 的 sample 数；
- `delivered`：take 成功返回的 sample 数；
- `superseded`：尚未 take 即被更新 sample 覆盖的数量；
- `pending`：snapshot 时槽内未读数量；
- `in_flight`：已从槽中保留、正在执行 Pixel CAS put/read 双重校验的数量；
- `discarded_on_reset`：session/reset 封口时被显式丢弃的 pending/in-flight 数；
- `discarded_on_error`：CAS 录入、解析或交付前校验失败后终止的保留 sample 数。

reset 必须先封存旧 epoch snapshot；新 epoch 的 counters 全部归零。正常 stop 不静默丢弃
pending：先停止 producer，再由 controller 在同一个绝对 stop deadline 内执行必须的
final drain；该 drain 不重绑逻辑 consumer thread。若 `produced>0` 且未发生 fatal error，
最终 delivered sequence 必须等于最后 produced sequence。stop 幂等，capture/start/
backend-stop/drain worker 与 backend child 的退出状态进入报告；超时或残留属于 fatal error。

## 5. 时间与缓冲声明

VC-003 adapter 的 `captured_at_ns` 语义冻结为：backend 成功返回 decoded frame 后立即读取的
host monotonic timestamp，报告值为 `timestamp_origin=host_monotonic_post_retrieve`。它不是
sensor exposure timestamp、device PTS 或 glass-to-glass latency。若兼容期继续保留
`received_at_ns`，其值必须与 `captured_at_ns` 完全相同，且不形成第二套 freshness 语义。

FrameSourceAdapter 使用同一 clock domain 的注入式 `now_ns` 计算 host-side age；
`age_ns <= 250,000,000` 才符合 ADR-011 freshness。DirectShow、驱动、USB、采集卡固件或
FFmpeg/OpenCV 的 upstream queue depth 固定报告为 `unknown`，除非后继专用测量形成独立证据。
即使 backend 接受了 buffer-size 请求，也只记录 requested/reported 值，不把其升级成已证实容量。

因此 `capacity=1/drain-to-latest` 的 Gate 结论严格限定为 Core-owned raw slot；五分钟 smoke
也不产生 sensor latency 或 vendor queue depth 结论。

## 6. VC-003 read-only 与输入所有权

Pilot logical source 为 `capture-card-primary`，现场 selector 优先使用 `VC-003 Video` 与受控的
匿名设备指纹，device index 只作诊断信息。B2 backend 必须从受控枚举/
wrapper 报告独立测得的 fingerprint，不得回显 config label 伪装身份事实；
通用 OpenCV 取不到稳定身份时只能使用 offline/unknown fingerprint，不得进入 B2
PASS。backend 在 session 开始前确定并写入 provenance；
运行中 backend、device、format、FourCC 或 calibration 变化即 fatal。

capture package 不依赖 `InputSink`、receiver、键盘、鼠标、窗口写入或前台焦点控制。B1 CI、
B2 hardware smoke 和 Candidate packet 必须同时满足：

```text
input_owner = legacy
real_input_call_count = 0
double_write_event_count = 0
receiver_connect_count = 0
window_write_count = 0
```

VC-003 的只读含义是只从视频源取样；请求 capture format 属采集配置，不授予 Core v2 任何游戏
输入权。

## 7. 来源谱系

Legacy capture 迁移源当前未包含在 `evidence/baseline/legacy-snapshot.json` 的 8 个文件中，且在
已记录 upstream commit `3e19173f8da5aab8405307bb9c6e3741dd3abd6b` 中无对应文件。B1 必须生成
新的 file-level migration snapshot，并使用 `source_kind=unversioned_legacy_local_snapshot`：

| 迁移源 | SHA-256 |
|---|---|
| `src/input/capture_card_source.py` | `a2f312da774ca61e2fae0f043c933f2be90ff08a239fc7d56bb151f36b39050c` |
| `src/input/frame_source.py` | `3cebd1e60d450143fabe7efc9de3c5ac698919e6d33a9024fa43b565b2c7e2d8` |
| `src/input/frame_normalizer.py` | `8eb0c84caae10dbf3564c52ef770a75e81548801f15fe485d2462c33a26a4be1` |

snapshot 至少记录 logical source ID、受控 locator、字节数、mtime、snapshot time、SHA-256、
source kind、upstream relationship、迁移用途和 reviewer。不得把上述文件表述为 upstream commit
内容，也不得在 Core runtime 从 Legacy 目录 import。

所有 corpus derivation 使用以下可重算链：

```text
source artifact hash
→ extraction tool/version + frame index/PTS
→ raw Pixel V1 digest
→ redaction transform/mask hash
→ de-identified derivative hash
→ truth record hash
→ corpus/split manifest hash
→ reports
→ G1 Frame Candidate packet
```

历史录像的 container FPS/PTS 只作 locator；现场 capture rate 使用 hardware smoke 的 host
monotonic 计数与窗口计算。

## 8. B1/B2 Gate 分轨

| 子包 | 范围 | 退出结论 |
|---|---|---|
| `G1-FRM-001B1` | Pixel V1/CAS、raw latest、VC-003 adapter + fake backend、provenance/corpus/truth 工具、Event Tape 映射、stress、schemas/verifiers、精确依赖锁和无硬件 CI | 软件基础完成；不生成硬件 PASS 或完整 FrameSource PASS |
| `G1-FRM-001B2` | 安装精确 B1 CI wheel，执行真实 VC-003 300 秒 smoke，封存真实 corpus/truth/privacy/provenance，并构建新 G1 Frame Candidate packet | 允许提交完整 `G1-FRM-001` 审计；G1 Gate 仍保持 In Progress |

B2 原则上不修改 B1 source、capture config、schema、tool 或依赖锁。硬件暴露实现缺陷时，发布新
B1 source commit 与 wheel，并从 B1 CI 和 B2 smoke 重新开始。

## 9. 五分钟硬件门槛

硬件报告必须绑定 B1 source commit、远端 CI wheel SHA-256、dependency lock、effective capture
config、calibration、adapter/backend 版本与匿名设备指纹。使用 clean Python 3.12 环境，预热
30 秒不计入统计，之后在单一 session 中测量至少 300.000 秒：

- requested `1920×1080@30`，记录 negotiated width/height/FourCC/FPS/backend；
- `successful_frames >= 9000` 且 `successful_frames / measured_seconds >= 30.0`；
- `admitted_frames >= 4500` 且 `admitted_frames / measured_seconds >= 15.0`；
- 相邻成功 retrieval 的 `max_inter_frame_gap_ns <= 250,000,000`；backend no-frame poll/timeout
  单独计数，不与 read/decode failure 混合；
- 所有成功 frame 为 canonical `1920×1080 BGR8`，所有 accepted frame host age `<=250ms`；
- source/read/decode failure、fatal、reconnect、fallback、stale、duplicate/out-of-order sequence、
  timestamp/identity/size/clock mismatch 均为 0；
- raw `max_depth=1`、counter equation 成立、final drain 返回最后 produced sequence；
- Event Tape hash chain、frame ledger、Pixel CAS 与 FramePacket cross-link 的 orphan/mismatch 均为 0；
- Core input/receiver/window write 和 double write 均为 0；stop 后残留 thread/child 为 0。

FPS 使用完整精度；29.97 不按 30 处理。该窗口采用比路线图全局 `read failure <=0.1%` 更严格的
零失败门槛，因为 ADR-011 将 source error 定义为 session fatal。

## 10. 失败、HOLD 与隔离语义

- 预检发现设备缺席、已被其他进程占用或没有合格现场窗口：`HOLD/NOT-RUN`；B1 结论保留，B2
  继续排队；
- 设备可用且开始 open/run 后，任何硬门槛或不变量未满足：`FAIL`，保留原始失败报告；
- source/wheel/config/schema/tool/packet 谱系不一致、CAS corruption、Event Tape 断链或公开制品
  含未去标识信息：`QUARANTINE`；
- hardware run 后任一绑定的软件或配置发生变化：旧报告标为 superseded，新的 source commit
  重新执行完整链；
- 设备枚举、历史 Legacy 日志、短 probe、文件时长或容器 FPS 的证据等级保持为补充 provenance，
  不替代本 ADR 的 hardware report。

## 11. Candidate packet 与 G0 封存

新 packet 的范围是 `G1 Frame Candidate`，生命周期保持 `candidate` 或 `shadow`，并显式记录
`overall_g1_state=In Progress`、`input_owner=legacy`、`real_input_call_count=0`。packet 绑定：

```text
B1 source commit S + CI wheel hash
→ hardware/corpus/truth/provenance/privacy report hashes
→ packet artifact hashes
→ packaging/outer-verification commit P（由外层 CI/Gate record 记录）
```

packet 本体排除包含自身的 commit hash，避免自引用。现有 G0 source
`7da29f4cfae0bd984b00c394b78e637088a7e452`、sealed packet
`04c794c59eb98af6e739415e1ecb72a335795bb9` 及其 Bundle/evidence 保持字节不变；
`verify_bundle --strict-g0` 继续独立通过。G1 使用新增 strict verifier，不复写 G0 manifest、report
或 failure index 的既有事实；B1/B2 失败进入独立的 G1 Frame failure index。

`G1-FRM-001B1` 与 `G1-FRM-001B2` 均完成并会签后，才提交 `G1-FRM-001` Completed 评审并
解锁 `G1-OBS-002`。这一步不等于完整 G1 Gate PASS。

## 12. 影响

- FramePacket 的 pixel identity 从外部声明升级为可重算 bytes/spec/content address 闭环；
- producer backpressure 有独立的容量、计数与 session 证据，不再与 admission latest 混淆；
- 硬件结论限定在真实测量范围，driver queue 与 sensor latency 保持 unknown；
- 真实素材进入私有、可追溯、可审计的数据链，公开 evidence 维持去标识；
- 软件确定性 Gate 与现场设备 Gate 分离，硬件报告能稳定绑定到唯一 source/wheel。
