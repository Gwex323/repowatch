"""GitHub 账号选择与宿主凭据解析。"""

import re
from uuid import uuid4

from repowatch.application.credentials import CredentialError, SecretValue, validate_reference
from repowatch.application.storage import ConflictError
from repowatch.security import DEFAULT_REDACTOR

ACCOUNT_PREFIX = "github-account-"
SECRET_PREFIX = "github-token-"


class GitHubAccountError(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class GitHubAccountVault:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault

    def create(self, reference, secret):
        self.vault.create(reference, secret)

    def read(self, reference):
        validate_reference(reference)
        if reference.casefold().startswith(ACCOUNT_PREFIX):
            previous = None
            for _ in range(2):
                with self.store.read() as records:
                    row = records.one("github_accounts", id=reference)
                if row is None or row["secret_reference"] == previous:
                    break
                previous = row["secret_reference"]
                try:
                    return self.vault.read(previous)
                except CredentialError:
                    # A concurrent rotation may have retired the reference we just read.
                    continue
            raise CredentialError()
        if reference.casefold().startswith(SECRET_PREFIX):
            raise CredentialError()
        return self.vault.read(reference)

    def delete(self, reference):
        self.vault.delete(reference)


class GitHubAccounts:
    def __init__(self, store, vault, verify):
        self.store, self.vault, self.verify = store, vault, verify

    @staticmethod
    def _public(row):
        return {
            key: row[key]
            for key in ("id", "github_user_id", "login", "is_default", "verified_at", "revision")
        }

    def list(self):
        with self.store.read() as records:
            rows = records.find("github_accounts")
        rows.sort(key=lambda row: row["login"].casefold())
        return [self._public(row) for row in rows]

    @staticmethod
    def _current(records, identifier, revision):
        row = records.one("github_accounts", id=identifier)
        if row is None:
            raise GitHubAccountError("github_account_missing", 404)
        if row["revision"] != revision:
            raise ConflictError("GitHub 账号已变更")
        return row

    def save(self, token, *, now, username=None, identifier=None, revision=None):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,512}", token):
            raise ValueError("GitHub 令牌格式无效")
        if identifier is None and (
            not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", username)
        ):
            raise ValueError("GitHub 用户名格式无效")
        secret = SecretValue(token)
        DEFAULT_REDACTOR.register(secret)
        identity = self.verify(secret)
        if identifier is None and identity["login"].casefold() != username.casefold():
            raise GitHubAccountError("github_account_mismatch")
        reference = SECRET_PREFIX + uuid4().hex
        created = False
        previous = None
        try:
            with self.store.write() as records:
                if identifier is None:
                    if records.one("github_accounts", github_user_id=identity["id"]):
                        raise GitHubAccountError("github_account_exists")
                    row = {
                        "id": ACCOUNT_PREFIX + uuid4().hex,
                        "github_user_id": identity["id"],
                        "is_default": int(not records.find("github_accounts")),
                        "revision": 1,
                    }
                else:
                    old = self._current(records, identifier, revision)
                    if identity["id"] != old["github_user_id"]:
                        raise GitHubAccountError("github_account_mismatch")
                    previous = old["secret_reference"]
                    row = {**old, "revision": revision + 1}
                self.vault.create(reference, secret)
                created = True
                row.update(login=identity["login"], secret_reference=reference, verified_at=now)
                if identifier is None:
                    records.add("github_accounts", row)
                else:
                    records.change("github_accounts", {"id": identifier, "revision": revision}, row)
        except Exception:
            if created:
                self._cleanup(reference)
            raise
        if previous:
            self._cleanup(previous)
        return self._public(row)

    def _cleanup(self, reference):
        try:
            self.vault.delete(reference)
        except CredentialError:
            # The reference is no longer active. Never revert to it after a committed update.
            pass

    def revalidate(self, identifier, revision, *, now):
        with self.store.read() as records:
            old = self._current(records, identifier, revision)
            secret = self.vault.read(old["secret_reference"])
        identity = self.verify(secret)
        if identity["id"] != old["github_user_id"]:
            raise GitHubAccountError("github_account_mismatch")
        with self.store.write() as records:
            self._current(records, identifier, revision)
            values = {"login": identity["login"], "verified_at": now, "revision": revision + 1}
            records.change("github_accounts", {"id": identifier, "revision": revision}, values)
        return self._public({**old, **values})

    def set_default(self, identifier, revision):
        with self.store.write() as records:
            row = self._current(records, identifier, revision)
            if row["is_default"]:
                return self._public(row)
            previous = records.one("github_accounts", is_default=1)
            if previous:
                records.change(
                    "github_accounts",
                    {"id": previous["id"]},
                    {"is_default": 0, "revision": previous["revision"] + 1},
                )
            values = {"is_default": 1, "revision": revision + 1}
            records.change("github_accounts", {"id": identifier, "revision": revision}, values)
        return self._public({**row, **values})

    def disconnect(self, identifier, revision):
        with self.store.write() as records:
            row = self._current(records, identifier, revision)
            if records.find("repositories", credential_reference=identifier):
                raise GitHubAccountError("github_account_in_use")
            records.remove("github_accounts", id=identifier)
        self._cleanup(row["secret_reference"])
        return {"disconnected": True}
