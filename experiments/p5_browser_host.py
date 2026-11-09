import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)

    def no_network(request):
        raise AssertionError("P5 管理操作不得隐式调用模型或 GitHub")

    services = HostServices(
        settings, vault=SimpleNamespace(), transport=httpx.MockTransport(no_network)
    )
    repository = services.tasks.register_repository(
        "P5-browser-source", admission_mode="all_bugs"
    )
    other = services.tasks.register_repository(
        "P5-browser-other", admission_mode="all_bugs"
    )
    content = {
        "symptoms": "会话令牌刷新失败",
        "applicability": "本地会话边界",
        "method": "检查令牌更新和失效条件",
        "verification": "受控界面夹具",
        "limitations": "浏览器夹具，不代表真实仓库修复",
    }
    with services.store.write() as records:
        records.add(
            "repository_memories",
            {
                "id": "p5-browser-memory",
                "repository_id": repository,
                "source_key": "controlled-browser-fixture",
                "state": "success",
                "content_json": canonical_json(content),
                "source_json": canonical_json(
                    {"fixture": True, "candidate_sha": "a" * 40}
                ),
                "created_at": 0,
            },
        )
    sessions = LocalSessions(f"http://127.0.0.1:{args.port}")
    app = create_app(
        services, sessions=sessions, static_dir=Path("frontend/dist").resolve()
    )
    print(
        json.dumps(
            {
                "bootstrap": sessions.bootstrap_token,
                "repository_id": repository,
                "other_id": other,
            }
        ),
        flush=True,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
