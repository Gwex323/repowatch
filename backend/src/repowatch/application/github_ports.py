"""GitHub 只读端口；不包含任何创建、评论、推送或合并能力。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReadPage:
    status: int
    items: tuple[dict, ...] = ()
    etag: str | None = None
    has_next: bool = False


class GitHubReadError(RuntimeError):
    def __init__(self, code, *, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__("GitHub 读取未完成：" + code)


class GitHubReadPort(Protocol):
    def repository(self, owner: str, name: str) -> dict: ...

    def issues_page(
        self, owner: str, name: str, *, page: int, etag: str | None = None
    ) -> ReadPage: ...
