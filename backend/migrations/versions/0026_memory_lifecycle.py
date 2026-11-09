from alembic import op

revision = "0026_memory_lifecycle"
down_revision = "0025_repository_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE memory_versions (
        memory_id TEXT NOT NULL REFERENCES repository_memories(id),
        revision INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        PRIMARY KEY(memory_id,revision)
    )""")
    op.execute("""CREATE TABLE memory_heads (
        memory_id TEXT PRIMARY KEY REFERENCES repository_memories(id),
        revision INTEGER NOT NULL DEFAULT 1,
        content_revision INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL CHECK(state IN
            ('success','failure','pending_review','manual_unverified','feedback_pending')),
        prior_state TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        source_task_id TEXT REFERENCES tasks(id),
        feedback_revision INTEGER,
        reviewed_feedback_revision INTEGER
    )""")
    op.execute("""CREATE TABLE memory_index_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL REFERENCES repository_memories(id),
        revision INTEGER NOT NULL,
        indexed INTEGER NOT NULL DEFAULT 0 CHECK(indexed IN (0,1))
    )""")
    op.execute("""CREATE TABLE memory_shares (
        memory_id TEXT PRIMARY KEY REFERENCES repository_memories(id),
        source_revision INTEGER NOT NULL,
        content_revision INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        actor TEXT NOT NULL
    )""")
    op.execute("""CREATE TABLE memory_index_configuration (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        revision INTEGER NOT NULL DEFAULT 1,
        configuration_json TEXT NOT NULL DEFAULT '{}'
    )""")
    op.execute("INSERT INTO memory_index_configuration(singleton) VALUES(1)")
    op.execute("""CREATE TABLE memory_injections (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        stage_key TEXT NOT NULL,
        dependencies_json TEXT NOT NULL,
        block_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE memory_request_audits (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        injection_id TEXT NOT NULL REFERENCES memory_injections(id),
        dependencies_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    initialize = """INSERT INTO memory_versions VALUES(NEW.id,1,NEW.content_json);
        INSERT INTO memory_heads(memory_id,state,prior_state,source_task_id,feedback_revision,
            reviewed_feedback_revision)
        VALUES(NEW.id,NEW.state,NEW.state,json_extract(NEW.source_json,'$.task_id'),
            json_extract(NEW.source_json,'$.feedback_revision'),
            json_extract(NEW.source_json,'$.feedback_revision'));
        INSERT INTO memory_index_events(memory_id,revision) VALUES(NEW.id,1);"""
    op.execute(f"""CREATE TRIGGER memory_initialize AFTER INSERT ON repository_memories
        BEGIN {initialize} END""")
    op.execute("INSERT INTO memory_versions SELECT id,1,content_json FROM repository_memories")
    op.execute("""INSERT INTO memory_heads(memory_id,state,prior_state,source_task_id,
        feedback_revision,reviewed_feedback_revision)
        SELECT id,state,state,json_extract(source_json,'$.task_id'),
            json_extract(source_json,'$.feedback_revision'),
            json_extract(source_json,'$.feedback_revision') FROM repository_memories""")
    op.execute(
        "INSERT INTO memory_index_events(memory_id,revision) SELECT memory_id,1 FROM memory_heads"
    )
    op.execute("""UPDATE memory_heads SET state='feedback_pending',
        feedback_revision=(SELECT feedback_revision FROM tasks WHERE id=source_task_id)
        WHERE state IN ('success','failure','pending_review') AND source_task_id IS NOT NULL
        AND reviewed_feedback_revision IS NOT NULL
        AND reviewed_feedback_revision !=
            (SELECT feedback_revision FROM tasks WHERE id=source_task_id)
    """)
    op.execute("""CREATE TRIGGER memory_feedback AFTER UPDATE OF feedback_revision ON tasks
        WHEN NEW.feedback_revision != OLD.feedback_revision
        BEGIN
            UPDATE memory_heads SET revision=revision+1,
                prior_state=CASE WHEN state='feedback_pending' THEN prior_state ELSE state END,
                state='feedback_pending', feedback_revision=NEW.feedback_revision
            WHERE source_task_id=NEW.id AND deleted=0 AND enabled=1
                AND state IN ('success','failure','pending_review','feedback_pending');
            INSERT INTO memory_index_events(memory_id,revision)
                SELECT memory_id,revision FROM memory_heads
                WHERE source_task_id=NEW.id AND state='feedback_pending' AND deleted=0;
        END""")
    for table in ("memory_versions", "memory_injections", "memory_request_audits"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_{action.lower()}_immutable
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'memory history is immutable'); END""")


def downgrade():
    raise RuntimeError("记忆版本、删除标记和请求审计不自动降级")
