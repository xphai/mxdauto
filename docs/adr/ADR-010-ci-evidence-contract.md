# ADR-010: CI Evidence Contract 与阶段门禁

**状态**: 接受（Accepted）
**日期**: 2026-08-29

## 背景

“测试通过”如果没有 commit、运行环境、命令和产物绑定，就不足以证明某个 Core v2 Bundle 可复现。仓库当前已有 GitHub Actions 基线，但当前流水线只覆盖静态质量、Manifest 示例校验、类型检查、pytest 和覆盖率；它还没有替代 Golden Replay、Shadow、干净机 smoke 或现场认证。

## 决策

### 1. 必须通过的 G0 CI 检查

`.github/workflows/ci.yml` 在 Pull Request、推送 `main` 和手动触发时执行，使用 Windows runner、Python 3.12 和开发依赖。以下检查全部成功才算 G0 CI 通过：

| 检查 | 当前命令 | 通过条件 |
|---|---|---|
| Ruff lint | `python -m ruff check .` | 退出码为 0 |
| Ruff format | `python -m ruff format --check .` | 退出码为 0 |
| Manifest schema | `python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json schemas/runtime-manifest.example.json` | 示例 manifest 通过 Draft 2020-12 schema |
| Mypy | `python -m mypy` | 退出码为 0 |
| Pytest + coverage | `python -m pytest --cov=maple_automation_core --cov-report=term-missing --cov-report=xml --cov-fail-under=90` | 测试退出码为 0 且覆盖率不低于 90% |

`coverage.xml` 由 workflow 以 `coverage-xml` artifact 上传；上传步骤的 `if: always()` 不会把前置失败转换为成功。

### 2. Evidence 记录格式

每次用于评审或发布的 CI 运行必须记录以下最小元数据（可在 GitHub Actions run、PR 检查和 artifact 索引中实现）：

```text
evidence_id
workflow_name
run_id / run_attempt
source_commit
event (pull_request | push | workflow_dispatch)
runner_os
python_version
dependency_install_result
check_results[]
artifact_names[]
started_at / completed_at
```

当运行与 Runtime Bundle 关联时，还必须填写 `release_id`、`runtime_manifest_version` 和 manifest hash；发布证据必须进一步引用 schema 要求的 `test_report_id` 和 `replay_report_id`。未绑定到 commit 和 Bundle 的截图、聊天文字或本地输出不构成发布证据。

### 3. 阶段边界

- G0：上述静态 CI 是合并基线；其结果只证明代码和示例 Manifest 通过，不证明现场控制安全或 Core v2 可接管输入。
- G1 前：必须增加/接入固定黄金录像的确定性回放、Core v2 Shadow 对照和干净机器 smoke，并把报告 ID 绑定到 Candidate Bundle。
- Canary/Certified 前：必须增加故障注入、现场会话、恢复指标和回退演练；具体阈值由对应 Stage Gate/ADR 明确。
- 在所有阶段，Core v2 Shadow 不得调用真实 `InputSink`；Legacy 保持唯一真实输入下发者。CI 不会通过模拟测试自动授予 Core v2 输入权。

## 本地复现

在仓库根目录执行以下 PowerShell 命令可以复现当前 G0 检查：

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python tools/validate_runtime_manifest.py --schema schemas/runtime-manifest.schema.json schemas/runtime-manifest.example.json
python -m mypy
python -m pytest --cov=maple_automation_core --cov-report=term-missing --cov-report=xml --cov-fail-under=90
```

本地输出必须与当前 commit 一起提交到评审记录；不要手工修改覆盖率或 manifest 示例来伪造证据。

## 实施要求

1. 保持 workflow 的命令、Python 版本和覆盖率门槛与本 ADR 同步；任何变更先更新 ADR 和战术包。
2. Pull Request 描述必须链接 CI run，并列出每个检查的结果；失败检查不得以“已知问题”标记为通过。
3. 新增运行时契约时，同一 PR 必须增加 contract test；新增回放/Shadow 行为时，必须增加对应 evidence 产物或明确标记为 G1 前置任务。
4. CI 只验证它实际运行的命令。没有生成的 `test_report_id`、`replay_report_id` 或现场 session ID 不得填入认证 Bundle。
5. 证据 artifact 的命名必须稳定，例如 `coverage-xml`、`test-report-<release_id>`、`replay-report-<release_id>`、`shadow-report-<session_id>`；实际 workflow 尚未上传的 artifact 不得在 README 中宣称已存在。

## 影响

- 合并和发布决策从口头状态变成可追溯证据。
- G0 的静态质量门禁与 G1/现场门禁边界清晰，不会把单元测试误报为实机认证。
- 每个 Bundle 都可以追溯到源码、命令、运行环境和报告；维护证据索引需要额外工作，但减少了不可复现发布。
