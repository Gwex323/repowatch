# 本地编码资产与检索降级（1.9）

日期：2026-11-04。范围：宿主 SQLite 的 FTS5／`sqlite-vec` 加载验证，三种本地 CPU 嵌入模型在 22 条中文／代码语料、12 条查询上的许可证、离线加载、召回与资源评测，以及编码规则和关键词降级路径。它是 P0 选型实验，不是产品检索实现，也不替代 P3 的真实仓库评测。

## 真实 Issue／PR 复评与资产锁定

核验日期：2026-11-09。用户授权只读获取真实语料及锁定编码运行时。本轮使用 `Gwex323/repowatch-p3-sandbox` 的 Issue #1／#3／#15／#17 和 PR #2／#4／#16／#18，共 8 条标题与完整正文，排除重复中间 Issue；未写入 GitHub。这是远端真实记录，但内容仍是受控算术夹具，不能代表生产仓库。

固定语料、来源 URL、远端更新时间、采集时间及 8 条人工相关性标注查询保存在 [语料快照](../../experiments/fixtures/embedding-real.json)，其中中文与代码查询各 4 条。每条查询指定一个目标记录；未将关联 Issue／PR 自动视为同一相关项。语料文件、索引正文与查询、依赖锁、资产锁及执行代码摘要见[复评报告](../../experiments/results/embedding-real-20261109.json)。评测不调参、不按结果替换查询，保留未命中及目标排名。

