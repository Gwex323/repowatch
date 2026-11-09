from alembic import op

revision = "0022_github_sync"
down_revision = "0021_distillation_state"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE github_sync_lease (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        owner TEXT NOT NULL,
        process_json TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE github_sync_state (
        repository_id TEXT PRIMARY KEY REFERENCES repositories(id),
        active_seconds INTEGER NOT NULL DEFAULT 7200 CHECK(active_seconds>0),
        idle_seconds INTEGER NOT NULL DEFAULT 86400 CHECK(idle_seconds>0),
        lookback_seconds INTEGER NOT NULL DEFAULT 604800 CHECK(lookback_seconds>0),
        requested_sequence INTEGER NOT NULL DEFAULT 0,
        completed_sequence INTEGER NOT NULL DEFAULT 0,
        last_success_at REAL,
        last_changed_at REAL,
        next_due_at REAL NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        error_code TEXT
    )""")
    op.execute("""CREATE TABLE github_sync_runs (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        owner TEXT NOT NULL,
        requested_sequence INTEGER NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        state TEXT NOT NULL CHECK(state IN ('running','completed','failed')),
        error_code TEXT
    )""")
    op.execute("""CREATE UNIQUE INDEX one_running_sync_per_repository
        ON github_sync_runs(repository_id) WHERE state='running'""")


def downgrade():
    raise RuntimeError("同步进度与请求历史不自动降级")
