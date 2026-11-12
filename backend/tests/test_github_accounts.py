from types import SimpleNamespace
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from repowatch.adapters.storage.sqlite import SQLiteBusinessStore, sqlite_engine
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.application.credentials import CredentialError, SecretValue
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate, migration_directory

ORIGIN = "http://127.0.0.1:8765"
ALICE = "fixture_" + "a" * 24
ALICE_NEW = "fixture_" + "b" * 24
BOB = "fixture_" + "c" * 24


def test_migration_preserves_existing_repository_reference(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    settings.data_dir.mkdir()
    configuration = Config()
    configuration.set_main_option("script_location", str(migration_directory()).replace("%", "%%"))
    engine = sqlite_engine(settings, create=True)
    try:
        with engine.begin() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "0027_local_operations")
            connection.exec_driver_sql(
                "INSERT INTO repositories (id, github_id, admission_mode, credential_reference) "
                "VALUES ('old', '123', 'manual', 'legacy')"
            )
    finally:
        engine.dispose()

    migrate(settings)
    store = SQLiteBusinessStore(settings)
    try:
        with store.read() as records:
            assert records.one("repositories", id="old")["credential_reference"] == "legacy"
            assert records.find("github_accounts") == []
    finally:
        store.close()


def test_account_binding_and_repository_selection(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    secrets = {"legacy": SecretValue(ALICE)}
    actors = {ALICE: (1, "alice"), ALICE_NEW: (1, "alice-renamed"), BOB: (2, "bob")}

    def read(reference):
        if reference not in secrets:
            raise CredentialError()
        return secrets[reference]

    def github(request):
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token not in actors:
            return httpx.Response(401)
        identifier, login = actors[token]
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": login, "id": identifier, "type": "User"})
        if "/branches/" in request.url.path:
            return httpx.Response(200, json={"commit": {"sha": "a" * 40}})
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "denied":
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "id": {"first": 101, "second": 102, "legacy": 103, "pending": 104}[name],
                "name": name,
                "owner": {"login": "upstream"},
                "default_branch": "main",
                "private": False,
                "permissions": {"push": True},
            },
        )

    vault = SimpleNamespace(
        create=lambda key, value: secrets.update({key: value}),
        read=read,
        delete=lambda key: secrets.pop(key, None),
    )
    host = HostServices(settings, vault=vault, github_transport=httpx.MockTransport(github))
    sessions = LocalSessions()
    client = TestClient(
        create_app(host, sessions=sessions), base_url=ORIGIN, client=("127.0.0.1", 5000)
    )
    root = "/api/v1"
    account_path = "/github/accounts"
    try:
        assert client.get(root + account_path).status_code == 401
        session = client.post(
            root + "/session/bootstrap",
            headers={"Origin": ORIGIN, "Authorization": "Bearer " + sessions.bootstrap_token},
        )
        assert session.status_code == 200
        headers = {"Origin": ORIGIN, "X-RepoWatch-Session": session.json()["csrf"]}

        def post(path, revision=None, status=200, **data):
            result = client.post(
                root + path,
                headers=headers,
                json={"operation_id": str(uuid4()), "expected_revision": revision, "data": data},
            )
            assert all(token not in result.text for token in actors)
            assert result.status_code == status, result.text
            return result.json()

        def preflight(name, **credentials):
            return post("/repositories/preflight", owner="upstream", name=name, **credentials)

        def register(check, revision=0):
            return post("/repositories/import", revision, preflight_id=check["preflight_id"])

        old = register(preflight("legacy", credential_reference="legacy"))
        post(account_path, username="bob", token=ALICE, status=409)
        alice = post(account_path, username="ALICE", token=ALICE)
        bob = post(account_path, username="bob", token=BOB)
        assert alice["is_default"] == 1
        assert "secret_reference" not in alice
        first = register(preflight("first", github_account_id=alice["id"]))
        second = register(preflight("second", github_account_id=alice["id"]))
        pending = preflight("pending", github_account_id=alice["id"])
        post(f"{account_path}/{bob['id']}/default", bob["revision"])

        state = client.get(root + "/state").json()
        refs = {repo["id"]: repo["credential_reference"] for repo in state["repositories"]}
        assert refs[first["repository_id"]] == refs[second["repository_id"]] == alice["id"]
        assert refs[old["repository_id"]] == "legacy"
        assert [row["id"] for row in state["github_accounts"] if row["is_default"]] == [bob["id"]]
        alice = next(row for row in state["github_accounts"] if row["id"] == alice["id"])
        path = f"{account_path}/{alice['id']}"
        assert post(path + "/disconnect", alice["revision"], status=409)["error"] == (
            "github_account_in_use"
        )
        post(path + "/token", alice["revision"], token=BOB, status=409)
        assert host.vault.read(alice["id"]).reveal() == ALICE
        rotated = post(path + "/token", alice["revision"], token=ALICE_NEW)
        assert rotated["login"] == "alice-renamed"
        assert host.vault.read(alice["id"]).reveal() == ALICE_NEW
        assert (
            post("/repositories/import", 0, status=409, preflight_id=pending["preflight_id"])[
                "error"
            ]
            == "github_account_preflight_stale"
        )
        assert (
            post(
                "/repositories/preflight",
                status=422,
                owner="upstream",
                name="denied",
                github_account_id=alice["id"],
            )["error"]
            == "github_authentication_or_permission"
        )
        updated = register(preflight("legacy", github_account_id=bob["id"]), revision=1)
        assert updated["repository_id"] == old["repository_id"]

        for name in ("first", "second"):
            register(preflight(name, github_account_id=bob["id"]), revision=1)
        post(path + "/verify", rotated["revision"])
        post(path + "/disconnect", rotated["revision"] + 1)
        assert all(secret.reveal() != ALICE_NEW for secret in secrets.values())
        assert all(
            token.encode() not in settings.business_database.read_bytes() for token in actors
        )
    finally:
        client.close()
        host.close()
