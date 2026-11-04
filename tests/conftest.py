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
if os.environ.get("FAKE_RHWP_MAX_NAME") and args[0] in ("export-markdown", "export-png") and len(os.path.basename(args[1]).encode()) > int(os.environ["FAKE_RHWP_MAX_NAME"]):
    print("오류: 출력 이름이 파일 이름 한도를 넘는다", file=sys.stderr)
    sys.exit(1)
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
if args[:1] == ["export-markdown"]:
    pages = json.loads(os.environ["FAKE_RHWP_PAGES"])
    out = args[args.index("-o") + 1]
    os.makedirs(out, exist_ok=True)
    source = os.path.basename(args[1]).rsplit(".", 1)[0]
    chosen = [int(args[args.index("-p") + 1])] if "-p" in args else list(range(len(pages)))
    entries = []
    for page in chosen:
        target = os.path.join(out, f"{{source}}_{{page + 1:03}}.md")
        open(target, "w", encoding="utf-8").write(pages[page])
        entries.append({{"page": page, "path": target}})
    print(json.dumps({{"pageCount": len(pages), "pages": entries}}))
    sys.exit(0)
if args[:1] == ["export-png"]:
    from PIL import Image
    out = args[args.index("-o") + 1]
    os.makedirs(out, exist_ok=True)
    source = os.path.basename(args[1]).rsplit(".", 1)[0]
    page = int(args[args.index("-p") + 1])
    pages = json.loads(os.environ["FAKE_RHWP_PAGES"])
    if page >= len(pages):
        sys.exit(1)
    scale = float(args[args.index("--scale") + 1]) if "--scale" in args else 1.0
    Image.new("RGB", (int((120 + page) * scale), int(80 * scale))).save(os.path.join(out, source + ".png"))
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


FAKE_KORDOC = r"""
const fs = require("fs");
const lines = fs.readFileSync(0, "utf8").split("\n").filter(Boolean);
const mode = process.env.FAKE_KORDOC_MODE || "ok";
if (mode === "hang") {
  const child = require("child_process").spawn("sleep", ["300"], { stdio: "ignore" });
  fs.writeFileSync(process.env.FAKE_KORDOC_PIDFILE, process.pid + " " + child.pid);
  setInterval(() => {}, 1000);
} else if (mode === "bad_protocol") {
  console.log(JSON.stringify({ ready: true, protocol: 1 }));
} else if (mode === "crash") {
  process.exit(7);
} else {
  console.log(JSON.stringify({ ready: true, protocol: 2 }));
  const request = JSON.parse(lines[0]);
  if (process.env.FAKE_KORDOC_LOG) fs.writeFileSync(process.env.FAKE_KORDOC_LOG, JSON.stringify({ request, cwd: process.cwd() }));
  console.log(JSON.stringify({ id: request.id, result: JSON.parse(process.env.FAKE_KORDOC_RESULT) }));
}
"""


@pytest.fixture
def node_path():
    found = __import__("shutil").which("node")
    if not found:
        pytest.skip("node가 없다")
    return os.path.dirname(found)


@pytest.fixture
def fake_kordoc(home):
    """설치 경로에 가짜 kordoc을 둔다. 진짜 계약은 slow 시험이 본다."""
    folder = home / ".cache/documents/kordoc/node_modules/kordoc"
    (folder / "dist").mkdir(parents=True)
    (folder / "package.json").write_text(json.dumps({"version": "4.24.43"}), encoding="utf-8")
    (folder / "dist/cli.js").write_text(FAKE_KORDOC, encoding="utf-8")
    return folder


def kordoc_result(markdown="", pages=None, page_count=None, warnings=None, **extra):
    result = {"success": True, "markdown": markdown, "metadata": {"pageMode": "layout"}, "warnings": warnings or []}
    if pages is not None:
        result["pages"] = [{"pageNumber": number, "markdown": text} for number, text in pages]
    if page_count is not None:
        result["pageCount"] = page_count
    result.update(extra)
    return json.dumps(result)
