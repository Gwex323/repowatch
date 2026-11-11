from fastapi import APIRouter

from repowatch.api.routes import Mutation
from repowatch.operations.maintenance import Maintenance
from repowatch.operations.native import NativeConfiguration
from repowatch.operations.reconcile import RestoreReconciliation


def operation_routes(services):
    router = APIRouter(prefix="/api/v1/operations")
    maintenance = Maintenance(services)

    @router.get("")
    def view():
        return maintenance.view()

    @router.post("/policy")
    def policy(body: Mutation):
        return maintenance.configure(
            body.operation_id, body.data, expected_revision=body.revision()
        )

    @router.post("/backup")
    def backup(body: Mutation):
        if body.data:
            raise ValueError("备份操作不接受其他参数")
        return maintenance.backup(body.operation_id)

    @router.post("/native")
    def native(body: Mutation):
        return NativeConfiguration(services.store).configure(
            body.operation_id,
            enabled=body.data["enabled"],
            expected_revision=body.revision(),
            host_root=body.data.get("host_root"),
        )

    @router.post("/cleanup")
    def cleanup(body: Mutation):
        dry_run = body.data.get("dry_run", True)
        if not dry_run and not body.data.get("plan_sha256"):
            raise ValueError("执行清理需要当前预览的摘要")
        return maintenance.cleanup(
            dry_run=dry_run,
            operation_id=body.operation_id,
            plan_sha256=body.data.get("plan_sha256"),
        )

    @router.post("/restore-report")
    def restore_report(body: Mutation):
        return RestoreReconciliation(services).report(body.operation_id)

    @router.post("/restore-acknowledge")
    def restore_acknowledge(body: Mutation):
        return RestoreReconciliation(services).acknowledge(
            body.operation_id,
            body.data["report_id"],
            body.data["basis_sha256"],
            explanation=body.data["explanation"],
        )

    return router
