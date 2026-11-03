"""Published squash refs must match every original runtime source byte."""
from importlib.util import module_from_spec, spec_from_file_location
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("source_identity_repro", ROOT / "scripts/acceptance_source_identity.py")
resolver = module_from_spec(spec)
spec.loader.exec_module(resolver)
pytestmark = pytest.mark.skipif(not (ROOT / "experiments/acceptance-v4/evaluate_acceptance_v4-frozen.py").exists(), reason="Local benchmark evidence not checked into git")


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    def git(*arguments):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(tmp_path / "no-hooks"), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(tmp_path), *arguments], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q")
    source = tmp_path / "src/arrmes/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# synthetic source fixture\n")
    git("add", "src")
    git("commit", "-qm", "Source fixture")
    frozen = git("rev-parse", "HEAD")
    records = [{"file": "src/arrmes/__init__.py", "sha256": resolver.file_sha(source)}]
    manifest = tmp_path / "experiments/acceptance-v4/runtime-source-manifests.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"sources": [{"frozen_commit": frozen, "files": records, "source_manifest_sha256": resolver.object_sha(records)}]}))
    monkeypatch.setattr(resolver, "ROOT", tmp_path)
    monkeypatch.setattr(resolver, "MANIFEST_SHA256", resolver.file_sha(manifest))
    (tmp_path / "README.md").write_text("A later squash-style commit changes only metadata.\n")
    git("add", "README.md")
    git("commit", "-qm", "Documentation fixture")
    return tmp_path, frozen, git, manifest


def test_different_published_commit_with_identical_source_is_recorded_truthfully(source_repo):
    root, frozen, git, _ = source_repo
    actual = git("rev-parse", "HEAD")
    assert actual != frozen
    identity = resolver.resolve_source(root / "src", frozen, "HEAD")
    assert identity["commit"] == actual
    assert identity["frozen_equivalent_source_commit"] == frozen
    assert identity["source_byte_equivalence_verified"] is True


def test_wrong_published_source_or_mutated_local_source_is_rejected(source_repo):
    root, frozen, git, _ = source_repo
    (root / "src/arrmes/__init__.py").write_bytes(b"# changed source\n")
    with pytest.raises(ValueError, match="differs"):
        resolver.resolve_source(root / "src", frozen, "HEAD")
    git("add", "src")
    git("commit", "-qm", "Changed source fixture")
    with pytest.raises(ValueError, match="differs"):
        resolver.resolve_source(root / "src", frozen, "HEAD")


def test_untracked_python_source_and_manifest_tampering_are_rejected(source_repo):
    root, frozen, _, manifest = source_repo
    extra = root / "src/arrmes/extra.py"
    extra.write_text("# untracked module\n")
    with pytest.raises(ValueError, match="differs"):
        resolver.resolve_source(root / "src", frozen, "HEAD")
    extra.unlink()
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest was changed"):
        resolver.resolve_source(root / "src", frozen, "HEAD")


def test_ref_flags_and_unknown_frozen_commits_fail_closed(source_repo):
    root, _, _, _ = source_repo
    with pytest.raises(ValueError, match="simple published"):
        resolver.resolve_source(root / "src", "f" * 40, "--help")
    with pytest.raises(ValueError, match="Unknown"):
        resolver.resolve_source(root / "src", "f" * 40, "HEAD")


