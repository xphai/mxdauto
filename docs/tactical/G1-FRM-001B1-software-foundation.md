# 战术包：G1-FRM-001B1 FrameSource 软件基础

| 字段 | 值 |
|---|---|
| package_id | `G1-FRM-001B1` |
| parent | `G1-FRM-001B` / `G1-FRM-001` |
| requirement | `REQ-CAP-001`、`REQ-UI-001`、`REQ-SAFE-002`、`REQ-OBS-002`、`REQ-PRI-001`、`REQ-REL-002` |
| gate | G1 FrameSource 子门禁 |
| decision | Sol-U 已批准执行 |
| implementation | 5.6 Luna max |
| status | `In Progress` |
| baseline | `main@c6d9bf5a406ce612a48e424d91686ca4fde144d0` |
| ADR | ADR-002、ADR-004、ADR-007、ADR-010、ADR-011、ADR-012 |
| successor | `G1-FRM-001B2` |

## 1. 目标

本包交付可在无 VC-003 的 Windows CI 中完整证明的软件基础：

1. 实现 ADR-012 Pixel V1 canonical bytes、digest、CAS put/get/verify 与 retention/privacy metadata；
2. 在 backend reader 与 `FrameSourceAdapter` 之间实现 Core-owned raw capacity=1、
   drain-to-latest producer；
3. 实现显式 read-only VC-003 adapter contract，并以 deterministic fake backend 覆盖 lifecycle、
   format、failure 和 stop 行为；
4. 固化 source/session/timestamp/backend/config/calibration provenance；
5. 提供 frame-ingestion corpus/truth import、Event Tape 映射和全链 provenance audit；
6. 用 deterministic interleaving 与固定 seed stress 证明计数、单槽、session 隔离和无 torn sample；
7. 提供 B2 hardware report 与 G1 Frame Candidate packet 所需的 schemas、builders 和 semantic
   verifiers；
8. 在 clean Python 3.12 构建可唯一绑定的 wheel，并保持 Core v2 真实输入为 0；
9. 保持 G0 sealed source、packet、Bundle 与 evidence 字节不变。

## 2. 非目标与结论边界

- 本包不执行真实 VC-003 五分钟 smoke，也不形成设备可用或硬件通过结论；
- 不实现 monster/player 感知、ONNX、WorldState、Planner、Shadow diff 或 CV 模型 truth；
- 不连接 receiver、键盘、鼠标或游戏窗口，不申请 Core v2 输入租约；
- 不测量 sensor exposure、glass-to-glass latency、USB/driver/vendor queue depth；
- 不修改或重新封装 G0 sealed Bundle/evidence；
- 本包 Completed 只允许启动 `G1-FRM-001B2`，完整 `G1-FRM-001` 与 G1 Gate 继续保持
  `In Progress`。

## 3. 预期实现边界

实现 PR 允许触及以下类别；最终文件名可在评审中收敛，但职责边界必须保持：

```text
src/maple_automation_core/capture/**       # pixels/CAS/raw latest/backend/provenance
src/maple_automation_core/replay/**        # corpus、Event Tape frame mapping/audit
tests/test_*frame* / test_*capture*        # contract、stress、failure、privacy
tools/run_capture_pressure.py
tools/import_frame_corpus.py
tools/audit_frame_provenance.py
tools/build_g1_frame_candidate.py
tools/verify_g1_frame_candidate.py
schemas/frame-pixel-artifact.schema.json
schemas/capture-source-provenance.schema.json
schemas/frame-corpus-manifest.schema.json
schemas/frame-truth.schema.json
schemas/capture-pressure-report.schema.json
schemas/vc003-hardware-smoke-report.schema.json
schemas/frame-provenance-audit-report.schema.json
schemas/g1-frame-candidate-packet.schema.json
configs/requirements.lock / pyproject.toml
.github/workflows/ci.yml
docs/adr/ADR-012-frame-pixels-and-capture-source.md
docs/tactical/G1-FRM-001B*.md
docs/ROADMAP.md / docs/REQUIREMENTS-TRACEABILITY.md
```

