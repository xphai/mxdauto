# ADR-010: CI Evidence Contract 与阶段门禁

**状态**: 接受（Accepted）
**日期**: 2026-08-29

## 背景

“测试通过”如果没有 source commit、sealed packet commit、运行环境、命令、metadata 总状态和产物 hash 绑定，就不足以证明某个 Core v2 Bundle 可复现。当前流水线已覆盖 G0 最小 Replay、Shadow、clean smoke 与 build；这些仍只是工程证据，不替代 G1 完整 corpus、G2 receiver clean-host 或任何现场认证。

## 决策

### 1. 必须通过的主线 CI 检查

`.github/workflows/ci.yml` 在 Pull Request、推送 `main` 和手动触发时执行，使用 Windows runner、Python 3.12 和开发依赖。以下检查全部成功才算 required `quality` 通过；阶段 Gate 仍独立判定：

| 检查 | 当前命令 | 通过条件 |
|---|---|---|
| Dependency lock | `verify_dependency_lock.py --check-installed` | 精确锁与已安装版本一致 |
| Ruff lint | `python -m ruff check src tests tools` | 退出码为 0 |
| Ruff format | `python -m ruff format --check src tests tools` | 退出码为 0 |
| Manifest / Candidate | schema validator + `verify_bundle.py --metadata-only --strict-g0` | example 与实际 Candidate schema 通过；strict evidence graph 通过 |
| Mypy | `python -m mypy` | 退出码为 0 |
| Checkout regression | `python tools/run_clean_smoke.py --mode checkout-regression ...` | 当前 HEAD 在隔离 venv build/install/test/Manifest/Replay/Shadow/rollback 全通过；报告绑定 tested commit |
| Pytest + coverage | `python -m pytest --junitxml=... --cov=maple_automation_core ... --cov-fail-under=90` | JUnit 无失败/错误且覆盖率不低于 90% |
| Checkout Golden Replay / Shadow | `run_golden_replay.py --runs 3`；`run_shadow.py`（均不传旧 manifest） | 当前代码的 3 次 deterministic；真实输入/双写为 0；由外层 CI evidence 绑定 HEAD |
| G1 Frame admission | `run_frame_admission_replay.py --runs 3` | synthetic fault matrix 三次 canonical digest 相同，专用 schema 通过，真实输入/双写为 0 |
| Build | `python -m build --wheel --sdist --no-isolation` + normalized sdist | wheel/sdist 生成且 hash 可查 |
| Evidence collector | `collect_ci_evidence.py ... --workflow-result ...` | payload `status=passed` 且 collector 退出码为 0 |

G0 历史 run 保留其原始 artifact 名。当前 workflow 使用稳定的 run-scoped 名：`quality-reports-<run_id>`、`build-artifacts-<run_id>`、`ci-evidence-<run_id>`、`checkout-smoke-<run_id>`、`g1-frame-admission-<run_id>`。上传步骤的 `if: always()` 只保证失败产物保留；它不把失败转换为通过。

### 2. Fail-closed metadata 规则

GitHub workflow conclusion 与 `ci-evidence.json.status` 必须同时为 `success/passed`。collector 必须：

1. 逐项记录 GitHub step outcome，不把 `success`/`passed` 混作解析错误；
2. 按 report kind 应用不变量：fixture SHA 与 canonical report digest 属于 Replay/Shadow，不强加给 clean-smoke；
3. G0 seal 路径分别记录 Candidate `source_commit` 与实际 Git `checkout_commit`，并保留其 packaging-only 规则；当前 checkout regression 只要求 sealed source 是 HEAD 祖先，不把新 wheel 绑定到旧 Candidate；
4. checkout regression 的 `source_commit=summary.tested_commit=checkout_commit=HEAD`，旧 G0 manifest 只记为 baseline；该报告作为当前 checkout artifact，不作为旧 Candidate evidence report 输入 collector；
5. 任一缺失、schema/binding/hash/invariant 错误或 workflow failure 使 payload 失败；
6. payload 总状态不是 `passed` 时自身以非零退出；artifact 仍上传供审计。

