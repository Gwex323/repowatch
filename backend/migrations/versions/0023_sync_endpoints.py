from alembic import op

revision = "0023_sync_endpoints"
down_revision = "0022_github_sync"
branch_labels = None
depends_on = None


def upgrade():
    for definition in (
        "revision INTEGER NOT NULL DEFAULT 1",
        "overlap_seconds INTEGER NOT NULL DEFAULT 300",
        "full_refresh_seconds INTEGER NOT NULL DEFAULT 604800",
        "last_result TEXT",
    ):
        op.execute(f"ALTER TABLE github_sync_state ADD COLUMN {definition}")
    op.execute("""CREATE TABLE github_endpoint_cache (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        query_key TEXT NOT NULL,
        page INTEGER NOT NULL,
        etag TEXT,
        payload_json TEXT NOT NULL,
        has_next INTEGER NOT NULL,
        observed_at REAL NOT NULL,
        PRIMARY KEY(repository_id,query_key,page)
    )""")
    op.execute("""CREATE TABLE github_query_progress (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        query_key TEXT NOT NULL,
        success_at REAL NOT NULL,
        full_refresh_at REAL,
        watermark_at REAL,
        PRIMARY KEY(repository_id,query_key)
    )""")
    op.execute("""CREATE TABLE github_remote_objects (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        remote_version TEXT,
        observed_at REAL NOT NULL,
        PRIMARY KEY(repository_id,kind,object_id)
    )""")


def downgrade():
    raise RuntimeError("同步端点历史不自动降级")
