import json
from types import SimpleNamespace

import pytest
from test_artifacts import kernel as kernel
from test_repository_memory import CONTENT, source

from repowatch.adapters.memory_index import MemoryIndex, keywords
from repowatch.application.memory import RepositoryMemory
from repowatch.application.memory_context import MemoryContext, MemoryContextChanged
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords


def setup(kernel, content=CONTENT):
    settings, store, _, _, artifacts, _ = kernel
    task, _ = source(kernel)
    memory = RepositoryMemory(store, artifacts)
    identifier = memory.record_verified("memory-intent", content, now=0)
    return memory, MemoryIndex(settings, memory), task, identifier


def operate(memory, identifier, action, data=None):
    revision = memory.get(identifier)["revision"]
    return memory.operate(
        f"{action}:{revision}",
        identifier,
        expected_revision=revision,
        action=action,
        data=data or {},
    )


def test_atomic_index_event_and_rebuild_chinese_and_identifiers(kernel):
    memory, index, task, identifier = setup(kernel)
    with memory.store.read() as records:
        assert len(records.find("memory_index_events")) == 1
    assert "create" in keywords("create_app")
    assert "app" in keywords("createApp")
    assert "登录" in keywords("登录失败")
    assert index.search(task["repository_id"], "乘法")["items"][0]["id"] == identifier
    assert index.rebuild()["mode"] == "keyword"
    assert index.search(task["repository_id"], "乘法")["items"][0]["id"] == identifier
    index.settings.index_database.write_bytes(b"not a database")
    assert index.status()["degradation"] == "index_corrupt"
    assert index.rebuild()["count"] == 1
    assert index.search(task["repository_id"], "乘法")["items"]


@pytest.mark.parametrize("action", ["disable", "delete", "edit"])
def test_stale_indices_and_context_cannot_reuse_invalid_memory(kernel, action):
    memory, index, task, identifier = setup(kernel)
    index.rebuild()
    services = SimpleNamespace(
        store=memory.store,
        artifacts=memory.artifacts,
        settings=index.settings,
        clock=lambda: 0,
        memory_index=index,
    )
    context = MemoryContext(services)
    injection = context.prepare(kernel[-1], "test", "乘法", "implement")
    assert injection["dependencies"][0]["memory_id"] == identifier
    context.guard(kernel[-1], injection, "request-before-change")
    operate(memory, identifier, action, {"content": {**CONTENT, "method": "改变语义"}})
    assert index.search(task["repository_id"], "乘法")["items"] == []
    with pytest.raises(MemoryContextChanged):
        context.guard(kernel[-1], injection, "must-not-send")
    clean = context.prepare(kernel[-1], "test", "乘法", "implement")
    assert clean["id"] != injection["id"]
    assert not clean["dependencies"]
    with memory.store.read() as records:
        assert len(records.find("memory_request_audits")) == 1


def test_cross_repository_only_returns_explicit_sanitized_body(kernel):
    memory, index, task, identifier = setup(kernel)
    other = TaskRecords(memory.store).register_repository("other", admission_mode="all_bugs")
    assert not index.search(other, "乘法")["items"]
    shared = {**CONTENT, "limitations": "只共享一般方法，不包含原始证据"}
    operate(memory, identifier, "share", {"content": shared, "confirmed_sanitized": True})
    index.rebuild()
    item = index.search(other, "乘法")["items"][0]
    assert item["visibility"] == "shared"
    assert item["source"] == {"memory_id": identifier}
    assert "repository_id" not in item
    assert "artifact" not in json.dumps(item)
    assert index.search(task["repository_id"], "乘法")["items"][0]["visibility"] == "repository"
    operate(memory, identifier, "disable")
    assert not index.search(other, "乘法")["items"]


def test_feedback_rechecks_all_versions_and_shared_derivative(kernel):
    memory, index, task, identifier = setup(kernel)
    other = TaskRecords(memory.store).register_repository("other", admission_mode="all_bugs")
    operate(memory, identifier, "share", {"content": CONTENT, "confirmed_sanitized": True})
    index.rebuild()
    TaskRecords(memory.store).observe(
        task["id"], "review", "1", {"changes": "format"}, kind="feedback", now=0
    )
    pending = memory.get(identifier)
    assert pending["state"] == "feedback_pending"
    assert not index.search(other, "乘法")["items"]
    memory.review_feedback(
        identifier,
        expected_revision=pending["revision"],
        feedback_revision=pending["feedback_revision"],
        unrelated=True,
        source_sha256=pending["source"]["source_sha256"],
    )
    index.rebuild()
    assert index.search(other, "乘法")["items"]
    TaskRecords(memory.store).observe(
        task["id"], "review", "2", {"changes": "real failure"}, kind="feedback", now=0
    )
    pending = memory.get(identifier)
    operate(memory, identifier, "delete")
    with pytest.raises(ConflictError):
        memory.review_feedback(
            identifier,
            expected_revision=pending["revision"],
            feedback_revision=pending["feedback_revision"],
            unrelated=True,
            source_sha256=pending["source"]["source_sha256"],
        )
    assert not index.search(other, "乘法")["items"]
    assert memory.record_verified("memory-intent", CONTENT, now=0) == identifier
    assert memory.get(identifier)["deleted"]


