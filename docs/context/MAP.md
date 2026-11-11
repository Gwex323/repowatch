# 目录、配置与运行对象映射

核验日期：2026-11-09。本轮增量范围：P4 同步、PR 生命周期、评审返修和浏览器夹具的位置。各阶段当前验收范围见 [NOW](NOW.md)，本页不替代运行证据或外部部署验证。

## 已存在的位置

所有路径均相对项目根目录；从 [README](../../README.md) 和 [AGENTS](../../AGENTS.md) 所在目录定位根，不依赖个人设备绝对路径。

| 位置 | 对象与关系 |
| --- | --- |
| 根目录 README、AGENTS、PROJECT_INDEX、GLOSSARY | 用途、规则、路由、术语；职责见 [项目索引](../../PROJECT_INDEX.md) |
| `docs/` | 需求、架构、实施契约、流程与可行性报告 |
| `docs/adr/` | 已接受的架构决定；保留原位置 |
| `docs/research/` | 固定提交的调查证据，不是安装目录或依赖锁 |
| `docs/context/` | 当前状态、位置映射、操作手册及历史导航 |
| `docs/development/` | 开发安装、P0 原型边界、协议字段、正式持久化／预算／控制恢复／供应商及工作区、修复发布和持续跟进说明 |
| `backend/pyproject.toml`、`backend/uv.lock` | Python 3.12 开发工程与锁定依赖 |
| `backend/src/repowatch/` | domain 业务对象，application 业务端口与持久化，adapters 的 SQLite、Windows 作业／凭据、模型、GitHub 与受控工作区适配；runtime 正式图及原型，api／worker 正式入口与串行派发 |
| `backend/src/repowatch/application/` 的 P4 模块 | `github_sync.py`、`github_polling.py`、`remote_observations.py`、`pr_lifecycle.py` 分别管理巡检、端点缓存／水位、原子观测和 PR／CI／评审范围；API 生命周期位于 `backend/src/repowatch/api/synchronizer.py`，返修沿用既有 Worker 与受控发布器 |
| P5 记忆模块 | `application/memory.py` 管来源／版本／共享与反馈；`application/memory_context.py` 管可撤销注入；`adapters/memory_index.py` 管锁定 CPU 编码、FTS／向量派生索引；`worker/memory.py` 管共享来源预算的提炼和复核；`frontend/src/Memory.tsx` 提供管理界面 |
| P6 本地运维模块 | `operations/launcher.py` 管 Windows 服务作业树，`locks.py` 管服务占用及维护屏障，`maintenance.py` 管备份／恢复与保留，`reconcile.py` 管恢复后只读远端／凭据核对与显式绑定确认；`native.py`／`native_prepare.py` 管原生配置、准备及实时准入，`adapters/native_client.py`／`native_workspaces.py` 和 `worker/native_runner.py` 接入固定工具链与原生命令，`_native/` 提供打包宿主及隔离内核；`api/operations.py`、`frontend/src/Operations.tsx` 提供运维入口；`scripts/package.ps1`／`install.ps1` 制作和安装完整 wheel。原生样本验收状态见 NOW |
| `backend/migrations/`、`backend/tests/` | 保留历史迁移并接续正式 schema；P0—P5 各模块、跨层及受控故障回归，当前要求与通过范围见 NOW |
| `frontend/` | React／TypeScript／Vite、锁文件和控制台；`Synchronization.tsx`、`PullRequestLifecycle.tsx` 提供 P4 入口；`dist/` 由 API 同源提供，浏览器不承载任务执行 |
| `experiments/run.py`、`experiments/results/` | 本地切片及 E1 实验入口、公开脱敏报告；私有原始结果位于已忽略的 `results/local/` |
| `experiments/p6-github-acceptance.py`、`p6_samples.py`、`p6-adaptation.py` | P6 四类专用 sandbox 样本、真实产品链路及脱敏证据汇总；`p6-sample-browser.mjs` 检查实际候选 Web，`results/p6-adaptation.json` 登记冻结版本及边界，操作前置见[首批样本复跑](../development/local-operations.md#首批适配样本与复跑) |
| `experiments/p1-browser.mjs`、`experiments/p1_browser_host.py` | P1 Edge 浏览器操作与临时回环宿主夹具，证据位于已忽略的 `.cache/p1-browser/` |
| `experiments/p4-browser.mjs`、`experiments/p4_browser_host.py` | P4 巡检、目标漂移及评审范围 UI 的临时回环／HTTP 夹具；证据位于已忽略的 `.cache/p4-browser/`，不替代真实 GitHub 生命周期验收 |
| `experiments/providers.example.json` | E1 配置模板，使用显式环境变量引用；实际配置 `providers.local.json` 已忽略，说明见 [E1](../development/e1-providers.md) |
| `openspec/config.yaml` | 当前规划 schema 为 `spec-driven`，内容使用中文、保留结构标题和 SHALL／MUST；配置中未声明外部 store |
| `openspec/changes/build-github-issue-agent-harness/` | 活动变更；元数据创建日期为 2026-11-03；proposal、design、tasks 及 specs 能力目录 |
| `openspec/specs/` | 当前仅占位；不能把活动 delta 当作已实现主规格 |
| `openspec/changes/archive/` | 当前仅占位；未来归档由对应工作流管理，本轮不移动规划 |

## 计划位置与版本边界

- 工程已按 P0 建立，最终模块细分见 [design：模块边界](../../openspec/changes/build-github-issue-agent-harness/design.md#1-modular-monolith-and-module-boundaries)，未实现模块仍属后续阶段。
- 技术路线见同一设计章节；本次锁文件和与调研版本差异见 P0 报告。`0.1.0.dev0` 是工程开发版本，未发布产品。
- API／Worker、业务数据库／图检查点和 Docker 工作区的关系见 [实施契约](../implementation-contracts.md) §2、§6、§10。正式数据位置见[业务持久化](../development/persistence.md)，凭据与脱敏见[P1 供应商](../development/providers.md)，开发启动和阶段操作见[本地控制](../development/local-control.md)，正式图边界见[运行引擎](../development/runtime.md)。完整安装与运维验收仍按 P6 执行。

映射变化时只修订本页对应位置；具体操作写 [RUNBOOK](RUNBOOK.md)，当前推进状态写 [NOW](NOW.md)。
