import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from repowatch.domain.task_records import content_digest

ENCODING_VERSION = "l2-cosine-keywords-v1"


def keywords(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    result = re.findall(r"[a-z0-9_]+", text.lower())
    result += [part for token in result.copy() for part in token.split("_") if part != token]
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(phrase[index : index + 2] for index in range(max(1, len(phrase) - 1)))
    return sorted(set(result))


class LockedEncoder:
    def __init__(self, asset, cache):
        from fastembed import TextEmbedding

        root = Path(cache) / ("models--" + asset["source"].replace("/", "--"))
        snapshot = root / "snapshots" / asset["revision"]
        actual = {
            path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()
        }
        if actual != set(asset["files"]):
            raise ValueError("编码资产缺失或清单不匹配")
        for name, expected in asset["files"].items():
            with (snapshot / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("编码资产摘要不匹配")
        self.asset = asset
        self.model = TextEmbedding(
            model_name=asset["model"],
            threads=4,
            local_files_only=True,
            specific_model_path=str(snapshot),
        )

    def encode(self, texts, *, query=False):
        from repowatch.runtime.embedding_probe import normalize

        method = self.model.query_embed if query else self.model.embed
        return [normalize(value) for value in method(texts, batch_size=8)]


class MemoryIndex:
    def __init__(self, settings, memory, *, encoder_factory=None):
        self.settings, self.memory = settings, memory
        self.encoder_factory = encoder_factory

    def configuration(self):
        with self.memory.store.read() as records:
            return dict(records.one("memory_index_configuration", singleton=1))

    def encoder(self, configuration):
        if not configuration.get("model"):
            return None
        if self.encoder_factory:
            return self.encoder_factory(configuration)
        bundled = Path(__file__).resolve().parents[1] / "_embedding-assets.lock.json"
        manifest = (
            bundled
            if bundled.is_file()
            else (Path(__file__).resolve().parents[4] / "experiments/embedding-assets.lock.json")
        )
        assets = json.loads(manifest.read_text("utf-8"))["models"]
        asset = next(item for item in assets if item["model"] == configuration["model"])
        return LockedEncoder(asset, self.settings.data_dir / "embedding-assets")

    @contextmanager
    def connect(self, *, read_only=False):
        target = self.settings.index_database
        if target.exists() and any(
            path.exists() and target.samefile(path)
            for path in (self.settings.business_database, self.settings.checkpoint_database)
        ):
            raise ValueError("派生索引不能指向业务库或检查点")
        connection = (
            sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)
            if read_only
            else sqlite3.connect(target, timeout=5)
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if read_only:
                connection.execute("PRAGMA query_only=ON")
                yield connection
                return
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS entries (
                document_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                repository_id TEXT NOT NULL, visibility TEXT NOT NULL, body TEXT NOT NULL,
                embedding BLOB)""")
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(document_id UNINDEXED,body)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def rebuild(self):
        try:
            return self._rebuild()
        except sqlite3.DatabaseError as error:
            if getattr(error, "sqlite_errorcode", None) not in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                raise
            suffix = ".corrupt-" + uuid4().hex
            for ending in ("", "-wal", "-shm"):
                path = Path(str(self.settings.index_database) + ending)
                if not path.resolve().is_relative_to(self.settings.data_dir):
                    raise ValueError("索引恢复路径不在受管数据目录") from None
                if path.is_file():
                    path.rename(path.with_name(path.name + suffix))
            return self._rebuild()

    def _rebuild(self):
        configuration = self.configuration()
        config = json.loads(configuration["configuration_json"])
        documents = []
        with self.memory.store.read() as records:
            events = records.find("memory_index_events")
            maximum = max((item["id"] for item in events), default=0)
            for row in records.find("repository_memories"):
                local = self.memory.eligible(records, row["id"], row["repository_id"])
                if not local:
                    continue
                body = "\n".join(local["content"].values())
                documents.append(
                    (
                        row["id"] + ":local",
                        row["id"],
                        local["revision"],
                        row["repository_id"],
                        "repository",
                        body,
                    )
                )
                shared = records.one("memory_shares", memory_id=row["id"])
                if shared and shared["content_revision"] == local["content_revision"]:
                    documents.append(
                        (
                            row["id"] + ":shared",
                            row["id"],
                            local["revision"],
                            row["repository_id"],
                            "shared",
                            "\n".join(json.loads(shared["content_json"]).values()),
                        )
                    )
        vectors, dim, error, asset_digest = [], 0, None, None
        try:
            encoder = self.encoder(config)
            if encoder and documents:
                asset_digest = content_digest(getattr(encoder, "asset", config))
                vectors = encoder.encode([item[-1] for item in documents])
                if len(vectors) != len(documents):
                    raise ValueError("编码结果数量不符")
                dim = len(vectors[0])
                if not dim or any(len(item) != dim for item in vectors):
                    raise ValueError("编码维度不一致")
        except Exception as problem:
            error = type(problem).__name__
            vectors, dim = [], 0
        with self.connect() as connection:
            connection.execute("DELETE FROM entries")
            connection.execute("DELETE FROM terms")
            if vectors:
                try:
                    import sqlite_vec

                    connection.enable_load_extension(True)
                    sqlite_vec.load(connection)
                    vectors = [sqlite_vec.serialize_float32(item) for item in vectors]
                except Exception as problem:
                    error, vectors, dim = type(problem).__name__, [], 0
            for index, document in enumerate(documents):
                connection.execute(
                    "INSERT INTO entries VALUES(?,?,?,?,?,?,?)",
                    (*document, vectors[index] if vectors else None),
                )
                connection.execute(
                    "INSERT INTO terms VALUES(?,?)", (document[0], " ".join(keywords(document[-1])))
                )
            status = {
                "configuration": configuration,
                "encoding": ENCODING_VERSION,
                "dimension": dim,
                "mode": "hybrid" if vectors else "keyword",
                "degradation": error or (None if vectors else "semantic_unavailable"),
                "count": len(documents),
                "max_event": maximum,
                "asset_digest": asset_digest,
            }
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES('active',?)", (json.dumps(status),)
            )
        with self.memory.store.write() as records:
            for item in records.find("memory_index_events", indexed=0):
                if item["id"] <= maximum:
                    records.change("memory_index_events", {"id": item["id"]}, {"indexed": 1})
        return status

    def status(self):
        with self.memory.store.read() as records:
            pending = len(records.find("memory_index_events", indexed=0))
            latest = max((row["id"] for row in records.find("memory_index_events")), default=0)
        if not self.settings.index_database.exists():
            return {"mode": "range", "degradation": "index_missing", "pending": pending}
        try:
            with self.connect(read_only=True) as connection:
                if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='metadata'"
                ).fetchone():
                    return {"mode": "range", "degradation": "index_empty", "pending": pending}
                row = connection.execute("SELECT value FROM metadata WHERE key='active'").fetchone()
            status = json.loads(row[0]) if row else {"mode": "range", "degradation": "index_empty"}
            if status.get("configuration") != self.configuration():
                status.update(mode="keyword", degradation="configuration_changed_rebuild_required")
            if status.get("encoding") != ENCODING_VERSION:
                status.update(mode="range", degradation="encoding_changed_rebuild_required")
            pending = max(pending, latest - status.get("max_event", 0))
            return {**status, "pending": pending}
        except sqlite3.Error:
            return {"mode": "range", "degradation": "index_corrupt", "pending": pending}

    def search(self, repository_id, query, *, limit=5, max_chars=6000):
        if not isinstance(query, str) or len(query) > 16000 or not 1 <= limit <= 20:
            raise ValueError("检索查询或条数无效")
        if not 256 <= max_chars <= 16000:
            raise ValueError("上下文长度必须有界")
        tokens = keywords(query)
        rankings = []
        status = self.status()
        allowed = set()
        with self.memory.store.read() as records:
            for row in records.find("repository_memories"):
                current = self.memory.eligible(records, row["id"], repository_id)
                if current:
                    allowed.add((current["id"], current["revision"], current["visibility"]))
        try:
            if status["mode"] != "range" and tokens:
                with self.connect(read_only=True) as connection:
                    connection.create_function(
                        "memory_current",
                        3,
                        lambda identifier, revision, visibility: int(
                            (identifier, revision, visibility) in allowed
                        ),
                    )
                    rows = connection.execute(
                        """SELECT entries.*,bm25(terms) AS score FROM terms
                        JOIN entries USING(document_id) WHERE terms MATCH ?
                        AND ((repository_id=? AND visibility='repository')
                        OR (repository_id!=? AND visibility='shared'))
                        AND memory_current(memory_id,revision,visibility)=1
                        ORDER BY score,document_id LIMIT 100""",
                        (
                            " OR ".join('"' + item.replace('"', '""') + '"' for item in tokens),
                            repository_id,
                            repository_id,
                        ),
                    ).fetchall()
                    rankings.append([dict(row) for row in rows])
                    if status["mode"] == "hybrid":
                        try:
                            encoder = self.encoder(
                                json.loads(self.configuration()["configuration_json"])
                            )
                            if (
                                content_digest(
                                    getattr(
                                        encoder,
                                        "asset",
                                        json.loads(self.configuration()["configuration_json"]),
                                    )
                                )
                                != status["asset_digest"]
                            ):
                                raise ValueError("编码资产版本变化，必须重建索引")
                            import sqlite_vec

                            connection.enable_load_extension(True)
                            sqlite_vec.load(connection)
                            vector = encoder.encode([query], query=True)[0]
                            if len(vector) != status["dimension"]:
                                raise ValueError("查询编码维度与索引不匹配")
                            rows = connection.execute(
                                """SELECT *,vec_distance_cosine(embedding,?)
                                AS distance FROM entries WHERE embedding IS NOT NULL
                                AND ((repository_id=? AND visibility='repository')
                                OR (repository_id!=? AND visibility='shared'))
                                AND memory_current(memory_id,revision,visibility)=1
                                ORDER BY distance,document_id LIMIT 100""",
                                (
                                    sqlite_vec.serialize_float32(vector),
                                    repository_id,
                                    repository_id,
                                ),
                            ).fetchall()
                            rankings.append([dict(row) for row in rows])
                        except Exception as problem:
                            status.update(mode="keyword", degradation=type(problem).__name__)
        except sqlite3.Error:
            status.update(mode="range", degradation="index_corrupt")
        scores, candidates = {}, {}
        for ranking in rankings:
            for rank, row in enumerate(ranking, 1):
                identifier = row["memory_id"]
                candidates[identifier] = row
                scores[identifier] = scores.get(identifier, 0) + 1 / (60 + rank)
        with self.memory.store.read() as records:
            if not rankings or status.get("pending", 0):
                for row in records.find("repository_memories"):
                    current = self.memory.eligible(records, row["id"], repository_id)
                    if (
                        current
                        and tokens
                        and set(tokens) & set(keywords(" ".join(current["content"].values())))
                    ):
                        candidates[row["id"]] = {"revision": current["revision"]}
                        scores.setdefault(row["id"], 1 / 160)
            output, length = [], 0
            for identifier in sorted(candidates, key=lambda key: (-scores[key], key)):
                current = self.memory.eligible(
                    records, identifier, repository_id, candidates[identifier]["revision"]
                )
                if not current:
                    continue
                text = canonical_result(current)
                if length + len(text) > max_chars:
                    continue
                output.append(current)
                length += len(text)
                if len(output) == limit:
                    break
        return {
            "items": output,
            "mode": status["mode"],
            "degradation": status.get("degradation"),
            "warning": "经验仅供参考；失败经验仅作风险提示，当前任务仍须独立验证",
        }


def canonical_result(item):
    return json.dumps(
        {key: item[key] for key in ("id", "revision", "content", "visibility", "state")},
        ensure_ascii=False,
        sort_keys=True,
    )