实现不得从 `F:\mxd\source\MapleStoryAutoLevelUp-main` 运行时 import。若选择 OpenCV、FFmpeg
wrapper、NumPy 或其他 capture dependency，其 Python 3.12 精确版本、安装来源和 artifact hash
必须进入 lock/CI evidence；系统上偶然存在的 Python 3.13 package 不属于 B1 证据。

## 4. 必须交付的契约

### 4.1 Pixel V1 与 CAS

- 逐字实现 ADR-012 的 canonical PixelSpec JSON 与 domain-separated digest；
- publish 前将 decode view 复制为 owned immutable bytes；
- `FramePacket.content_hash == pixel_digest`，`image_ref=cas://sha256/<digest>`；
- CAS write 使用同文件系统临时文件到 final object 的原子替换；已存在对象必须重新核验，内容冲突
  fail closed；
- 所有 CAS mutation 使用 storage-root scoped OS 文件锁；parent 复验、派生图扫描、object/
  occurrence publish 与写后 DAG 校验属于同一跨进程事务；
- resolver 对 uncompressed bytes 重算 digest，并校验 spec、长度、encoding hash；
- CAS object metadata 只包含由 key/backing 可重算的不变事实；session/privacy/
  source-container/derivation 放在独立 occurrence envelope，并由 corpus/Candidate
  外层哈希冻结；
- storage root canonicalize 后执行 traversal/symlink boundary checks；
- raw source 与 derived working pixels 使用两个 PixelArtifact，以 parent digest、transform version 和
  calibration hash 相连；raw 三字段全 null，derived 三字段全存在、parent 已存在
  且禁止 self-parent、orphan parent 与任意长度的派生环；
- retention/privacy class 必填；测试 teardown 不把真实 raw pixels 上传为公开 CI artifact。

### 4.2 RawLatestSlot

每个 counter epoch 必须在原子 snapshot 中满足：

```text
produced = delivered + superseded + pending + in_flight
           + discarded_on_reset + discarded_on_error
pending ∈ {0,1}
in_flight ∈ {0,1}
max_depth = 1（只要至少 publish 一次）
```

必须实现：

- single producer、single logical consumer、immutable sample；
- overwrite pending、take-and-clear、blocked take deadline/cancel；
- atomic metrics、最后 produced/delivered sequence、counter epoch/session identity；
- producer stop → controller 在同一绝对 deadline 内执行必须 final drain（不重绑
  logical consumer）→ backend/thread/child cleanup；
- reset 封存旧 epoch，清点 pending/in-flight 到 `discarded_on_reset`，新 session
  counters 从零开始；
- CAS admission 失败的保留 sample 进入 `discarded_on_error`，不计入 delivered；
- 第二 consumer、publish-after-stop、old-session publish、double stop/reset race 都有稳定结果码；
- raw slot 与 ADR-011 admitted slot 使用不同类型和不同 metrics，报告中不得合并计数。

### 4.3 VC-003 adapter 与 lifecycle

最小接口应覆盖 `configure/describe/start/read/status/stop` 或语义等价方法，并冻结状态机：

```text
CREATED → RUNNING → STOPPED
              ↘ FATAL
```

- logical source=`capture-card-primary`；现场设备 selector=`VC-003 Video` + 匿名 fingerprint；
- B2 fingerprint 必须由受控后端枚举/wrapper 独立报告并与 provenance 精确一致；
  通用 OpenCV 的 config device label 不作测量身份，unknown fingerprint 仅限 offline CI；
- backend、device、requested/negotiated format、FourCC、versions 在 start 后冻结；
- reader 连续 retrieve，成功帧赋单调 source sequence；
- timestamp origin=`host_monotonic_post_retrieve`；兼容字段 `received_at_ns` 与
  `captured_at_ns` 相等；
