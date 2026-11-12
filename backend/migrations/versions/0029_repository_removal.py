"""从工作台移除仓库但保留审计记录。"""

from alembic import op

revision = "0029_repository_removal"
down_revision = "0028_github_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE repositories ADD COLUMN removed INTEGER NOT NULL DEFAULT 0 "
        "CHECK(removed IN (0,1))"
    )
    op.execute("ALTER TABLE repositories ADD COLUMN removed_at REAL")


def downgrade():
    raise RuntimeError("请恢复启用仓库移除功能前的兼容备份")