run `33202897083` 正是 workflow success / metadata failed 的隔离样本，不参与 G0 绑定。修复后的 run [`33204844985`](https://github.com/xphai/mxdauto/actions/runs/33204844985) 同时满足两层状态，`source=7da29f4...`、`checkout=4317c47...`，其 payload 已纳入 sealed packet `04c794c...`。successor run [`33205169227`](https://github.com/xphai/mxdauto/actions/runs/33205169227) 再以 `checkout=04c794c...` 复验最终 packet。失败 run 的原始材料与 digest 统一记录在 `evidence/failures/failure-index.json`。

### 3. Evidence 记录格式

每次用于评审或发布的 CI 运行必须记录以下最小元数据（可在 GitHub Actions run、PR 检查和 artifact 索引中实现）：

```text
evidence_id
workflow_name
run_id / run_attempt
source_commit
sealed_packet_commit / checkout_commit / head_sha
event (pull_request | push | workflow_dispatch)
runner_os
python_version
dependency_install_result
check_results[]
artifact_names[]
artifact_sha256[]
started_at / completed_at
```

当运行与 Runtime Bundle 关联时，还必须填写 `release_id`、Manifest hash、test/replay/shadow/clean report ID 与 hash。sealed G0 证据继续遵循 source commit / packet checkout 与 packaging-only 谱系；G1 当前 checkout 报告独立绑定 HEAD，并把 G0 Bundle 仅列为 regression baseline。二者不得互相替代。未绑定实际 commit 的截图、聊天文字或本地输出不构成 Gate 证据。

### 4. 阶段边界

- G0：上述 CI 证明 Candidate 工程管道、最小 synthetic Replay/Shadow 和 clean smoke；不证明完整感知链、现场控制安全或 Core v2 可接管输入。
- G1：必须扩展为固定录像 corpus、人工 truth/split、完整感知/WorldState/Planner 与 Legacy Shadow taxonomy；G0 synthetic smoke 不得被改名为 G1 完成。
- Canary/Certified 前：必须增加故障注入、现场会话、恢复指标和回退演练；具体阈值由对应 Stage Gate/ADR 明确。
- 在所有阶段，Core v2 Shadow 不得调用真实 `InputSink`；Legacy 保持唯一真实输入下发者。CI 不会通过模拟测试自动授予 Core v2 输入权。

## 本地复现

在仓库根目录执行以下 PowerShell 命令可以复现当前 G0 检查：

```powershell
python -m pip install --requirement configs/requirements.lock
python tools/verify_dependency_lock.py --lock configs/requirements.lock --check-installed
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest schemas/runtime-manifest.example.json
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json --manifest bundles/candidate-core-v2-20260829-shadow/runtime-manifest.json
python tools/verify_bundle.py --bundle-dir bundles/candidate-core-v2-20260829-shadow --metadata-only --strict-g0
python -m mypy
python tools/run_clean_smoke.py --mode checkout-regression --output evidence/ci-run/checkout-smoke-report.json
python -m pytest --junitxml=evidence/ci-run/junit.xml --cov=maple_automation_core --cov-report=term-missing --cov-report=xml:evidence/ci-run/coverage.xml --cov-fail-under=90
python tools/run_golden_replay.py --runs 3 --report evidence/ci-run/golden-replay-report.json
python tools/run_shadow.py --report evidence/ci-run/golden-shadow-report.json
python tools/run_frame_admission_replay.py --runs 3 --fixture fixtures/g1/frame_admission_v1.json --schema schemas/frame-admission-report.schema.json --report evidence/ci-run/frame-admission-report.json
```

checkout regression 要求整洁、已提交的工作树。输出必须与当前 commit 一起进入评审记录；不得手工修改覆盖率或 manifest 示例来改变证据结论。

## 实施要求

1. 保持 workflow 的命令、Python 版本和覆盖率门槛与本 ADR 同步；任何变更先更新 ADR 和战术包。
2. Pull Request 描述必须链接 CI run，并列出每个检查的结果；失败检查不得以“已知问题”标记为通过。
3. 新增运行时契约时，同一 PR 必须增加 contract test；新增回放/Shadow 行为时，必须增加对应 evidence 产物或明确标记为 G1 前置任务。
4. CI 只验证它实际运行的命令。没有生成的 `test_report_id`、`replay_report_id` 或现场 session ID 不得填入认证 Bundle。
5. 证据 artifact 使用 workflow 中的五组 run-scoped 稳定名称；实际未上传或 metadata failed 的 artifact 不得宣称为 Gate 通过证据。
6. branch protection、required checks、PR 和 Gate countersign 是独立治理门禁；CI passed 不自动产生 G0 PASS。

## 影响

- 合并和发布决策从口头状态变成可追溯证据。
- G0 的静态质量门禁与 G1/现场门禁边界清晰，不会把单元测试误报为实机认证。
- 每个 Bundle 都可以追溯到源码、命令、运行环境和报告；维护证据索引需要额外工作，但减少了不可复现发布。
