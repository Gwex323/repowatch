from alembic import op

revision = "0025_repository_memory"
down_revision = "0024_pr_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE repository_memories (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        source_key TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        state TEXT NOT NULL CHECK(state IN
            ('success','failure','pending_review','manual_unverified')),
        content_json TEXT NOT NULL,
        source_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(repository_id,source_key)
    )""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER repository_memories_{action.lower()}_immutable
            BEFORE {action} ON repository_memories
            BEGIN SELECT RAISE(ABORT,'memory source records are immutable'); END""")


def downgrade():
    raise RuntimeError("记忆来源与证据历史不自动降级")
