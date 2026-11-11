from alembic import op

revision = "0027_local_operations"
down_revision = "0026_memory_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""CREATE TABLE operations_configuration (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        revision INTEGER NOT NULL DEFAULT 1,
        policy_json TEXT NOT NULL DEFAULT
        '{"minimum_free_bytes":67108864,"artifact_days":30,"log_days":14,"workspace_days":7}',
        native_json TEXT NOT NULL DEFAULT '{"enabled":false}'
    )""")
    op.execute("INSERT INTO operations_configuration(singleton) VALUES(1)")


def downgrade():
    raise RuntimeError("运维审计与保留策略不自动降级，请恢复兼容备份")