class Encoder:
    def __init__(self, configuration):
        self.configuration = configuration
        self.asset = configuration

    def encode(self, texts, *, query=False):
        dimension = self.configuration.get("dimension", 3)
        return [[1.0, *([0.0] * (dimension - 1))] for _text in texts]


def configure(index, configuration):
    with index.memory.store.write() as records:
        row = records.one("memory_index_configuration", singleton=1)
        records.change(
            "memory_index_configuration",
            {"singleton": 1},
            {"revision": row["revision"] + 1, "configuration_json": json.dumps(configuration)},
        )


def test_versioned_sqlite_vectors_rebuild_after_dimension_or_rule_change(kernel):
    memory, original, task, identifier = setup(kernel)
    index = MemoryIndex(original.settings, memory, encoder_factory=Encoder)
    configure(index, {"model": "fixture", "dimension": 3})
    assert index.rebuild()["dimension"] == 3
    assert index.search(task["repository_id"], "different semantics")["mode"] == "hybrid"
    assert (
        index.search(task["repository_id"], "different semantics")["items"][0]["id"] == identifier
    )
    configure(index, {"model": "fixture", "dimension": 5, "encoding": "v2"})
    assert index.status()["mode"] == "keyword"
    assert index.rebuild()["dimension"] == 5
    restarted = MemoryIndex(index.settings, memory, encoder_factory=Encoder)
    assert restarted.search(task["repository_id"], "different semantics")["mode"] == "hybrid"
    configure(index, {"model": None})
    assert index.rebuild()["mode"] == "keyword"


def test_content_edits_do_not_inherit_validation_and_operations_replay(kernel):
    memory, _, _, identifier = setup(kernel)
    data = {"content": {**CONTENT, "method": "人工语义编辑"}}
    revision = memory.get(identifier)["revision"]
    result = memory.operate(
        "edit-op", identifier, expected_revision=revision, action="edit", data=data
    )
    assert memory.get(identifier)["state"] == "manual_unverified"
    assert (
        memory.operate("edit-op", identifier, expected_revision=revision, action="edit", data=data)
        == result
    )
    with pytest.raises(ConflictError):
        memory.operate("another", identifier, expected_revision=revision, action="delete", data={})


def test_index_lag_does_not_hide_new_facts_or_mix_scope(kernel):
    memory, index, task, identifier = setup(kernel)
    index.rebuild()
    with memory.store.write() as records:
        records.add("memory_index_events", {"memory_id": identifier, "revision": 1})
    assert index.status()["pending"]
    assert index.search(task["repository_id"], "乘法")["items"]


def test_encoding_change_rejects_both_old_vector_and_keyword_rules(kernel, monkeypatch):
    memory, index, task, identifier = setup(kernel)
    index.rebuild()
    monkeypatch.setattr("repowatch.adapters.memory_index.ENCODING_VERSION", "test-next-encoding")
    assert index.status()["mode"] == "range"
    assert index.search(task["repository_id"], "乘法")["items"][0]["id"] == identifier
    assert index.rebuild()["encoding"] == "test-next-encoding"


def test_manual_operation_id_cannot_be_reused_for_another_repository(kernel):
    memory, _, task, _ = setup(kernel)
    other = TaskRecords(memory.store).register_repository("other", admission_mode="all_bugs")
    first = memory.record_manual(
        task["repository_id"], "stable-key", CONTENT, now=0, operation_id="same-operation"
    )
    assert (
        memory.record_manual(
            task["repository_id"], "stable-key", CONTENT, now=0, operation_id="same-operation"
        )
        == first
    )
    with pytest.raises(ConflictError):
        memory.record_manual(other, "stable-key", CONTENT, now=0, operation_id="same-operation")