- EOF/read/decode/copy/hash/identity/format drift 进入首次根因 fatal latch；
- session 内自动 reconnect、backend fallback 和 frame renumber 均关闭；
- stop 有 deadline、可重复调用，并报告 thread/child exit；
- `upstream_queue_depth=unknown` 是默认且正确的 provenance 值。

fake backend 必须可脚本化注入成功帧、blocked read、EOF、异常、format drift、timestamp rollback、
shutdown 延迟和不可退出 child，以便 CI 覆盖全部失败分支。

### 4.4 Provenance、corpus 与 Event Tape

新增 migration snapshot，至少绑定下列本地未版本化来源：

| 文件 | SHA-256 | provenance 结论 |
|---|---|---|
| `src/input/capture_card_source.py` | `a2f312da774ca61e2fae0f043c933f2be90ff08a239fc7d56bb151f36b39050c` | `unversioned_legacy_local_snapshot` |
| `src/input/frame_source.py` | `3cebd1e60d450143fabe7efc9de3c5ac698919e6d33a9024fa43b565b2c7e2d8` | `unversioned_legacy_local_snapshot` |
| `src/input/frame_normalizer.py` | `8eb0c84caae10dbf3564c52ef770a75e81548801f15fe485d2462c33a26a4be1` | `unversioned_legacy_local_snapshot` |

snapshot 同时记录这些文件未出现在 upstream
`3e19173f8da5aab8405307bb9c6e3741dd3abd6b`，并标明“迁移语义来源、无 runtime import”。

corpus/truth 工具只处理 frame ingestion truth：source artifact、session/frame locator、PixelSpec、
pixel digest、geometry/calibration、expected admission、derivation、privacy/license、review。工具必须：

- 按 source session 生成 split，拒绝 split 间 digest 重叠；
- 区分 raw private object、de-identified public derivative 与 hash-only ledger；
- 将 source→extraction→pixel→redaction→truth→manifest 每一跳绑定 SHA-256；
- 将 accepted/suppressed/fatal FrameSource 结果映射到 Event Tape，并保持 hash chain；
- 对 missing/orphan/mismatch、额外 schema 字段、非法 privacy class fail closed；
- 不把启发式 detector 输出标记为人工 truth。

B1 使用 synthetic/de-identified fixtures 验证工具；真实 3-session/300-frame corpus 在 B2 封存。
provenance audit 必须标注 `b1_fixture` 或 `b2_gate`；前者不得被 Candidate
verifier 升级。`b2_gate` 硬性要求 300 samples、300 unique pixels、3 个
independent sessions、六类 category 全覆盖、wrong-size negative、独立复核率
至少 20% 与 `full_cas`重算；其中至少一个 `live_session` 绑定同一硬件 smoke/
source provenance session，且该现场 session 至少贡献 100 个 samples。Candidate full verifier
必须由调用方显式提供受控 private CAS、truth root 与 Event Tape 路径并重建 provenance report；
报告中的 `full_cas` 字样不构成证明。

## 5. 证据 schema 与 semantic verifier

以下 schema 均使用明确 `schema_version`、`additionalProperties:false` 和稳定 enum：

| Schema/report | 最小强制内容 |
|---|---|
| `frame-pixel-artifact` | PixelSpec、digest/ref、encoded hash/size、retention/privacy、source/session/sequence、parent derivation |
| `capture-source-provenance` | logical source、匿名 fingerprint、requested/negotiated properties、backend/tool/dependency/source/config/calibration hashes、timestamp origin、upstream buffer knowledge |
| `frame-corpus-manifest` | corpus、sources/sessions/samples/splits、CAS/truth refs、privacy/license、derivation hashes |
| `frame-truth` | ingestion expectation、review/adjudication、record digest |
| `capture-pressure-report` | source/wheel/env/seeds/schedules、counter epochs、invariants、timeouts/memory、input audit、failures/status |
| `vc003-hardware-smoke-report` | B2 bindings、monotonic window、rates/counts、format、freshness、slot/process/Event Tape/input/privacy metrics、limitations |
| `frame-provenance-audit-report` | source→pixels→truth/corpus→FramePacket→Event Tape→hardware→packet cross-links 与 orphan/mismatch |
| `g1-frame-candidate-packet` | B1 source/wheel、G0 baseline ref、capture/pixel/calibration/corpus/truth/report hashes、input policy、limitations、signoffs |

