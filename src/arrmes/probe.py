"""Explicit research probe of the existing GET-only household tool.

This is not called by inference or the planner. It cannot dispatch another
operation. The installed helper owns authentication, API access and export
verification; its path is a trusted operator setting, never a model argument.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .protocol import Call, InvalidCall, normalized, validate_call


def command(call: Call, helper: Path, output: Path) -> list[str]:
    call = validate_call(call.name, call.arguments)
    if call.name != "tv_missing":
        raise InvalidCall("This probe accepts only the read-only tv_missing action")
    args = normalized(call)["arguments"]
    result = [sys.executable, str(helper), "--output-dir", str(output)]
    if not args["include_specials"]:
        result.append("--exclude-specials")
    # All options precede an explicit end-of-options separator. A title cannot
    # become a CLI flag and no shell ever parses the model's output.
    return result + ["tv-missing", "--", args["title"]]


def run(call: Call, helper: Path, output: Path) -> dict:
    if not helper.is_file():
        raise ValueError("The installed ge-api helper is required")
    invocation = command(call, helper, output)
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    completed = subprocess.run(invocation, capture_output=True, text=True, timeout=90, check=False)
    try:
        receipt = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("The helper did not return a JSON receipt") from exc
    if not isinstance(receipt, dict):
        raise ValueError("Invalid helper receipt")
    return {"probe": "existing-ge-api-tv-missing", "read_only": True,
            "helper_sha256": digest, "returncode": completed.returncode,
            "call": normalized(call), "receipt": receipt}
