from pathlib import Path
import subprocess

import pytest

from arrmes.probe import command
from arrmes.protocol import Call, InvalidCall


def test_only_read_action_is_admitted_and_specials_default_is_translated():
    helper = Path("/trusted/ge_api.py")
    output = Path("/local/reports")
    cmd = command(Call("tv_missing", {"title": "The Last of Us"}), helper, output)
    assert "--exclude-specials" in cmd
    assert cmd[-3:] == ["tv-missing", "--", "The Last of Us"]
    cmd = command(Call("tv_missing", {"title": "X", "include_specials": True}), helper, output)
    assert "--exclude-specials" not in cmd
    for name, args in [("tv_search_missing", {"title": "X"}), ("now_playing", {}), ("waypoint_status", {})]:
        with pytest.raises(InvalidCall):
            command(Call(name, args), helper, output)


def test_title_cannot_inject_an_argument_or_shell_command(tmp_path):
    helper = tmp_path / "helper.py"
    # A real argparse process verifies the command boundary independently.
    helper.write_text('import argparse,json\np=argparse.ArgumentParser()\np.add_argument("--output-dir")\np.add_argument("--exclude-specials", action="store_true")\np.add_argument("operation")\np.add_argument("target")\na=p.parse_args()\nprint(json.dumps(vars(a)))\n')
    title = '--output-dir /elsewhere; $(touch injected)'
    cmd = command(Call("tv_missing", {"title": title}), helper, tmp_path / "reports")
    done = subprocess.run(cmd, capture_output=True, text=True, check=True)
    import json
    value = json.loads(done.stdout)
    assert value["target"] == title
    assert value["output_dir"] == str(tmp_path / "reports")
    assert not (tmp_path / "injected").exists()
