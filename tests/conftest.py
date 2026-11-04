import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / ".claude/skills/documents/scripts/cli.py"


class Run:
    def __init__(self, completed):
        self.code = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    @property
    def json(self):
        return json.loads(self.out)

    @property
    def lines(self):
        return [json.loads(line) for line in self.out.splitlines() if line.strip()]


@pytest.fixture
def home(tmp_path):
    """스킬의 설치본이 없는 빈 홈."""
    path = tmp_path / "빈홈"
    path.mkdir()
    return path


@pytest.fixture
def cli(home, tmp_path):
    """실제 하위 프로세스로 cli.py를 부른다. 기본은 rhwp가 PATH에 없는 환경이다."""

    def run(*args, cwd=None, path_prefix=None, env=None, timeout=120, stdin=None):
        environment = {k: v for k, v in os.environ.items() if k not in ("PATH", "HOME", "COLUMNS")}
        system_path = "/usr/bin:/bin"
        environment["PATH"] = f"{path_prefix}:{system_path}" if path_prefix else system_path
        environment["HOME"] = str(home)
        environment.update(env or {})
        completed = subprocess.run(
            [sys.executable, str(CLI), *map(str, args)],
            cwd=cwd or tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=timeout,
            stdin=stdin,
        )
        return Run(completed)

    return run


FAKE_RHWP = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_RHWP_MODE", "ok")
if args[:1] == ["--version"]:
    print("rhwp v" + os.environ.get("FAKE_RHWP_VERSION", "0.8.7"))
    sys.exit(0)
if mode == "fail":
    print("오류: 실패", file=sys.stderr)
    sys.exit(1)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "truncated":
    print(json.dumps({{"pageCount": 1, "pages": [], "truncated": True, "omittedCount": 1}}))
    sys.exit(0)
if args[:1] == ["export-text"]:
    print("LAYOUT_OVERFLOW 잡음", file=sys.stderr)
    pages = json.loads(os.environ["FAKE_RHWP_PAGES"])
    print(json.dumps({{"pageCount": len(pages), "pages": [{{"page": i, "text": t}} for i, t in enumerate(pages)], "truncated": False, "omittedCount": 0}}))
    sys.exit(0)
sys.exit(9)
"""


@pytest.fixture
def fake_rhwp(tmp_path):
    folder = tmp_path / "가짜도구"
    folder.mkdir()
    binary = folder / "rhwp"
    binary.write_text(FAKE_RHWP.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return folder