每个 report 的通用字段至少包括：

```text
schema_version, report_id, generated_at,
source_commit, tool_artifact_sha256, config_sha256,
status, failures[], limitations[], artifacts[],
canonical_report_sha256
```

JSON Schema 只验证形状；semantic verifier 必须打开 artifacts、重算 hash/counters/rates/cross-links，
再派生最终 status，不信任报告中手填的 `passed=true`。报告自身 digest 使用规定的 canonical JSON
并排除自身 digest 字段，规则写入 schema 描述和 verifier test。

## 6. 验收矩阵

### 6.1 Pixel/CAS

| 场景 | 硬性预期 |
|---|---|
| canonical 1920×1080 BGR8 | exact PixelSpec/digest/ref，put/get 后 bytes 相同 |
| all-zero known-answer vector | digest=`c23a85d7fe7002f426293d40fb9a02a8795c41f7ef7ea801b082a969793ab4bc` |
| mutable decode buffer 后续被改写 | published bytes 与 digest 保持不变 |
| 相同 bytes、相同 spec | digest/ref 相同，CAS 幂等去重 |
| 相同 bytes、不同 spec | digest 不同 |
| encoded object 损坏 | resolver/verifier FAIL，FramePacket 不发出 |
| spec/length/hash mismatch | fail closed；failure evidence 保留 |
| traversal/symlink escape | 拒绝且无 storage root 外读写 |
| raw→working derivation | parent、calibration、transform 与两个 pixel digests 全部可重算 |
| 两进程并发追加 A→B/B→A | 恰一条边成功；写后 graph 保持无环 |

### 6.2 Deterministic 与 concurrent stress

1. barrier 控制 publish/take/reset/stop/fatal 的确定性 schedule；同一 fixture 连续 3 次的 events、
   counters、artifact list 与 canonical report digest 完全一致；
2. 固定 seeds 的随机 stress 总计至少 100,000 次轻量 raw publish/take；producer、consumer、metrics
   observer、stop/reset controller 并发运行；
3. 至少 1,000 次 blocked-read/start/reset/stop/session race；
4. 每个 epoch 的完整 counter equation 成立，pending/in-flight 只为 0/1，
   raw high-watermark 为 1；
5. delivered sequence 单调唯一，final drain 为 last produced，无 old-session crossing；
6. sample 的 sequence/spec/digest/bytes 无 torn tuple；无 deadlock、未捕获异常或重复终态；
7. stop 幂等，所有可正常退出 fixture 的 thread/child 在 2 秒内退出；不可退出 fixture 必须稳定
   产生 fatal cleanup report；
8. slot memory 不随 publish 数线性增长；report/event ledger 的显式增长单独计量；
9. 至少使用一组 full-size 1920×1080 fixtures 覆盖 copy/hash/CAS，与 100,000 次轻量 slot stress
   分开执行。

### 6.3 Read-only 与 failure matrix

| 场景 | 硬性预期 |
|---|---|
| backend EOF/read/decode error | 当前 session fatal latch；无自动 reconnect |
| identity/format/FourCC drift | fatal；后续 admission 抑制 |
| backend A open 失败 | 记录 A 失败；无静默 fallback 到 B |
| reset/recovery | 新 backend instance、新 session、新 counter epoch |
| stale/sequence/geometry mismatch | 沿用 ADR-011 reason code、Event Tape 与 plan suppression |
| input audit | `input_owner=legacy`；real input、receiver connect、window write、double write 全为 0 |
| upstream buffer | report 为 `unknown`，不声明 driver/vendor capacity=1 |

## 7. CI 与提交绑定

B1 PR 必须在 Windows/Python 3.12 执行：

