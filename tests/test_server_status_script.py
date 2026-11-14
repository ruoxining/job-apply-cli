import importlib.util
from pathlib import Path
import shlex
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "server_status.py"
SPEC = importlib.util.spec_from_file_location("server_status", SCRIPT)
server_status = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server_status)


def test_sections_and_compose_rows_parse_remote_output():
    output = """__REPOSITORY__
present
__REVISION__
abc1234
__COMPOSE__
{"Service":"api","State":"running","Health":"healthy"}
{"Service":"worker","State":"running","Health":""}
__API__
{"service":{"status":"ok"}}
"""
    sections = server_status._sections(output)

    assert sections["repository"] == "present"
    assert sections["revision"] == "abc1234"
    assert [row["Service"] for row in server_status._compose_rows(sections["compose"])] == [
        "api", "worker"
    ]


def test_report_explains_missing_deployment(capsys):
    result = server_status._print_report("example-server", {"repository": "missing"})

    assert result == 2
    assert "NOT DEPLOYED" in capsys.readouterr().out


def test_remote_path_is_one_quoted_shell_argument(monkeypatch):
    remote_dir = "/srv/work space/$(unexpected); 'quoted'"
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, script=kwargs["input"])
        return SimpleNamespace(returncode=0, stdout="__REPOSITORY__\nmissing\n", stderr="")

    monkeypatch.setattr(server_status.sys, "argv", [
        "server_status.py", "--host", "example-server", "--remote-dir", remote_dir
    ])
    monkeypatch.setattr(server_status.subprocess, "run", fake_run)
    assert server_status.main() == 2
    assert shlex.split(captured["command"][-1]) == ["sh", "-s", "--", remote_dir]
    assert 'os.environ.get("API_TOKEN", "")' in captured["script"]