| 候选 | 中文 recall@3 | 代码 recall@3 | 总体 MRR | 本轮结论 |
| --- | --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5` | 0.50 | 1.00 | 0.734375 | 中文未过 0.75 门槛 |
| `jinaai/jina-embeddings-v2-base-zh` | 0.50 | 1.00 | 0.743750 | 中文未过 0.75 门槛 |
| `jinaai/jina-embeddings-v2-base-code` | 0.50 | 1.00 | 0.737500 | 中文未过门槛，且未声明中文支持 |

三者均完成 CPU 离线加载、编码、再次离线加载及 sqlite-vec 查询；独立进程重跑得到相同召回结果。两条查询要求识别直接／Fork 发布的 PR，原 PR 标题与正文主要是发布模板，未进入前三。此观察不证明模型能否检索更广泛的生产问题。按原有门槛，本轮 `selection.model=null`、`fallback=keyword`；P0 的中文主选只保留为历史结果，当前没有通过复评的默认语义模型。关键词降级是既定实现路线，尚非已完成的产品检索。

编码运行时使用 [pyproject](../../backend/pyproject.toml) 的非默认 `embedding` 依赖组：FastEmbed 0.9.1、ONNX Runtime 1.31.1、psutil 7.2.2、sqlite-vec 0.1.9；[uv.lock](../../backend/uv.lock) 固定全部传递依赖及分发包摘要。已实际按锁安装并离线核对声明一致；不再依赖 `.cache/embedding-eval` 中未锁定的评测环境。

[模型资产锁](../../experiments/embedding-assets.lock.json) 固定三个候选的来源仓库、完整 revision 和全部缓存文件 SHA-256（包括 ONNX、tokenizer 与配置）。中文候选 revision 为 `c1ff9086a89a1123d7b5eff58055a665db4fb4b9`，ONNX SHA-256 为 `4b0e9fa6e5c77cff56e0c9c673ba1aad61e793e592fdd4b05690b68826b7d3a2`。复评先检查完整文件清单与摘要，再以 `specific_model_path` 指向固定快照，禁止隐式下载；文件缺失、增加或篡改均拒绝加载。`lock` 动作仅供显式重新锁定，不应在复跑前自动运行以掩盖变化。资产未纳入 Git，其他设备须另行准备与锁文件一致的资产，本次未验证全新设备分发。

复跑（已有匹配资产）：

```powershell
uv sync --project backend --locked --group embedding --group dev
$env:HF_HUB_OFFLINE='1'
backend/.venv/Scripts/python.exe experiments/embedding-real.py evaluate
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_embedding_probe.py backend/tests/test_embedding_assets.py -q
```

本轮专项 7 项通过，Ruff 检查／格式通过；未执行全后端、浏览器、生产记忆集成、E10／E12／E16 或真实付费模型。本轮不是 10.9 的至少 30 项时序／仓库隔离对照集；生产召回、混合检索与结构化关联、并发内存限制仍待 P5 验证。以下章节为 P0 历史实验详情，当前结论以本节为准。

## 组件结果

宿主使用 SQLite 3.46.0，`sqlite-vec` 0.1.9。验证结果：

- FTS5 可创建，`unicode61` 与 `trigram` 两种 tokenizer 均可建表、写入和查询。
- `vec0` 可加载，`float[N]` 列支持插入与 KNN 查询；本轮另验证 `distance_metric=cosine` 可用，产品检索使用 L2 归一化后的余弦距离。
- `unicode61` 不切分中文，中文短语查询返回 0；`trigram` 对 3 字及以上中文子串可命中（如“令牌刷新失败”“数据库迁移”），2 字查询（如“登录”）不命中。
- `LIKE '%词%'` 可兜底精确子串（“登录”命中 1 条），但对语义改写无效：“用户被强制退出”在两种 tokenizer 和 `LIKE` 下均为 0。

原始结果见报告 `experiments/results/local/embedding-20261104T005203865138Z.json` 的 `components` 字段。

## 候选与评测

评测语料为 22 条中文／代码混合记录，查询 12 条（6 条中文、6 条代码语义），语料摘要 `sha256:8fa07fe4…`。模型通过 FastEmbed 0.9.1 + ONNX Runtime 在 CPU、4 线程下加载，文档用 `embed`、查询用 `query_embed`，向量 L2 归一化后按余弦排序。加载与编码耗时取本机单次测量，RSS 为进程加载后常驻内存。

| 模型 | 许可证 | 维度 | 体积 | 加载 | 离线加载 | 编码 22 文档＋12 查询 | 进程 RSS | 中文 recall@3／MRR | 代码 recall@3／MRR | 总体 recall@3／MRR | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5` | MIT | 512 | 0.09GB | 0.17s | 0.14s | 0.08s | 184MB | 1.000／0.917 | 0.333／0.327 | 0.667／0.622 | 代码召回低于门槛，淘汰 |
| `jinaai/jina-embeddings-v2-base-zh` | Apache-2.0 | 768 | 0.64GB | 1.29s | 1.25s | 0.36s | 729MB | 1.000／0.889 | 0.833／0.848 | 0.917／0.869 | 主选 |
| `jinaai/jina-embeddings-v2-base-code` | Apache-2.0 | 768 | 0.64GB | 1.35s | 1.25s | 0.51s | 743MB | 1.000／0.917 | 1.000／0.778 | 1.000／0.847 | 代码备选 |

入选门槛为中文与代码 recall@3 均不低于 0.75、离线加载成功、模型声明支持中文；通过门槛的候选按总体 MRR、recall@3、加载时间和体积排序。`bge-small-zh` 中文可用但代码语义明显不足，且其 FastEmbed 来源为 `Qdrant/bge-small-zh-v1.5` 优化版而非原仓库，不作为默认资产。

主选 `jina-embeddings-v2-base-zh` 的理由是中文优先的产品语料、模型卡声明中文支持、代码召回 0.833 已过门槛，且总体 MRR 最高。备选 `jina-embeddings-v2-base-code` 的候选召回为 1.000，但模型卡只声明英文与 30 种编程语言，不声明中文；其代码查询 MRR 0.778 也低于中文模型的 0.848。只有确认语料以代码为主并重新评测中文后，才切换该备选。

该语料仅用于筛掉明显不合格的资产，不能证明生产召回率。P3 接入真实 Issue／PR 语料后必须重跑同一评测，并记录语料版本、查询集与结果，不得沿用本轮数字作为产品指标。

