import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/export_public.py"
SPEC = importlib.util.spec_from_file_location("export_public", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def test_export_omits_private_files_and_history(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "public-files.txt").write_text("public-files.txt\nREADME.md\n")
    (root / "README.md").write_text("Fictional example only.")
    (root / "private").mkdir()
    (root / "private/resume.pdf").write_bytes(b"private resume")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("private repository metadata")
    output = tmp_path / "public"
    result = exporter.export(root, output, exporter.manifest_paths(root), [])
    assert result["files"] == 2
    assert sorted(p.name for p in output.iterdir()) == ["README.md", "public-files.txt"]
    with pytest.raises(ValueError, match="new directory"):
        exporter.export(root, output, exporter.manifest_paths(root), [])


def test_audit_rejects_private_markers_without_echoing_them(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("candidate-private-marker")
    findings = exporter.audit(tmp_path, [path], ["candidate-private-marker"])
    assert findings == [{"file": "README.md", "line": 1, "rule": "local_private_marker"}]


@pytest.mark.parametrize("entry", ["private/profile.json", "../outside.txt", "resume.pdf", ".env"])
def test_manifest_rejects_private_or_escaping_paths(tmp_path, entry):
    (tmp_path / "public-files.txt").write_text("public-files.txt\n" + entry + "\n")
    with pytest.raises(ValueError):
        exporter.manifest_paths(tmp_path)


def test_manifest_refuses_symlinked_directory(tmp_path):
    (tmp_path / "actual").mkdir()
    (tmp_path / "actual/file.md").write_text("example")
    (tmp_path / "docs").symlink_to("actual", target_is_directory=True)
    (tmp_path / "public-files.txt").write_text("public-files.txt\ndocs/file.md\n")
    with pytest.raises(ValueError, match="symlinks"):
        exporter.manifest_paths(tmp_path)
