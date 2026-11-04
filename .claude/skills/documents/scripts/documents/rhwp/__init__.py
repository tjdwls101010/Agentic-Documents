"""rhwp CLI를 찾고 부른다. 쪽은 밖으로 나갈 때 1기준이다."""
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

VERSION = "0.8.7"
PAGE_TEXT_TIMEOUT = 30.0
_MARKDOWN_TIMEOUT = 60.0
_PNG_TIMEOUT = 60.0
_DOWNLOAD_TIMEOUT = 300
_VERSION_TIMEOUT = 10.0

_RELEASE = "https://github.com/edwardkim/rhwp/releases/download/v{version}/{name}"
_ARCHIVES = {
    ("Darwin", "arm64"): ("rhwp-v0.8.7-macos-aarch64.tar.gz", "6ed46aa63f6d10c447842999f73abc59cf9586c5645fefc5e763bd6a6d43662d"),
    ("Darwin", "x86_64"): ("rhwp-v0.8.7-macos-x86_64.tar.gz", "07b1a5ea570e410d1b52b4d848a94f00c64674e84004883c62efad7daab61da4"),
    ("Linux", "x86_64"): ("rhwp-v0.8.7-linux-x86_64.tar.gz", "f1e541a25cfe76b6bc1daf18bf9c1f9a5ac5e03e2c01b188daaf54107214f631"),
    ("Linux", "aarch64"): ("rhwp-v0.8.7-linux-aarch64.tar.gz", "c4a70f5ed11b99586ee89d1ac9069145d58a0831d00e87858bb01626e2ebf313"),
    ("Windows", "AMD64"): ("rhwp-v0.8.7-windows-x86_64.zip", "f7ac326e98f595ae8fecd8da7a614ee9a37db77bdf938782a8e07c9da473ec6f"),
}


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


class MarkdownPages(NamedTuple):
    page_count: int
    pages: dict  # 쪽 번호(1기준) -> Markdown


def _short_link(path, folder: str) -> str:
    """파일 시스템에 따라 원본 이름이 240바이트에 가까우면 rhwp의 출력 이름이 한도를 넘는다. 짧은 이름의 심링크로 부른다."""
    with open(path, "rb") as stream:
        suffix = ".hwpx" if stream.read(2) == b"PK" else ".hwp"
    link = os.path.join(folder, "d" + suffix)
    os.symlink(os.path.abspath(path), link)
    return link


def markdown_pages(path, binary: str, pages=None) -> MarkdownPages:
    """쪽별 Markdown을 준다. pages(1기준)가 없으면 모든 쪽이다. 그림은 `![image N](…)`로 나오고 임시 assets는 버린다."""
    with tempfile.TemporaryDirectory(prefix="documents-rhwp-") as folder:
        link = _short_link(path, folder)
        output = os.path.join(folder, "out")
        collected = {}
        page_count = None
        selections = [None] if pages is None else list(pages)
        for page in selections:
            arguments = [binary, "export-markdown", link, "-o", output, "--json"]
            if page is not None:
                arguments += ["-p", str(page - 1)]
            try:
                completed = _run(arguments, timeout=_MARKDOWN_TIMEOUT)
            except OSError as error:
                raise ToolError(str(error)) from error
            if completed.returncode != 0:
                raise ToolError(f"rhwp 종료 코드 {completed.returncode}")
            try:
                manifest = json.loads(completed.stdout)
                page_count = manifest["pageCount"]
                for entry in manifest["pages"]:
                    with open(entry["path"], encoding="utf-8") as stream:
                        collected[entry["page"] + 1] = stream.read()
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise ToolError("rhwp 결과를 읽을 수 없다") from error
        return MarkdownPages(page_count, collected)


def page_count(path, binary: str) -> int:
    return len(page_texts(path, binary))


def render_page(path, binary: str, page: int, target) -> None:
    """쪽(1기준)을 PNG로 그려 target에 옮긴다. 한 쪽이면 `원본이름.png`가 되므로 빈 임시 폴더에서 집는다."""
    with tempfile.TemporaryDirectory(prefix="documents-rhwp-") as folder:
        link = _short_link(path, folder)
        output = os.path.join(folder, "out")
        try:
            completed = _run([binary, "export-png", link, "-o", output, "-p", str(page - 1), "--vlm-target", "claude"], timeout=_PNG_TIMEOUT)
        except OSError as error:
            raise ToolError(str(error)) from error
        if completed.returncode != 0:
            raise ToolError(f"rhwp 종료 코드 {completed.returncode}")
        made = [name for name in os.listdir(output) if name.endswith(".png")] if os.path.isdir(output) else []
        if len(made) != 1:
            raise ToolError("rhwp가 그림을 만들지 않았다")
        shutil.copyfile(os.path.join(output, made[0]), target)


def status() -> dict:
    candidates = [str(installed_path())]
    on_path = shutil.which("rhwp")
    if on_path:
        candidates.append(on_path)
    seen = None
    for candidate in candidates:
        if not os.access(candidate, os.X_OK):
            continue
        version = _version_of(candidate)
        if version == VERSION:
            return {"path": candidate, "version": version, "ok": True}
        seen = seen or {"path": candidate, "version": version, "ok": False}
    return seen or {"path": None, "version": None, "ok": False}


def install() -> None:
    """고정 판을 내려받아 해시를 검사하고 ~/.cache/documents/bin/rhwp에 둔다."""
    archive = _ARCHIVES.get((platform.system(), platform.machine()))
    if archive is None:
        raise ToolError(f"이 플랫폼의 rhwp 배포본이 없다: {platform.system()} {platform.machine()}")
    name, expected = archive
    target = installed_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="documents-rhwp-") as folder:
        downloaded = os.path.join(folder, name)
        request = urllib.request.Request(_RELEASE.format(version=VERSION, name=name), headers={"User-Agent": "documents-skill"})
        try:
            with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response, open(downloaded, "wb") as stream:
                shutil.copyfileobj(response, stream)
        except OSError as error:
            raise ToolError(f"rhwp를 내려받지 못했다: {error}") from error
        digest = hashlib.sha256()
        with open(downloaded, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ToolError("내려받은 rhwp의 해시가 다르다")
        temporary = target.with_name(target.name + ".part")
        with open(temporary, "wb") as output:
            _copy_binary(downloaded, output)
        temporary.chmod(0o755)
        os.replace(temporary, target)


def _copy_binary(archive_path: str, output) -> None:
    try:
        if archive_path.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                member = next(item for item in archive.namelist() if item.rsplit("/", 1)[-1] in ("rhwp", "rhwp.exe"))
                with archive.open(member) as stream:
                    shutil.copyfileobj(stream, output)
        else:
            with tarfile.open(archive_path) as archive:
                stream = archive.extractfile("rhwp/rhwp")
                shutil.copyfileobj(stream, output)
    except (KeyError, StopIteration, tarfile.TarError, zipfile.BadZipFile, TypeError, AttributeError) as error:
        raise ToolError("rhwp 압축 안에 실행 파일이 없다") from error
