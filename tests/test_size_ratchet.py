import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "size_ratchet.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("size_ratchet", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_baseline(path, *, defs=1, oversized=None):
    path.write_text(
        json.dumps(
            {
                "runner_top_level_defs": defs,
                "oversized_modules": oversized or {},
            }
        ),
        encoding="utf-8",
    )


def write_module(src_dir, name, lines):
    (src_dir / name).write_text("x = 1\n" * lines, encoding="utf-8")


def test_new_runner_definition_fails_but_nested_definition_does_not(tmp_path, capsys):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "runner.py").write_text(
        "def existing():\n    def nested():\n        return None\n    return nested\n\n"
        "def added():\n    return None\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, defs=1)

    result = load_tool().main(
        ["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline
    )

    assert result == 1
    assert "runner_top_level_defs" in capsys.readouterr().out


def test_baselined_file_must_not_grow_but_can_shrink(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "runner.py").write_text("", encoding="utf-8")
    write_module(src_dir, "large.py", 1002)
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, defs=0, oversized={"large.py": 1002})

    tool = load_tool()
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline) == 0
    write_module(src_dir, "large.py", 1001)
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline) == 0
    write_module(src_dir, "large.py", 1003)
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline) == 1


def test_new_module_crossing_cap_fails_but_boundary_passes(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "runner.py").write_text("", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, defs=0)
    write_module(src_dir, "new.py", 1000)
    tool = load_tool()
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline) == 0
    write_module(src_dir, "new.py", 1001)
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=baseline) == 1


def test_update_records_current_values_and_prunes_small_files(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "runner.py").write_text("def one():\n    pass\n", encoding="utf-8")
    write_module(src_dir, "large.py", 1000)
    write_module(src_dir, "still_large.py", 1001)
    baseline = tmp_path / "baseline.json"
    write_baseline(baseline, defs=99, oversized={"large.py": 1001, "gone.py": 2000})

    assert load_tool().main(
        ["size_ratchet.py", "--update"], src_dir=src_dir, baseline_file=baseline
    ) == 0
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "runner_top_level_defs": 1,
        "oversized_modules": {"still_large.py": 1001},
    }


def test_script_entrypoint_exits_successfully(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(TOOL)])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(TOOL), run_name="__main__")
    assert excinfo.value.code == 0


def test_missing_and_malformed_baselines_fail(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "runner.py").write_text("", encoding="utf-8")
    tool = load_tool()
    missing = tmp_path / "missing.json"
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=missing) == 1
    malformed = tmp_path / "malformed.json"
    malformed.write_text("[]", encoding="utf-8")
    assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=malformed) == 1
    for value in (
        "not json",
        json.dumps({"runner_top_level_defs": 0}),
        json.dumps({"runner_top_level_defs": 0, "oversized_modules": {"x": True}}),
        json.dumps({"runner_top_level_defs": True, "oversized_modules": {}}),
    ):
        malformed.write_text(value, encoding="utf-8")
        assert tool.main(["size_ratchet.py"], src_dir=src_dir, baseline_file=malformed) == 1


def test_invalid_arguments_source_and_runner_fail_fast(tmp_path, capsys):
    tool = load_tool()
    assert tool.main(["size_ratchet.py", "one", "two"], src_dir=tmp_path) == 1
    assert tool.main(["size_ratchet.py"], src_dir=tmp_path / "missing") == 1
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "runner.py").write_text("def broken(:\n", encoding="utf-8")
    assert tool.main(["size_ratchet.py"], src_dir=broken) == 1
    assert "size ratchet" in capsys.readouterr().err
