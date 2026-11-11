"""Explicit local operations. No command implicitly upgrades a user database."""

import argparse
import json
from pathlib import Path

from repowatch.application.credentials import CredentialError
from repowatch.application.storage import StorageError
from repowatch.configuration import StorageSettings
from repowatch.security import DEFAULT_REDACTOR, install_log_redaction


def main():
    parser = argparse.ArgumentParser(description="RepoWatch 本地启停、备份与诊断")
    parser.add_argument(
        "action",
        choices=[
            "initialize",
            "migrate",
            "start",
            "open",
            "serve",
            "status",
            "stop",
            "backup",
            "restore",
            "restore-report",
            "restore-acknowledge",
            "cleanup",
            "diagnose",
            "native-prepare",
            "native-bind",
        ],
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--generation", help=argparse.SUPPRESS)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--report-id")
    parser.add_argument("--basis-sha256")
    parser.add_argument("--explanation")
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--execution-sid")
    parser.add_argument("--python-runtime", type=Path)
    parser.add_argument("--control-url")
    parser.add_argument("--apply", action="store_true", help="显式执行已预览的清理")
    args = parser.parse_args()
    settings = (
        StorageSettings(args.data_dir) if args.data_dir else StorageSettings.from_environment()
    )
    install_log_redaction()
    try:
        result = execute(args, settings)
    except (ValueError, PermissionError, OSError, StorageError, CredentialError):
        print(json.dumps({"state": "operation_failed", "action": args.action}, ensure_ascii=False))
        return 2
    print(json.dumps(DEFAULT_REDACTOR.clean(result), ensure_ascii=False, indent=2))
    return 0


def execute(args, settings):
    from repowatch.operations.launcher import request_open, request_stop, start, status, supervise
    from repowatch.operations.maintenance import Maintenance, dependency_status, restore
    from repowatch.operations.migrate import migrate

    if args.action == "native-prepare":
        from repowatch.operations.native_prepare import prepare

        if not all((args.native_root, args.execution_sid, args.python_runtime, args.control_url)):
            raise ValueError("原生准备需要全新目录、专用账户 SID、工具链及实际控制 API URL")
        return prepare(
            args.native_root,
            execution_sid=args.execution_sid,
            python_root=args.python_runtime,
            control_url=args.control_url,
        )

    if args.action in {"initialize", "migrate"}:
        dependency_status()
        if args.action == "initialize" and settings.business_database.exists():
            raise ValueError("目标已初始化，请先备份并显式迁移")
        migrate(settings)
        return {"initialized": True, "paused": True}
    if args.action == "status":
        return status(settings)
    if args.action == "start":
        return start(settings, port=args.port, browser=not args.no_browser)
    if args.action == "serve":
        supervise(settings, port=args.port, browser=not args.no_browser, generation=args.generation)
        return {"state": "stopped"}
    if args.action == "stop":
        return request_stop(settings)
    if args.action == "open":
        return request_open(settings)
    if args.action == "restore":
        if args.backup_path is None or not args.backup_path.is_absolute():
            raise ValueError("恢复需要备份的绝对路径")
        return restore(args.backup_path, settings)
    from repowatch.api.host import HostServices

    services = HostServices(settings)
    try:
        if args.action == "native-bind":
            from repowatch.operations.native import NativeConfiguration

            if not args.operation_id or not args.native_root:
                raise ValueError("原生绑定需要操作标识及宿主目录")
            with services.store.read() as records:
                revision = records.one("operations_configuration", singleton=1)["revision"]
            return NativeConfiguration(services.store).configure(
                args.operation_id,
                enabled=True,
                expected_revision=revision,
                host_root=args.native_root,
            )
        maintenance = Maintenance(services)
        if args.action in {"restore-report", "restore-acknowledge"}:
            from repowatch.operations.reconcile import RestoreReconciliation

            if not args.operation_id:
                raise ValueError("恢复核对需要稳定操作标识")
            reconciliation = RestoreReconciliation(services)
            if args.action == "restore-report":
                return reconciliation.report(args.operation_id)
            if not args.report_id or not args.basis_sha256 or not args.explanation:
                raise ValueError("确认恢复核对需要当前报告、摘要和明确依据")
            return reconciliation.acknowledge(
                args.operation_id, args.report_id, args.basis_sha256, explanation=args.explanation
            )
        if args.action == "backup":
            if not args.operation_id:
                raise ValueError("备份必须提供稳定操作标识")
            return maintenance.backup(args.operation_id)
        if args.action == "cleanup":
            if args.apply and (not args.plan_sha256 or not args.operation_id):
                raise ValueError("执行清理需要操作标识与当前预览摘要")
            return maintenance.cleanup(
                dry_run=not args.apply, operation_id=args.operation_id, plan_sha256=args.plan_sha256
            )
        return maintenance.view()
    finally:
        services.close()


if __name__ == "__main__":
    raise SystemExit(main())
