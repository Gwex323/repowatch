import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("REPOWATCH_EMBEDDING_TESTS") != "1", reason="显式本地资产验收")
@pytest.mark.parametrize("position,dimension", [(0, 512), (1, 768)])
def test_locked_asset_offline_fresh_process(position, dimension):
    root = Path(__file__).resolve().parents[2]
    script = """
import json,socket,sys,sqlite3
from pathlib import Path
from repowatch.adapters.memory_index import LockedEncoder
import sqlite_vec
def no_network(*args, **kwargs):
    raise AssertionError('离线验收不得联网')
socket.create_connection=no_network
socket.socket.connect=no_network
root=Path(sys.argv[1])
asset=json.loads((root/'experiments/embedding-assets.lock.json').read_text())['models'][int(sys.argv[2])]
encoder=LockedEncoder(asset,root/'.cache/fastembed')
vectors=encoder.encode(['令牌刷新失败','create_app 初始化应用'])
query=encoder.encode(['令牌刷新失败'],query=True)[0]
connection=sqlite3.connect(':memory:')
connection.enable_load_extension(True)
sqlite_vec.load(connection)
distance=connection.execute('select vec_distance_cosine(?,?)',
    (sqlite_vec.serialize_float32(vectors[0]),sqlite_vec.serialize_float32(query))).fetchone()[0]
print(json.dumps({'dimension':len(query),'distance':distance,'revision':asset['revision']}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(position)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["dimension"] == dimension
    assert report["distance"] < 0.001
