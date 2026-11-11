"""独立 P6 样本；仅在专用 sandbox 分支使用，无外部包下载或服务依赖。"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    key: str
    route: str
    path: str
    marker: str
    fixed: str
    files: dict[str, str]
    probe_path: str
    probe: str
    probe_argv: list[str]
    image: str
    build: list[str]
    preview: list[str] | None = None


PY_CLI = """import argparse

def p6_cli_add(left, right):
    return left - right  # P6_PYTHON_CLI_BUG

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('left', type=int)
    parser.add_argument('right', type=int)
    args = parser.parse_args()
    print(p6_cli_add(args.left, args.right))
"""
PY_CLI_TEST = """import subprocess
import sys
import unittest

class CliTest(unittest.TestCase):
    def test_adds_via_cli(self):
        completed = subprocess.run([sys.executable, 'p6_python_cli.py', '2', '3'],
                                   capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout.strip(), '5')
    def test_zero(self):
        completed = subprocess.run([sys.executable, 'p6_python_cli.py', '2', '0'],
                                   capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout.strip(), '2')
"""
PY_WEB = """import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def p6_web_add(left, right):
    return left - right  # P6_PYTHON_WEB_BUG

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/':
            self.send_error(404)
            return
        value = p6_web_add(2, 3)
        body = ('<!doctype html><html><head><title>P6 Python Web</title></head>'
                '<body><h1>P6 Python Web</h1><output id="result">'
                + str(value) + '</output></body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ThreadingHTTPServer(('0.0.0.0', args.port), Handler).serve_forever()
"""
PY_WEB_TEST = """import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from p6_python_web import Handler

class WebTest(unittest.TestCase):
    def test_http_result(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen('http://127.0.0.1:' + str(server.server_port) + '/') as response:
                self.assertIn('<output id="result">5</output>', response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
"""
JS_CLI = """import {pathToFileURL} from 'node:url';
export function p6_js_add(left, right) {
  return left - right; // P6_JAVASCRIPT_CLI_BUG
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  console.log(p6_js_add(Number(process.argv[2]), Number(process.argv[3])));
}
"""
JS_TEST = """import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('actual CLI returns the sum', () => {
  const result = spawnSync(process.execPath, ['p6_node_cli.mjs', '2', '3'], {encoding:'utf8'});
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), '5');
});
test('CLI zero argument', () => {
  const result = spawnSync(process.execPath, ['p6_node_cli.mjs', '2', '0'], {encoding:'utf8'});
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), '2');
});
"""
TS_CALC = """export function p6_ts_add(left: number, right: number): number {
  return left - right; // P6_TYPESCRIPT_WEB_BUG
}
"""
TS_BUILD = """import {execFileSync} from 'node:child_process';
import {mkdirSync, writeFileSync} from 'node:fs';
execFileSync(process.execPath, ['/opt/repowatch/typescript/lib/tsc.js', '--project', 'tsconfig.json'], {stdio:'inherit'});
mkdirSync('dist', {recursive:true});
writeFileSync('dist/index.html', '<!doctype html><html><head><title>P6 TypeScript Web</title></head><body><h1>P6 TypeScript Web</h1><output id="result">Loading</output><script type="module" src="/app.js"></script></body></html>');
writeFileSync('dist/app.js', "fetch('/api/result').then(r=>r.json()).then(r=>document.getElementById('result').textContent=String(r.value));");
console.log('P6_TYPESCRIPT_BUILD_OK');
"""
TS_SERVER = """import {createServer as createHttpServer} from 'node:http';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {p6_ts_add} from './dist/p6_calc.js';
export function createServer() {
  return createHttpServer((req,res) => {
    if (req.url === '/api/result') {
      res.writeHead(200, {'Content-Type':'application/json'});
      res.end(JSON.stringify({value:p6_ts_add(2,3)}));
    } else if (req.url === '/' || req.url === '/app.js') {
      res.writeHead(200, {'Content-Type':req.url === '/' ? 'text/html' : 'text/javascript'});
      res.end(readFileSync(req.url === '/' ? 'dist/index.html' : 'dist/app.js'));
    } else { res.writeHead(404); res.end(); }
  });
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  createServer().listen(Number(process.env.PORT || 8080), '0.0.0.0');
}
"""
TS_TEST = """import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from '../server.mjs';
test('compiled TypeScript serves a correct HTTP response', async () => {
  const server = createServer();
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  try {
    const base = 'http://127.0.0.1:' + server.address().port;
    assert.match(await (await fetch(base + '/')).text(), /P6 TypeScript Web/);
    assert.equal((await (await fetch(base + '/api/result')).json()).value, 5);
  } finally { await new Promise(done => server.close(done)); }
});
"""


def samples():
    python = "python:3.13-slim-bookworm"
    node = "node:24-bookworm-slim"
    result = {}
    for key, route, path, code, test, symbol, marker in (
        (
            "python-cli",
            "direct",
            "p6_python_cli.py",
            PY_CLI,
            PY_CLI_TEST,
            "p6_cli_add",
            "P6_PYTHON_CLI_BUG",
        ),
        (
            "python-web",
            "fork",
            "p6_python_web.py",
            PY_WEB,
            PY_WEB_TEST,
            "p6_web_add",
            "P6_PYTHON_WEB_BUG",
        ),
    ):
        probe = (
            f"from {path[:-3]} import {symbol}\n"
            f"if {symbol}(2, 3) != 5:\n    print('TARGET_BUG')\n    raise SystemExit(1)\n"
            "print('TARGET_OK')\n"
        )
        result[key] = Sample(
            key,
            route,
            path,
            "return left - right  # " + marker,
            code.replace("left - right", "left + right"),
            {
                path: code,
                "tests/test_sample.py": test,
                "README.md": "# P6 " + key + "\nSelf-contained controlled sample.\n",
            },
            "tests/probe_p6.py",
            probe,
            ["python", "-c", "exec(open('tests/probe_p6.py').read())"],
            python,
            ["python", "-m", "compileall", "-q", "."],
            ["python", "-B", "-u", "/source/" + path, "--port", "8080"]
            if key.endswith("web")
            else None,
        )
    result["javascript-cli"] = Sample(
        "javascript-cli",
        "direct",
        "p6_node_cli.mjs",
        "return left - right; // P6_JAVASCRIPT_CLI_BUG",
        JS_CLI.replace("left - right", "left + right"),
        {
            "p6_node_cli.mjs": JS_CLI,
            "tests/test_cli.mjs": JS_TEST,
            "package.json": json.dumps(
                {
                    "name": "p6-javascript-cli",
                    "version": "1.0.0",
                    "type": "module",
                    "scripts": {
                        "test": "node --test tests/test_cli.mjs",
                        "build": "node --check p6_node_cli.mjs",
                    },
                },
                indent=2,
            ),
        },
        "tests/probe_p6.mjs",
        "import {p6_js_add} from '../p6_node_cli.mjs';\nif (p6_js_add(2,3)!==5) {console.log('TARGET_BUG');process.exit(1);} console.log('TARGET_OK');\n",
        ["node", "tests/probe_p6.mjs"],
        node,
        ["npm", "run", "build"],
    )
    result["typescript-web"] = Sample(
        "typescript-web",
        "fork",
        "p6_calc.ts",
        "return left - right; // P6_TYPESCRIPT_WEB_BUG",
        TS_CALC.replace("left - right", "left + right"),
        {
            "p6_calc.ts": TS_CALC,
            "build.mjs": TS_BUILD,
            "server.mjs": TS_SERVER,
            "tests/test_web.mjs": TS_TEST,
            "tsconfig.json": json.dumps(
                {
                    "compilerOptions": {
                        "strict": True,
                        "target": "ES2022",
                        "module": "ES2022",
                        "outDir": "dist",
                    },
                    "include": ["p6_calc.ts"],
                },
                indent=2,
            ),
            "package.json": json.dumps(
                {
                    "name": "p6-typescript-web",
                    "version": "1.0.0",
                    "type": "module",
                    "scripts": {
                        "test": "node build.mjs && node --test tests/test_web.mjs",
                        "typecheck": "node /opt/repowatch/typescript/lib/tsc.js --noEmit",
                        "build": "node build.mjs",
                    },
                },
                indent=2,
            ),
        },
        "tests/probe_p6.mjs",
        "import {p6_ts_add} from '../p6_calc.ts';\nif (p6_ts_add(2,3)!==5) {console.log('TARGET_BUG');process.exit(1);} console.log('TARGET_OK');\n",
        ["node", "tests/probe_p6.mjs"],
        "repowatch-p6-typescript:locked",
        ["npm", "run", "build"],
        [
            "sh",
            "-c",
            "cp -a /source/. /tmp/app; cd /tmp/app; node build.mjs && PORT=8080 node server.mjs",
        ],
    )
    return result
