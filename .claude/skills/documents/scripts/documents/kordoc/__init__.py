"""kordoc 워커를 부른다. CLI에는 OCR 끄기가 없어 `parse-worker --protocol 2`를 쓴다."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

VERSION = "4.24.43"
PROTOCOL = 2
DEFAULT_TIMEOUT = 120.0
_STATUS_TIMEOUT = 30
_INSTALL_TIMEOUT = 900


class ToolError(Exception):
    """kordoc이 없거나 프로토콜이 맞지 않거나 멈췄다."""


class ParseFailure(Exception):
    """kordoc이 이 파일을 못 읽었다."""

    def __init__(self, code: str | None, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Parsed(NamedTuple):
    markdown: str
    pages: list  # (쪽 번호, Markdown)
    page_count: int | None
    warnings: list  # 원본 모양 {code, message, page?}


def installed_root() -> Path:
    return Path.home() / ".cache/documents/kordoc"


def _package() -> Path:
    return installed_root() / "node_modules/kordoc"


def _cli() -> Path:
    return _package() / "dist/cli.js"


def _installed_version() -> str | None:
    try:
        return json.loads((_package() / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def _usable_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise ToolError("node가 없다")
    if _installed_version() != VERSION or not _cli().exists():
        raise ToolError(f"kordoc {VERSION}이 설치되어 있지 않다")
    return node


def _communicate(arguments, *, input_text: str | None, timeout: float, cwd: str | None = None) -> subprocess.CompletedProcess:
    """새 세션에서 돌려 시간이 지나면 프로세스 그룹째 죽인다."""
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ToolError(f"kordoc이 {timeout:g}초 안에 끝나지 않았다") from error
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def parse(path, options: dict, *, timeout: float = DEFAULT_TIMEOUT) -> Parsed:
    node = _usable_node()
    request = json.dumps({"id": 1, "cmd": "parse", "file": os.path.abspath(path), "options": options}, ensure_ascii=False)
    # 빈 임시 폴더를 작업 폴더로 삼아 kordoc의 부산물이 사용자 폴더에 남지 않게 한다.
    with tempfile.TemporaryDirectory(prefix="documents-kordoc-") as folder:
        try:
            completed = _communicate([node, str(_cli()), "parse-worker", "--protocol", str(PROTOCOL)], input_text=request + '\n{"cmd":"quit"}\n', timeout=timeout, cwd=folder)
        except OSError as error:
            raise ToolError(str(error)) from error
    lines = completed.stdout.splitlines()
    try:
        ready = json.loads(lines[0])
        if not ready.get("ready") or ready.get("protocol") != PROTOCOL:
            raise ToolError(f"kordoc 프로토콜이 다르다: {lines[0][:100]}")
        answer = json.loads(lines[1])
    except (IndexError, ValueError, AttributeError) as error:
        raise ToolError(f"kordoc 응답을 읽을 수 없다(종료 코드 {completed.returncode}): {completed.stderr.strip()[:200]}") from error
    if answer.get("error"):
        raise ToolError(f"kordoc 요청 오류: {answer['error'].get('code')}: {answer['error'].get('message')}")
    result = answer.get("result") or {}
    if not result.get("success"):
        raise ParseFailure(result.get("code"), result.get("error") or "kordoc이 읽지 못했다")
    pages = [(page["pageNumber"], page.get("markdown", "")) for page in result.get("pages") or []]
    page_count = result.get("pageCount")
    return Parsed(result.get("markdown") or "", pages, page_count, result.get("warnings") or [])


def node_status() -> dict:
    node = shutil.which("node")
    if node is None:
        return {"path": None, "version": None, "ok": False}
    try:
        version = _communicate([node, "--version"], input_text=None, timeout=_STATUS_TIMEOUT).stdout.strip()
    except (OSError, ToolError):
        return {"path": node, "version": None, "ok": False}
    return {"path": node, "version": version.lstrip("v") or None, "ok": bool(version)}


def status() -> dict:
    version = _installed_version()
    return {"path": str(_cli()) if _cli().exists() else None, "version": version, "ok": version == VERSION and _cli().exists()}


def ocr_status() -> dict:
    """OCR 모델 상태만 본다. 모델은 처음 `--ocr`을 쓸 때 kordoc이 받는다."""
    try:
        node = _usable_node()
        completed = _communicate([node, str(_cli()), "check-ocr-models", "--status-only"], input_text=None, timeout=_STATUS_TIMEOUT)
        return {"ready": bool(json.loads(completed.stdout).get("allReady"))}
    except (ToolError, OSError, ValueError, AttributeError):
        return {"ready": None}


def install() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise ToolError("npm이 없다. Node.js를 설치한다")
    root = installed_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        completed = _communicate([npm, "install", "--prefix", str(root), f"kordoc@{VERSION}", "--no-audit", "--no-fund"], input_text=None, timeout=_INSTALL_TIMEOUT)
    except OSError as error:
        raise ToolError(str(error)) from error
    if completed.returncode != 0:
        raise ToolError(f"kordoc 설치 실패: {completed.stderr.strip()[-300:]}")
