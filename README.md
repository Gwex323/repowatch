# RepoWatch

RepoWatch 是面向本地开发的 GitHub Issue Agent Harness。P0—P6 的实施与受控技术验收已完成，具备持久调度／预算／恢复、仓库扫描、修复证据与受控发布、持续同步、记忆管理及本地安装／启停／运维；最终用户签收与发布另行记录。实际验证范围与剩余限制见 [NOW](docs/context/NOW.md)。

## 基本使用与接续

本地发行包制作、安装启停、升级与备份恢复见[本地安装与运维](docs/development/local-operations.md)；开发调试见[开发入口](docs/development/README.md)，人工控制见[本地控制](docs/development/local-control.md)。当前目标和接续点统一见 [NOW](docs/context/NOW.md)，操作前置条件见 [RUNBOOK](docs/context/RUNBOOK.md)。当前仍为开发预览，规划、代码验收、最终用户验收和发布授权分别记录。

后续 AI 先确认 [AGENTS](AGENTS.md) 中的适用规则，需要定位时使用 [PROJECT_INDEX](PROJECT_INDEX.md)。索引按问题选读，不要求顺序读完所有链接；目录和配置关系见 [MAP](docs/context/MAP.md)。

## 按需查阅

- 产品范围：[需求基线](docs/requirements-baseline.md)。
- 最终架构：[设计基线](docs/design-baseline.md)。
- 实现约束：[实施契约](docs/implementation-contracts.md)。
- 流程交接：[工作流程](docs/workflow-design.md)及[接入与记忆设计](docs/integration-discussion.md)。
- 复用、风险和实验：[可行性报告与阶段路线](docs/feasibility-report.md)。
- 待实施规划：[OpenSpec 提案](openspec/changes/build-github-issue-agent-harness/proposal.md)，按需定位规格、设计和任务。

## 决定与证据

- [术语表](GLOSSARY.md)、[架构决策记录](docs/adr/)、[访谈历史](docs/design-discovery.md)。
- [扫描自动补全规则](docs/scan-recovery-proposal.md)：已确认规则，沿用原讨论文件名。
- [相关项目实现及许可证对比](docs/research/project-comparison.md)、[固定提交快照](docs/research/repository-snapshots.json)。
- [文档复核结果与实现验证边界](docs/document-review.md)。

历史追溯路径见 [历史索引](docs/context/history/README.md)；历史检查范围不代表新增文档或当前产品已经验证。

巡检目标初值：活跃仓库约每 2 小时，空闲仓库每天一次，可按用户配置调整。设计文件描述完整目标行为；当前阶段证据以 NOW 和任务清单为准，P0 的真实供应商与对象交接切片不替代完整仓库修复和发布验收。