def test_upgrade_preserves_pending_feedback_for_preexisting_memory(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    from repowatch.adapters.storage import sqlite
    from repowatch.configuration import StorageSettings
    from repowatch.operations.migrate import migrate, migration_directory

    settings = StorageSettings(tmp_path / "upgrade")
    settings.data_dir.mkdir()
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_directory()))
    engine = sqlite.sqlite_engine(settings, create=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "0025_repository_memory")
    engine.dispose()
    monkeypatch.setattr(sqlite, "SCHEMA_REVISION", "0025_repository_memory")
    old = sqlite.SQLiteBusinessStore(settings)
    tasks = TaskRecords(old)
    repository = tasks.register_repository("old-memory", admission_mode="all_bugs")
    task = tasks.ensure_task(repository, "old-memory-task", now=0)
    with old.write() as records:
        records.add(
            "repository_memories",
            {
                "id": "old-memory",
                "repository_id": repository,
                "source_key": "old-source",
                "state": "success",
                "content_json": json.dumps(CONTENT),
                "source_json": json.dumps({"task_id": task, "feedback_revision": 1}),
                "created_at": 0,
            },
        )
        records.change("tasks", {"id": task}, {"feedback_revision": 2})
    old.close()
    monkeypatch.setattr(sqlite, "SCHEMA_REVISION", "0026_memory_lifecycle")
    migrate(settings)
    new = sqlite.SQLiteBusinessStore(settings)
    try:
        with new.read() as records:
            head = records.one("memory_heads", memory_id="old-memory")
            assert head["state"] == "feedback_pending"
            assert head["feedback_revision"] == 2
            assert head["reviewed_feedback_revision"] == 1
            assert records.find("memory_index_events", memory_id="old-memory")
    finally:
        new.close()


def test_product_fts_returns_chinese_and_code_identifier_samples(kernel):
    memory, index, task, identifier = setup(
        kernel,
        {**CONTENT, "symptoms": "登录失败", "method": "核对 create_app 与 createApp 的初始化顺序"},
    )
    index.rebuild()
    for query in ("登录", "create_app", "createApp"):
        result = index.search(task["repository_id"], query)
        assert result["mode"] == "keyword"
        assert result["items"][0]["id"] == identifier
    assert index.search(task["repository_id"], "登录", max_chars=256)["items"] == []


def test_memory_and_index_events_roll_back_together(kernel, monkeypatch):
    memory, _, task, _ = setup(kernel)
    with memory.store.read() as records:
        before = len(records.find("memory_index_events"))

    def fail_event(*args, **kwargs):
        raise RuntimeError("受控事务中断")

    monkeypatch.setattr("repowatch.application.memory.business_event", fail_event)
    with pytest.raises(RuntimeError):
        memory.record_manual(task["repository_id"], "must-roll-back", CONTENT, now=0)
    with memory.store.read() as records:
        assert len(records.find("repository_memories")) == 1
        assert len(records.find("memory_heads")) == 1
        assert len(records.find("memory_versions")) == 1
        assert len(records.find("memory_index_events")) == before


def test_sharing_requires_explicit_sanitized_confirmation(kernel):
    memory, _, _, identifier = setup(kernel)
    revision = memory.get(identifier)["revision"]
    with pytest.raises(PermissionError):
        memory.operate(
            "unconfirmed",
            identifier,
            expected_revision=revision,
            action="share",
            data={"content": CONTENT},
        )
    with pytest.raises(ValueError):
        memory.operate(
            "raw-url",
            identifier,
            expected_revision=revision,
            action="share",
            data={
                "content": {**CONTENT, "method": "https://private.example/raw"},
                "confirmed_sanitized": True,
            },
        )


def test_invalid_candidates_are_filtered_before_vector_top_k(kernel):
    memory, original, task, _ = setup(kernel)
    with memory.store.write() as records:
        for index in range(101):
            records.add(
                "repository_memories",
                {
                    "id": f"a-stale-{index}",
                    "repository_id": task["repository_id"],
                    "source_key": f"stale-{index}",
                    "state": "success",
                    "content_json": json.dumps(CONTENT),
                    "source_json": "{}",
                    "created_at": 0,
                },
            )
        records.add(
            "repository_memories",
            {
                "id": "z-valid",
                "repository_id": task["repository_id"],
                "source_key": "valid",
                "state": "success",
                "content_json": json.dumps(CONTENT),
                "source_json": "{}",
                "created_at": 0,
            },
        )
    index = MemoryIndex(original.settings, memory, encoder_factory=Encoder)
    configure(index, {"model": "fixture"})
    index.rebuild()
    with memory.store.write() as records:
        for head in records.find("memory_heads"):
            if head["memory_id"] != "z-valid":
                records.change("memory_heads", {"memory_id": head["memory_id"]}, {"enabled": 0})
    assert index.status()["pending"] == 0
    result = index.search(task["repository_id"], "unrelated semantic query", limit=1)
    assert result["mode"] == "hybrid"
    assert result["items"][0]["id"] == "z-valid"
