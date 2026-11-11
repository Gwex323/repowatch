"""Dispatch by the immutable workspace backend; Docker remains the default."""

from repowatch.adapters.native_workspaces import NativeWorkspaces
from repowatch.operations.native import require_backend


class WorkspaceBackends:
    def __init__(self, store, docker, objects):
        self.store, self.docker_backend, self.objects = store, docker, objects

    def native_host(self):
        with self.store.read() as records:
            return require_backend(records, {"execution_backend": "native"})

    def for_workspace(self, workspace):
        if workspace.get("kind", "docker") == "native":
            return NativeWorkspaces(self.native_host(), self.objects)
        if workspace.get("kind", "docker") != "docker":
            raise PermissionError("工作区执行后端无效")
        return self.docker_backend

    def create_for(self, configuration, *args, **kwargs):
        if configuration.get("execution_backend", "docker") == "native":
            backend = NativeWorkspaces(self.native_host(), self.objects)
            # The native toolchain is pinned by host admission, independently of Docker images.
            requested = kwargs.get("image")
            if (
                isinstance(requested, str)
                and requested.startswith("sha256:")
                and requested != backend.default_image
            ):
                raise PermissionError("原生工具链与冻结的工作区运行时摘要不匹配")
            kwargs["image"] = backend.default_image
            return backend.create(*args, **kwargs)
        return self.docker_backend.create(*args, **kwargs)

    def docker(self, *args, **kwargs):
        return self.docker_backend.docker(*args, **kwargs)

    def command_status(self, *args, **kwargs):
        return self.docker_backend.command_status(*args, **kwargs)

    def exists(self, workspace):
        return self.for_workspace(workspace).exists(workspace)

    def remove(self, workspace):
        return self.for_workspace(workspace).remove(workspace)

    def read_source(self, workspace, *args):
        return self.for_workspace(workspace).read_source(workspace, *args)

    def write_source(self, workspace, *args):
        return self.for_workspace(workspace).write_source(workspace, *args)

    def delete_source(self, workspace, *args):
        return self.for_workspace(workspace).delete_source(workspace, *args)

    def snapshot(self, workspace, *args):
        return self.for_workspace(workspace).snapshot(workspace, *args)

    def freeze_read_only(self, workspace):
        return self.for_workspace(workspace).freeze_read_only(workspace)