def test_actual_source_archive_extracts_without_git_and_preserves_unmanaged_paths(tmp_path, monkeypatch):
    destination = tmp_path.resolve() / "snapshot"
    monkeypatch.setattr(resolver, "git", lambda *args: (_ for _ in ()).throw(AssertionError("Snapshot must not need Git objects")))
    frozen = "e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4"
    source = resolver.extract_source_snapshot(frozen, destination)
    identity = resolver.resolve_source(source, frozen)
    assert identity["commit"] is None and identity["source_byte_equivalence_verified"]
    assert len(identity["files"]) == 35
    sentinel = destination / "unrelated-sentinel"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        resolver.extract_source_snapshot(frozen, destination)
    assert sentinel.read_bytes() == b"preserve"
    alias = tmp_path.resolve() / "alias"
    try:
        alias.symlink_to(destination, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            return  # Windows runner lacks symbolic-link privilege; other checks above still ran.
        raise
    with pytest.raises(ValueError, match="symlink"):
        resolver.extract_source_snapshot(frozen, alias / "new-child")
    assert not (destination / "new-child").exists() and sentinel.read_bytes() == b"preserve"


def test_actual_source_archive_tampering_fails_before_destination_creation(tmp_path, monkeypatch):
    folder = tmp_path.resolve() / "experiments/acceptance-v4"
    (folder / "sources").mkdir(parents=True)
    original = ROOT / "experiments/acceptance-v4"
    (folder / "runtime-source-archives.json").write_bytes((original / "runtime-source-archives.json").read_bytes())
    (folder / "sources/improved.jsonl").write_bytes((original / "sources/improved.jsonl").read_bytes() + b"mutation")
    monkeypatch.setattr(resolver, "ROOT", tmp_path.resolve())
    destination = tmp_path.resolve() / "new-parent/snapshot"
    with pytest.raises(ValueError, match="archive identity"):
        resolver.extract_source_snapshot("e5e76b80ac3f0c2f222bfcd629ae51c76a0cabd4", destination)
    assert not destination.parent.exists()


def test_actual_original_harness_and_five_first_pass_receipts_remain_unchanged():
    folder = ROOT / "experiments/acceptance-v4"
    assert resolver.file_sha(folder / "evaluate_acceptance_v4-frozen.py") == resolver.FROZEN_HARNESS_SHA256
    assert resolver.file_sha(folder / "runtime-source-manifests.json") == resolver.MANIFEST_SHA256
    expected = {
        "released-public": "d192e5681669a9a5c0c032d9354514a1368b163b1f39ffe906abc9bedf53012e",
        "improved-public": "a87d351b55741b2cd8010289a1f0ec0cb0b71209b9ddfacb926abe303b8494f6",
        "qwen06-base": "f1f159e725dd5495f87b5e1707e9f072978f4b14c3c3e27de60d74425766a6d8",
        "qwen4b-reference": "076f2000ce830f56989db95f0a36a99b2028e0d9203c231a8fdd459f078a638b",
        "deterministic": "f1187e52f9fb57f6c42ca6c571b81f9597d95799b7c592adb85128373c256f44",
    }
    for name, digest in expected.items():
        path = folder / (name + ".json")
        assert resolver.file_sha(path) == digest
        receipt = json.loads(path.read_text())
        assert receipt["complete"] and len(receipt["rows"]) == 1000
        assert receipt["harness_sha256"] == resolver.FROZEN_HARNESS_SHA256
        assert resolver.file_sha(folder / (name + ".raw.jsonl")) == receipt["raw_journal_sha256"]


def test_post_freeze_metadata_resolver_does_not_change_inference_or_scoring_functions():
    frozen = ast.parse((ROOT / "experiments/acceptance-v4/evaluate_acceptance_v4-frozen.py").read_text())
    current = ast.parse((ROOT / "scripts/evaluate_acceptance_v4.py").read_text())
    names = {"Grader", "call_object", "strict_json", "unique_pairs", "boundary", "grade_row", "control_sensitivity", "summarize", "quantiles", "clustered_interval", "group_scores", "detailed_groups", "timed_infer", "prompt_identity", "fault_injections", "artifact_identity", "read_dataset"}
    def bodies(tree):
        return {node.name: ast.dump(node, include_attributes=False) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names}
    assert set(bodies(frozen)) == names
    assert bodies(current) == bodies(frozen)


def test_original_run_loop_diff_is_limited_to_declared_metadata_resolution():
    frozen = ast.parse((ROOT / "experiments/acceptance-v4/evaluate_acceptance_v4-frozen.py").read_text())
    current = ast.parse((ROOT / "scripts/evaluate_acceptance_v4.py").read_text())
    old = next(node for node in frozen.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    new = next(node for node in current.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    class RemoveDeclaredMetadataOnlyChanges(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            return None if node.name == "identify_source" else self.generic_visit(node)
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "identify_source":
                return ast.Call(func=ast.Name(id="source_identity", ctx=ast.Load()),
                    args=[ast.Attribute(value=ast.Name(id="args", ctx=ast.Load()), attr=key, ctx=ast.Load()) for key in ["source_root", "runtime_revision"]], keywords=[])
            return self.generic_visit(node)
        def visit_Dict(self, node):
            omitted = {"harness_code_version", "original_first_pass_harness_sha256", "post_freeze_code_change"}
            pairs = [(key, value) for key, value in zip(node.keys, node.values) if not isinstance(key, ast.Constant) or key.value not in omitted]
            node.keys, node.values = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
            return self.generic_visit(node)
    normalized = RemoveDeclaredMetadataOnlyChanges().visit(copy.deepcopy(new))
    assert ast.dump(normalized, include_attributes=False) == ast.dump(old, include_attributes=False)
