import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "embedding_real", Path(__file__).resolve().parents[2] / "experiments/embedding-real.py"
)
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


def test_snapshot_rejects_modified_missing_and_extra_files(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"original")
    asset = {"model": "fixture", "files": {"model.onnx": experiment.digest(model)}}
    experiment.verify_snapshot(tmp_path, asset)
    model.write_bytes(b"modified")
    with pytest.raises(ValueError, match="digest mismatch"):
        experiment.verify_snapshot(tmp_path, asset)
    model.unlink()
    with pytest.raises(ValueError, match="inventory mismatch"):
        experiment.verify_snapshot(tmp_path, asset)
    model.write_bytes(b"original")
    (tmp_path / "extra.json").write_text("{}")
    with pytest.raises(ValueError, match="inventory mismatch"):
        experiment.verify_snapshot(tmp_path, asset)
