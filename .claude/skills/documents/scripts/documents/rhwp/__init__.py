"""rhwp CLI를 찾고 부른다. 쪽은 밖으로 나갈 때 1기준이다."""
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

VERSION = "0.8.7"
PAGE_TEXT_TIMEOUT = 30.0
_VERSION_TIMEOUT = 10.0


class ToolError(Exception):
    """rhwp가 없거나 실패했거나 결과를 믿을 수 없다."""


def installed_path() -> Path:
    return Path.home() / ".cache/documents/bin/rhwp"


def _run(arguments, *, timeout: float) -> subprocess.CompletedProcess:
    """새 세션에서 돌려 시간이 지나면 자손까지 죽인다. stderr의 LAYOUT_OVERFLOW 잡음은 버린다."""
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ToolError(f"rhwp가 {timeout:g}초 안에 끝나지 않았다") from error
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, None)


def _version_of(binary: str) -> str | None:
    try:
        output = _run([binary, "--version"], timeout=_VERSION_TIMEOUT).stdout.decode("utf-8", "replace")
    except (OSError, ToolError):
        return None
    return output.strip().rpartition("v")[2] or None


def find() -> str | None:
    """고정 판의 실행 파일을 찾는다. 다른 판은 쪽이 달라질 수 있어 쓰지 않는다."""
    candidates = [str(installed_path())]
    on_path = shutil.which("rhwp")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if os.access(candidate, os.X_OK) and _version_of(candidate) == VERSION:
            return candidate
    return None


def page_texts(path, binary: str, *, timeout: float = PAGE_TEXT_TIMEOUT) -> list:
    """쪽별 글자를 쪽 순서대로 준다(batch에는 쪽별 글자가 없어 파일마다 단일 호출한다)."""
    try:
        completed = _run([binary, "export-text", os.fspath(path), "--json"], timeout=timeout)
    except OSError as error:
        raise ToolError(str(error)) from error
    if completed.returncode != 0:
        raise ToolError(f"rhwp 종료 코드 {completed.returncode}")
    try:
        document = json.loads(completed.stdout)
        if document.get("truncated"):
            raise ToolError("rhwp 결과가 잘렸다")
        return [page["text"] for page in sorted(document["pages"], key=lambda page: page["page"])]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ToolError("rhwp 결과를 읽을 수 없다") from error