## 编码与索引契约

- 文档编码调用 `TextEmbedding.embed`，查询编码调用 `TextEmbedding.query_embed`；两者都不加任务前缀（模型卡声明不需要前缀）。
- 向量统一做 L2 归一化，`vec0` 使用 `distance_metric=cosine`；不混用未归一化向量或不同距离度量。
- 批量大小 8、CPU 线程 4；这是评测值，生产并发和批大小需在 P3 按延迟与内存重新标定。
- 每条索引记录至少保存：模型名、许可证、维度、归一化方式、距离度量、模型来源（`sources.hf`）、模型文件、语料摘要。当前候选的模型文件分别为 `model_optimized.onnx`（bge）与 `onnx/model.onnx`（两个 Jina 模型）。
- 维度变化必须重建 `vec0` 表并提升索引版本；同一张向量表不允许混用不同模型或维度。模型或语料摘要变化时，旧索引标记失效，不静默复用。
- 模型缓存位于已忽略的 `.cache/fastembed`，离线加载用 `local_files_only=True` 验证通过。生产资产需固定模型版本并校验文件摘要，运行时不隐式下载；具体校验与分发方式待 P1／P3 实施。
- 本轮实测单进程 RSS 为 184—743MB，处于 16GB 基线可接受范围；未测 32GB 以上的并发余量、换页或与 API／Worker 同机运行的峰值。生产需为编码进程设置独立内存上限并验证超限降级。

`experiments/run.py embedding` 会把上述编码字段写入报告；`embedding-contracts` 固化组件加载、中文短词、`LIKE` 兜底和选型门槛。

## 关键词降级

以下情况必须走关键词路径，不得把“无结果”解释为“无相关问题”：

- 没有通过门槛的嵌入模型，或模型／缓存资产缺失、加载失败。
- 索引版本、维度或语料摘要与当前配置不一致，且尚未完成重建。
- 查询语言或长度不适合向量检索时仍可先走关键词，例如 2 字中文词。

关键词路径规则：

- ASCII 标识符（如 `create_app`）用 `unicode61`；中文 3 字及以上用 `trigram`。
- 2 字中文用 `LIKE '%词%'`，范围限制在当前仓库／候选集，避免全表扫描；该路径只保证精确子串，不保证语义召回。
- 向量与关键词结果合并时按业务记录 ID 去重，保留两路来源标记；不直接把 BM25 分与余弦分相加。语义改写查询在关键词路径无命中时，应显示“未找到”并保留人工检索入口，不伪造关联。
- 关键词降级不自动切换到云端嵌入或外部检索；需要外部服务时必须由显式配置和用户授权启用。

## 运行

```powershell
backend/.venv/Scripts/python.exe experiments/run.py embedding-contracts
backend/.venv/Scripts/python.exe experiments/run.py embedding --model BAAI/bge-small-zh-v1.5 --model jinaai/jina-embeddings-v2-base-zh --model jinaai/jina-embeddings-v2-base-code
```

P0 当时的依赖位于已忽略的 `.cache/embedding-eval`，未写入锁文件。2026-11-09 起使用上方锁定的可选依赖组与固定资产；评测加载仅使用本地文件。旧入口仍保留 P0 语料，真实语料复评使用上方 `embedding-real.py evaluate`，不混用两类报告。

## 限制

- 语料和查询为人工构造的小样本，只覆盖单一中文／代码混合场景；没有真实 Issue 长文本、多语言、代码片段或权限过滤。
- 只测单进程、单次加载和 4 线程编码，没有并发、冷启动抖动、长时间运行内存增长或 16／32GB 边界的压力结果。
- `vec0` 为暴力近邻，未验证大规模索引、分片、增量重建或删除后的召回一致性。
- 未实现产品检索、索引迁移、权限过滤、结果合并 UI 或人工反馈闭环；这些属于后续阶段。
- 未验证模型许可证的法律意见，只记录模型卡声明的许可证；发布前仍需按分发方式复核。
