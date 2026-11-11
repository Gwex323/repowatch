"""显式启动回环 API；浏览器片段接收一次引导，不将令牌写到日志。"""

import argparse
import asyncio
import json
import webbrowser
from pathlib import Path

import uvicorn

from repowatch.api.host import create_host_app
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings


def main():
    parser = argparse.ArgumentParser(description="RepoWatch 本地控制台；先显式完成数据库迁移")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口无效")
    origin = f"http://127.0.0.1:{args.port}"
    sessions = LocalSessions(origin)
    settings = (
        StorageSettings(args.data_dir) if args.data_dir else StorageSettings.from_environment()
    )
    app = create_host_app(settings, sessions=sessions)

    class BrowserServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and args.stop_file:
                args.stop_file.with_suffix(".ready").touch()
            if self.started and not args.no_browser:
                webbrowser.open(origin + "/#session=" + sessions.bootstrap_token)

    server = BrowserServer(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    )

    async def serve():
        async def watch():
            opened = None
            while not server.should_exit:
                if args.stop_file and args.stop_file.exists():
                    server.should_exit = True
                    return
                if args.stop_file:
                    request = args.stop_file.parent / "open.json"
                    if request.is_file():
                        value = json.loads(request.read_text("utf-8"))
                        generation = args.stop_file.stem.removeprefix("api-")
                        if value.get("generation") == generation and value.get("request") != opened:
                            opened = value["request"]
                            sessions.rotate_bootstrap()
                            webbrowser.open(origin + "/#session=" + sessions.bootstrap_token)
                await asyncio.sleep(0.2)

        task = asyncio.create_task(watch())
        try:
            await server.serve()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