- dependency lock exact-install verification；
- Ruff lint/format、Mypy、全量 Pytest、repo coverage `>=90%`；
- Pixel/CAS contract、deterministic 3-run、100,000 次 stress、1,000 lifecycle races；
- 全部新增 schema example 与 negative fixtures；
- semantic verifier tamper tests；
- 独立 G1 Frame failure index；失败 fixtures/reports 不进入 G0 failure index；
- current-checkout clean build/install/smoke；
- `verify_bundle --strict-g0` 对 sealed G0 继续成功；
- build wheel/sdist，并记录 source commit、wheel SHA-256、Python/runner/dependency lock；
- Core v2 `real_input_call_count=0`、`double_write_event_count=0`。

远端 CI artifact 至少包括 frame stress report、provenance fixture audit、JUnit/coverage、clean smoke、
build hashes 与 CI metadata。B2 只接受 protected merge 后 main CI 产生、与 B1 source commit 精确绑定
的 wheel；本地随手 build 不进入 hardware Gate。

### 7.1 打包与证据边界

- wheel 是可安装的 runtime 交付物，携带 `src/maple_automation_core/**` 与 `py.typed`；
  `tools/`、`schemas/`、`configs/`、`fixtures/` 和 `docs/` 属于 checkout/证据资产，
  不要求随 sdist 一并安装。Candidate packet 通过仓库相对路径与 SHA-256 引用这些资产。
- `configs/g1-frame-requirements.lock` 是 Windows/Python 3.12 runtime 的 hash-enforced lock；
  `configs/requirements.lock` 是 G0 开发/质量工具链的精确版本 lock。
  `checkout-regression` clean smoke 分别安装并验证两者；`g0-seal` 保持历史 G0 lock-only
  reproduction，不将未封存的 G1 runtime 依赖引入 G0 seal。G1 installed-runtime 报告的 runtime
  绑定固定为 G1 lock，G0 lock 仅用于开发/质量工具链。
- JUnit、coverage 和 machine-readable evidence 在上传前统一将 runner/checkout 临时路径相对化，
  并执行绝对路径扫描；XML 与已知的 checkout-smoke envelope 可在 collector 中完成预期 scrub，
  其余 JSON/JSONL/log 被重写时将 privacy check 标记为 failed；G0 sealed evidence 保持原字节不变。

## 8. 失败条件与回退

以下任一项保持 B1 `In Progress` 或形成失败记录：

- Pixel V1 bytes/spec/digest/ref 任一跳重算失败；
- raw slot capacity、counter equation、final drain、session isolation 失败；
- hidden reconnect/fallback、sequence 重编号、driver queue 被表述为已证实容量 1；
- timestamp 被标记为 device/sensor PTS；
- runtime import Legacy、source snapshot 错绑 upstream commit；
- stress deadlock、thread/child 泄漏、内存随 publish 线性增长；
- schema 只检查 `passed` 字段而未语义重算；
- Core input/receiver/window write 非零；
- G0 sealed artifact 发生任何字节变化或 strict-G0 regression 失败。

回退为关闭新 FrameSource feature entry、停止 producer、执行 final drain/cleanup、保留失败报告，运行
状态回到 001A admission/G0 sealed baseline；Legacy 继续独占真实输入。

## 9. 完成定义

`G1-FRM-001B1=Completed` 需要同时满足：

1. ADR-012 与本战术包冻结内容由 Sol-U 复核；
2. 实现 PR 通过 protected required checks 并合并；
3. main post-merge CI 全绿，source commit、wheel SHA-256、artifact hashes 和 metadata status 已绑定；
4. QA/evidence、技术和发布复核新增 schemas、tamper tests、stress 与 G0 seal；
5. zero-input audit 成功；
6. 实际 commit/run/hash 回填本包和需求追踪矩阵。

B1 完成后仅把 `G1-FRM-001B2` 从 `Queued` 推进到可执行；`G1-FRM-001`、`G1-OBS-002`
和完整 G1 Gate 状态不变。
