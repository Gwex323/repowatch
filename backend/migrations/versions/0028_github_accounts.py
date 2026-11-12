from alembic import op

revision = "0028_github_accounts"
down_revision = "0027_local_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE github_accounts (
        id TEXT PRIMARY KEY NOT NULL,
        github_user_id TEXT UNIQUE NOT NULL,
        login TEXT NOT NULL,
        secret_reference TEXT UNIQUE NOT NULL,
        is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
        verified_at REAL NOT NULL,
        revision INTEGER NOT NULL
    )""")
    op.execute(
        "CREATE UNIQUE INDEX github_accounts_default ON github_accounts(is_default) "
        "WHERE is_default=1"
    )


def downgrade():
    raise RuntimeError("请恢复绑定功能启用前的兼容备份")
