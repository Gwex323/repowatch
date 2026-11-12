"""正式本地服务组装；不隐式迁移、不启动 Worker 或推理请求。"""

import time
from hashlib import sha256
from pathlib import Path

from repowatch.adapters.credentials import WindowsCredentialVault
from repowatch.adapters.git_objects import GitObjects
from repowatch.adapters.github import GitHubReadAdapter
from repowatch.adapters.memory_index import MemoryIndex
from repowatch.adapters.providers import ProviderAdapter
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.api.app import create_app
from repowatch.api.security import LocalSessions
from repowatch.application.artifacts import ArtifactService
from repowatch.application.budget_adjustments import BudgetAdjustments
from repowatch.application.budgets import BudgetService
from repowatch.application.candidate_issues import CandidateIssues
from repowatch.application.console import Console
from repowatch.application.control import ControlService
from repowatch.application.diagnostics import DiagnosticService
from repowatch.application.github_accounts import GitHubAccounts, GitHubAccountVault
from repowatch.application.github_polling import GitHubPolling
from repowatch.application.github_reads import GitHubReads, MemorySourceFacts
from repowatch.application.github_sync import GitHubSynchronization
from repowatch.application.issue_context import IssueContexts
from repowatch.application.memory import RepositoryMemory
from repowatch.application.orchestration import Orchestrator
from repowatch.application.pr_lifecycle import PullRequestLifecycle
from repowatch.application.providers import ProviderService
from repowatch.application.recovery import RecoveryService
from repowatch.application.repositories import RepositoryService
from repowatch.application.roles import RoleService
from repowatch.application.scans import ScanService
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.operations.locks import guarded
from repowatch.worker.commands import CommandSupervisor


class HostServices:
    def __init__(
        self, settings, *, vault=None, clock=time.time, transport=None, github_transport=None
    ):
        from repowatch.operations.maintenance import dependency_status

        dependency_status()
        self.settings, self.clock = settings, clock
        self.store = SQLiteBusinessStore(settings)
        namespace = sha256(str(settings.data_dir).casefold().encode()).hexdigest()[:40]
        raw_vault = vault if vault is not None else WindowsCredentialVault(namespace)
        self.vault = GitHubAccountVault(self.store, raw_vault)
        self.github_accounts = GitHubAccounts(
            self.store, raw_vault, GitHubReadAdapter(transport=github_transport).authenticated_user
        )
        self.tasks, self.queue = TaskRecords(self.store), Orchestrator(self.store)
        self.budgets = BudgetService(self.store, settings.artifacts_dir)
        self.adjustments = BudgetAdjustments(self.store)
        self.artifacts = ArtifactService(self.store, settings.artifacts_dir)
        self.commands = CommandSupervisor(
            self.store, settings.data_dir / "commands", self.queue, clock=clock
        )
        self.control = ControlService(self.store, commands=self.commands)
        self.recovery = RecoveryService(self.store, settings, commands=self.commands)
        self.providers = ProviderService(self.store, self.vault)
        self.adapter = ProviderAdapter(self.providers, transport=transport)
        self.roles = RoleService(self.store, self.artifacts)
        self.diagnostics = DiagnosticService(self.store)
        self.github_reads = GitHubReads(self.store, GitHubReadAdapter())
        self.repositories = RepositoryService(
            self.store,
            lambda reference: GitHubReadAdapter(
                vault=self.vault, credential_reference=reference, transport=github_transport
            ),
        )
        self.synchronization = GitHubSynchronization(
            self.store,
            GitHubPolling(self),
            clock=clock,
        )
        self.queue.target_gate = self.repositories.verify_target
        self.memory_sources = MemorySourceFacts(self.store)
        self.memories = RepositoryMemory(self.store, self.artifacts)
        self.memory_index = MemoryIndex(settings, self.memories)
        self.scans = ScanService(
            self.store, GitObjects(settings.data_dir / "git-objects"), self.artifacts
        )
        self.console = Console(self.store, self.budgets, self.providers, self.roles)
        self.issue_contexts = IssueContexts(self)
        self.candidate_issues = CandidateIssues(self)
        self.lifecycle = PullRequestLifecycle(self)

    @guarded
    def reconcile_startup(self):
        self.providers.reconcile()
        self.github_reads.reconcile()
        return self.recovery.scan_startup(now=self.clock())

    def close(self):
        self.store.close()


def create_host_app(settings=None, *, sessions=None, static_dir=None):
    services = HostServices(settings or StorageSettings.from_environment())
    services.reconcile_startup()
    packaged = Path(__file__).resolve().parents[1] / "_frontend"
    built_frontend = (
        packaged if packaged.is_dir() else Path(__file__).resolve().parents[4] / "frontend" / "dist"
    )
    if static_dir is None and built_frontend.is_dir():
        static_dir = built_frontend
    return create_app(services, sessions=sessions or LocalSessions(), static_dir=static_dir)
