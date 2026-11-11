# 本地安装与运维

核验对象：P6 本地产品化。实现及验收范围以 [NOW](../context/NOW.md) 和 [任务](../../openspec/changes/build-github-issue-agent-harness/tasks.md#11-p6--local-packaging-and-operations) 为准；本文命令是操作说明，不代表已经执行。开发调试沿用[本地控制](local-control.md)，数据职责见[业务持久化](persistence.md)。

## 安装与生产资源

宿主为 Windows、Python 3.12；默认执行环境为 Docker Desktop 的 Linux 容器／WSL2。业务库、检查点、凭据和 API／Worker 均留在 Windows，不能在 WSL 侧直接打开同一数据库。安装器要求已有 uv；从源码制作发行包还需锁定的 pnpm 与 Node，不修改全局工具。

```powershell
pwsh -File scripts/package.ps1
pwsh -File .cache/release/install.ps1 -InstallDirectory <新的绝对安装目录>
```

安装器只接受新目录，不覆盖旧安装、不迁移数据。`-Offline -CacheDirectory <缓存目录>` 使用已准备的依赖缓存；缺少 wheel 时明确失败。发行目录包含 wheel、`requirements.lock` 和安装脚本。wheel 包含生产前端、迁移及编码资产清单；运行不依赖 Vite 开发服务器。模型实体不随包隐式下载；准备方式见[记忆说明](memory.md#可重建索引与降级)，默认关键词降级保持不变。

以下用安装目录中的 `Scripts/python.exe` 表示 `$pythonExecutable`，用独立本地数据目录表示 `$dataDirectory`。首次明确初始化后，队列保持暂停：

```powershell
& $pythonExecutable -m repowatch.operations initialize --data-dir $dataDirectory
& $pythonExecutable -m repowatch.operations start --data-dir $dataDirectory
& $pythonExecutable -m repowatch.operations status --data-dir $dataDirectory
& $pythonExecutable -m repowatch.operations open --data-dir $dataDirectory
& $pythonExecutable -m repowatch.operations stop --data-dir $dataDirectory
```

`start` 默认端口 8765，可用 `--port` 修改；`--no-browser` 只禁止首次打开浏览器。`open` 让运行中的 API 生成新的短期一次引导并打开页面，令牌只放 URL 片段，不保存到状态或日志。关闭浏览器不停止服务。`stop` 先保存暂停并核实命令退出，再等待 Worker／API 停止；状态为 `stopped` 且 `clean_stop=true` 才是已确认正常退出。异常或超时保留暂停和对账原因，不冒充正常停止。

启动器使用 Windows 作业对象管理子进程树，子服务先等待启动门，加入作业后才执行；API 就绪后才启动 Worker。状态携带代次与进程创建时间，不凭 PID 判断所有权。重复启动拒绝；重启不自动恢复业务队列。人工入口和恢复门槛沿用[控制与恢复](control-recovery.md)。

## 升级与回滚

先暂停、等待运行安全结束并建立有效备份，再停止服务。确认 `status` 已停止；关闭其他手工启动的 API／Worker。安装新版本到新目录，使用新解释器显式迁移：

```powershell
& $pythonExecutable -m repowatch.operations migrate --data-dir $dataDirectory
```

迁移同时取得唯一迁移锁和全部写者维护锁，存在打开的数据服务则拒绝。启动只检查 schema／关键 SDK，不隐式迁移；恢复未完成标记或不兼容检查点版本均拒绝。迁移保留已有暂停与数据，不自动降级。回滚使用对应版本的有效备份恢复到新目录，原目录和旧安装保留；不能用旧二进制直接打开升级后的库。

## 一致备份与恢复

UI 的“系统与维护”提供备份、保留策略、清理预览和结果回读。先暂停并等待运行／命令安全结束；有遗留执行时拒绝备份和清理。

```powershell
& $pythonExecutable -m repowatch.operations backup --data-dir $dataDirectory --operation-id <本次稳定标识>
& $pythonExecutable -m repowatch.operations restore --backup-path <备份绝对路径> --data-dir <新的本地数据目录>
```

备份位于数据目录的 `backups/backup-…/`。统一屏障覆盖完整 API 操作、同步轮询、Worker、检查点、索引、归档和清理；屏障内取得 SQLite 一致快照、固定产物／Git 对象及摘要清单，完成核验后原子公布。不会只复制活动主库而漏 WAL，不改变原暂停状态。屏障等待超时表示仍有写者，请处理后重新提交；未完成目录不算有效备份。

恢复只接受新目录，并校验清单格式、路径、每个摘要及数据库完整性。中断恢复留下禁止启动标记，不通过迁移掩盖缺失数据。恢复后强制暂停，旧工作区就绪标记失效，并生成待核对原因：

- 核对 GitHub Issue／PR、分支和发布回执。备份之后发生的远端写入仍可能存在，先只读核对并恢复关联，不能重发创建请求。
- 核对 Docker 工作区卷、冻结内容和实际证据；受管 Git 对象和归档随备份，运行卷不能凭旧记录宣称存在。
- 重新验证宿主凭据引用。备份不导出凭据库；更换数据目录会改变宿主凭据命名空间，须重新配置有效引用。

系统页提供“生成恢复核对报告”：只读查询当前仓库身份、目标分支和全部开放／关闭 PR，并验证当前凭据引用。匹配原任务的稳定分支、执行身份、上游／发布仓库、Issue 关闭声明及发布标记后，才列为可恢复绑定候选。点击“确认当前恢复核对”会重新读取并核对报告摘要，过期、多个匹配、缺凭据或身份冲突均拒绝；确认不发送任何远端写请求，不重建 PR，不把旧代码证据标为有效，也不自动解除暂停。通用“提交处理结果”不能绕过该核对。

```powershell
& $pythonExecutable -m repowatch.operations restore-report --data-dir $dataDirectory --operation-id <报告标识>
& $pythonExecutable -m repowatch.operations restore-acknowledge --data-dir $dataDirectory --operation-id <确认标识> --report-id <报告标识> --basis-sha256 <报告摘要> --explanation <核对依据>
```

完成核对后再通过明确恢复入口继续；其他遗留运行／未知远端操作仍须分别处理，不能只看到数据库恢复成功就放行队列。

## 磁盘与保留策略

默认最低可用空间 64 MiB；完成任务产物保留 30 天、命令日志 14 天、工作区 7 天，均可在 UI 修改。每次执行和发布守卫检查空间；不足时停止新执行／发布，管理和清理仍可使用，处理后重启并对账。

```powershell
& $pythonExecutable -m repowatch.operations cleanup --data-dir $dataDirectory
& $pythonExecutable -m repowatch.operations cleanup --data-dir $dataDirectory --apply --operation-id <稳定标识> --plan-sha256 <当前预览摘要>
```

清理保护未完成／阻塞任务及记忆来源证据。应用绑定当前策略和候选摘要，变化后重新预览；删除前先使业务引用失效，即使删除中断也不能复用旧证据。删除记忆不自动删除原任务审计材料。业务事实库及账本不由保留策略删除。

## 日志、原生执行与诊断

启动诊断为 `runtime/service.json` 及其中 `logs` 指向的 supervisor／API／Worker 代次日志；状态不包含控制令牌或模型密钥。清理保护当前服务日志，已退出代次按日志保留天数处理。日志统一脱敏，不记录原始 SDK 异常正文。`diagnose` 输出依赖、数据版本、磁盘、服务及备份状态，不调用模型、运行仓库脚本或改写 GitHub。

原生执行必须显式配置并通过隔离身份、文件、凭据及控制接口能力验收。当前适配对未经验证的原生请求明确拒绝，不回退为当前用户 shell；受支持的固定 CPython 标准库 CLI 样本已完成[独立验收](#原生宿主独立验收)。默认 Docker 路径继续按工作区能力准入执行。

环境准备阶段可使用 [`native-preflight.ps1`](../../scripts/native-preflight.ps1)，由操作者生成专用运行 ID、预期执行账户 SID 及无敏感内容的控制端标记路径，并将探针和配置分发到该账户可读的独立目录。脚本在身份不匹配或管理员身份时拒绝；只尝试打开已有标记的读／写句柄，不修改标记字节。仅明确的访问拒绝计为目录隔离证据，文件不存在和其他错误均为未核实。输出始终为 `native_admitted=false`，不检查真实密钥，也不能替代原生适配、凭据／控制接口边界或完整样本验收。原始环境配置和临时分发位置保存在本机忽略目录，不写入公开报告；密码只在本机 Windows 交互中输入。

[`p6-native-isolation.ps1`](../../experiments/p6-native-isolation.ps1) 调用打包的 [`qualification.ps1`](../../backend/src/repowatch/_native/qualification.ps1) 和 [`kernel.cs`](../../backend/src/repowatch/_native/kernel.cs)，验证 Windows 隔离机制。实验在核对普通账户后创建唯一临时 AppContainer，未授予网络能力；子进程使用显式环境、挂起创建、令牌核验和作业对象启动门。资格实验不继承句柄；正式命令仅继承受控的 stdin／stdout／stderr，不能继承作业或控制句柄。仅对新建目录设置包 SID 权限及任务目录低完整性标签，验证目录外和源码保护，不检查真实秘密。临时应用配置正常结束删除，无敏感内容的报告保留；实验通过仍不自动准入产品。机制依据见[微软启动说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)，范围及历史源码摘要见[预检登记](../../experiments/results/p6-native-preflight.json)。

扩展实验编译 [`security_probe.cs`](../../backend/src/repowatch/_native/security_probe.cs)，仅访问新建并正常结束删除的假凭据、进程环境标记及自建监听器，不读取真实凭据。专用账户最终检查已覆盖实际产品控制 API 前后 401，以及其连接和自建回环监听器的有界超时；普通进程前后均可达，监听端未收到隔离连接。有界超时不写为明确拒绝。超时按 PID 和创建时间核对；突然终止新建作业宿主后，活动成员由 3 降至 0。这个机制结果不替代正式产品命令的持久身份与恢复验收。

[`p6-native-session.ps1`](../../experiments/p6-native-session.ps1) 提供环境验收用的限时会话，最多运行 1800 秒，只允许 `native-qualification-v1` 的固定 `qualify` 请求，拒绝额外字段及任意命令／路径／脚本参数。准备阶段只让执行账户读取代码和请求、写入独立响应目录；就绪记录还须核对实际进程所有者。结果原子发布并按请求 ID 复用，不因用户口头报告“已启动”就视为进程存在。这个会话只运行固定实验，不是正式 Worker／原生执行后端，不接受仓库任务。

正式原生实现使用独立的 `repowatch-native-v1` 协议。`repowatch native-prepare` 要求新目录、专用本地账户 SID、完整 CPython 标准库目录及正在运行的控制 API `/api/v1/state`；创建受限 ACL、固定代码／工具链摘要和 `native-host.cmd`，不会自行启用。专用账户执行启动器并通过当前环境检查后，控制账户以 `repowatch native-bind --native-root <目录> --data-dir <验收库> --operation-id <唯一标识>` 显式绑定。任务配置选择 `execution_backend: native`，并可将 `workspace_image` 固定为准备器给出的运行时摘要；已冻结的摘要不匹配时拒绝。每次执行前复核进程身份、非管理员状态、创建时间、代码／运行时和权限。系统页显示当前核验结果，宿主停止或失效后下一次刷新撤销准入显示；配置开关本身不是能力证明。

原生 Runner 沿用任务执行权、累计预算和证据账本。每次从受控源码创建独立任务副本，工具链只读、任务副本可写；子进程挂起后先核验实际 AppContainer 令牌并持久化 PID／创建时间，再由控制端打开启动门。暂停、截止和恢复使用固定请求身份、取消标记及具有限定控制权限的命名作业；不得重放未核实的命令。当前实现范围为 Windows x64 上固定 CPython 标准库的离线 CLI；第三方依赖准备、非普通 Git 对象、不安全 Windows 路径等明确不可用，默认 Docker 的既有四类样本支持范围保留。完整产品样本、最终安装及故障回归的状态统一见 [NOW](../context/NOW.md)。

### 原生宿主独立验收

2026-11-10 已在本机专用普通账户与独立控制账户之间完成[原生产品样本](../../experiments/results/p6-native-product.json)：实际扫描、Issue #27、原生前后探针／回归、候选冻结、独审、[PR #28](https://github.com/Gwex323/repowatch-p3-sandbox/pull/28)及成功记忆。候选 `ff84ce7b73534877b781d74b5790260f442aab79` 通过独立编译及 CLI 执行，输出为 `5`；19 条产品命令均登记为原生并核实停止。运行于新安装 wheel，模型为 HTTP 夹具，付费模型调用为 0；旧失败、实际用量、有限恢复窗口和原累计预算均保留，没有通过重建账本消除失败。

本轮临时宿主在验收后已清洁停止；资格 API 已发出停止请求，并通过宿主 TCP 表核实不再监听，未另取其进程退出码。该结果不是永久在线准入；未来新实例须使用实际控制 API 地址准备／启动并显式绑定宿主，执行前仍重新核验。验收用临时配置不应直接当作长期运行配置。

隔离范围见[预检登记](../../experiments/results/p6-native-preflight.json)：实际 AppContainer、源码保护、受控句柄／环境、假凭据拒绝、真实 API 前后 401、网络有界不可达、超时及三成员进程树的宿主崩溃清理。网络有界超时不等同明确拒绝；这些测试也不证明任意宿主、依赖或真实模型效果。首次实现暴露的响应替换、源码读取单步预留、冻结时元数据及引用更新、成功终态信号均已修复并复验；失败历史保留供回查。

## 开发预览变更与验收限制

本轮在 P5 基础上增加完整 wheel 与锁定安装器、Windows API／Worker 监督启停、生产系统页、唯一迁移、跨写者维护屏障、一致备份／新目录恢复、恢复后的只读对账和受保护清理；补齐 PR 的 CI／评审／合并跳转及待核验远端引用输入。版本仍为 `0.1.0.dev0`；P6 实施与受控技术验收已闭合，最终用户签收和发布没有由阶段任务代替。最终安装包的本地运维 12 项、Edge 9 项及独立原生候选复验均通过；来源与补丁时效见 NOW。

2026-11-10 已在用户授权的两座 sandbox 中完成四类小型样本的真实 Git／Docker／GitHub 流程，模型继续用确定性 HTTP 夹具；TS 链路由已安装 wheel 执行，反馈修复后重新离线安装并续跑。付费模型未调用，原用户和 P3／P4 验收库未迁移。用户随后提供了本机专用账户并继续原生验收，当前结果见 [NOW](../context/NOW.md)，不再沿用此前缺少环境时的延期决定。任意仓库依赖及私有服务未验证；LFS 实体／子模块内容缺失继续阻塞，默认语义模型仍未过真实语料门槛，E12 不宣称生产提效。

最终回归、浏览器、安装包实测及未完成任务统一见 [NOW](../context/NOW.md)；[逐规格证据登记](../../experiments/results/p6-acceptance.json)列出 11 份规格、207 个场景、关联测试及 E1—E20 的受控证据和限制。关联模块测试不是每个场景独立执行的证明，也不替代真实模型与最终产品验收。复跑登记：

```powershell
backend/.venv/Scripts/python.exe -X utf8 experiments/p6-acceptance.py --junit .cache/p6-native-full.xml .cache/p6-native-qualified-unit.xml .cache/p6-native-terminal.xml .cache/p6-native-terminal-shared.xml .cache/p6-native-capability-view.xml .cache/p6-native-object-handoff.xml
```

## 首批适配样本与复跑

适配范围限于 `p6_samples.py` 中四份无需访问包注册表的小型样本：Python 标准库 CLI、Python HTTP Web、Node JavaScript CLI，以及实际 TypeScript 编译并由 Node 提供服务的 Web。四份均由产品执行扫描、选择候选、Issue 查重／创建、修复前后探针、回归、独审、PR 发布与记忆提炼；CLI 实际运行返回 `5`，两份 Web 通过 Edge 页面结果、刷新及运行错误检查。[脱敏报告](../../experiments/results/p6-adaptation.json)记录仓库身份、候选 SHA、镜像、构建／浏览器结果及失败恢复历史。失败经验仍可为 `pending_review`，不会冒充成功经验。

验收准备器只给既有 sandbox 新增 `p6/acceptance-20261110-…` 样本分支，产品创建 Issue／PR，操作者在确认回执后补 P6 PR 标题前缀；这三类动作分别登记。PR 保持开放，不自动合并或发布。现有凭据引用必须已由操作者在受保护凭据库中配置；脚本不能用于任意生产仓库，也不能仅因本文命令获得新一轮远端写入授权。

当前专用验收数据与模型夹具可复用；使用对应安装解释器替换下方路径可验证 wheel。`verify` 读取现有冻结候选并创建临时容器，不写远端；`run` 仍须具有当轮 GitHub 写入授权。示例：

```powershell
backend/.venv/Scripts/python.exe -X utf8 experiments/p6-github-acceptance.py verify python-web
backend/.venv/Scripts/python.exe -X utf8 experiments/p6-github-acceptance.py run javascript-cli --allow-github-writes
backend/.venv/Scripts/python.exe -X utf8 experiments/p6-adaptation.py
```

TS 编译镜像从本地锁定的 `frontend/node_modules/typescript` 制作并按内容摘要命名，构建使用 `--network=none`；不隐式下载工具。普通 Web／CLI 验证容器不挂宿主凭据或控制数据，Web 预览仅发布回环端口。退出后删除本轮预览容器及工作区卷，验收数据保持全局暂停。
