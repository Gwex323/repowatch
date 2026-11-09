from alembic import op

revision = "0024_pr_lifecycle"
down_revision = "0023_sync_endpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE github_sync_state ADD COLUMN change_sequence INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE github_sync_runs ADD COLUMN initial_change_sequence INTEGER NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE github_remote_objects ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    op.execute(
        "ALTER TABLE github_sync_state ADD COLUMN ci_policy_revision INTEGER NOT NULL DEFAULT 1"
    )
    op.execute("ALTER TABLE github_sync_state ADD COLUMN ci_policy_json TEXT NOT NULL DEFAULT '{}'")
    op.execute("""CREATE TABLE pr_lifecycle (
        task_id TEXT PRIMARY KEY REFERENCES managed_pull_requests(task_id),
        revision INTEGER NOT NULL DEFAULT 1,
        snapshot_json TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        feedback_sha256 TEXT NOT NULL,
        number INTEGER NOT NULL,
        head_sha TEXT NOT NULL,
        base_sha TEXT NOT NULL,
        merge_sha TEXT,
        delivery_state TEXT NOT NULL,
        issue_state TEXT NOT NULL,
        ci_state TEXT NOT NULL,
        observed_at REAL NOT NULL
    )""")
    op.execute("""CREATE TABLE review_scope_decisions (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        context_sha256 TEXT NOT NULL,
        decision TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        input_revision INTEGER NOT NULL,
        feedback_revision INTEGER NOT NULL,
        acceptance_revision INTEGER NOT NULL,
        actor TEXT NOT NULL,
        PRIMARY KEY(task_id,context_sha256)
    )""")
    op.execute("""CREATE TABLE acceptance_carryovers (
        task_id TEXT NOT NULL REFERENCES tasks(id),
        revision INTEGER NOT NULL,
        source_revision INTEGER NOT NULL,
        context_sha256 TEXT NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        PRIMARY KEY(task_id,revision),
        FOREIGN KEY(task_id,revision) REFERENCES task_acceptance_specs(task_id,revision),
        FOREIGN KEY(task_id,source_revision) REFERENCES task_acceptance_specs(task_id,revision)
    )""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER acceptance_carryovers_{action.lower()}_immutable
            BEFORE {action} ON acceptance_carryovers
            BEGIN SELECT RAISE(ABORT,'approval carryover evidence is immutable'); END""")


def downgrade():
    raise RuntimeError("PR 生命周期与评审决定不自动降级")
