"""image: 쪽 그림과 내장 그림을 PNG로 만든다. `.documents` 관리."""
import hashlib
import io
import os
import re
import time
import warnings
from pathlib import Path

from . import extract, ooxml, pdf, rhwp
from .extract import ToolUnavailable, Unreadable, UsageProblem

__all__ = ["make_images", "ToolUnavailable", "Unreadable", "UsageProblem"]

_OLD_AFTER_SECONDS = 86400
_MEMBER_LIMIT = 256 * 1024 * 1024
_NAME_BYTES_LIMIT = 180
_KEPT_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
_DOCTOR_HINT = "`doctor --fix`로 설치하거나 상태를 확인한다"
_BAD_NAME = re.compile(r'[/\\\x00-\x1f]')


def _prepare_default_folder() -> Path:
    base = Path.cwd() / ".documents"
    folder = base / "pages"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Unreadable("io", f"작업 폴더에 그림을 쓸 수 없다: {error}. `--out-dir`로 쓸 곳을 준다") from error
    ignore = base / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass  # 흔적 숨김을 못 해도 그림은 쓴다
    return folder


def _drop_old_pictures(folder: Path) -> None:
    limit = time.time() - _OLD_AFTER_SECONDS
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < limit:
                os.unlink(entry.path)
        except OSError:
            continue


def _prefix(path: str) -> str:
    status = os.stat(path)
    return hashlib.sha256(f"{os.path.abspath(path)}\0{status.st_mtime_ns}".encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _size_of(path: Path) -> tuple:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


def _check_pages(pages, count: int) -> None:
    beyond = [page for page in pages if page > count]
    if beyond:
        raise UsageProblem(f"--pages가 범위 밖이다: {beyond[0]}. 이 문서는 {count}쪽이다")


def _render_pdf(path: str, pages, folder_of, prefix: str) -> dict:
    if not pages:
        raise UsageProblem("pdf는 --pages가 필요하다")
    try:
        _check_pages(pages, pdf.page_count(path))
        folder = folder_of()
        images = []
        for page in pages:
            target = folder / f"{prefix}_p{page:04}.png"
            if target.exists() and target.stat().st_size:
                width, height = _size_of(target)
            else:
                width, height = pdf.render_page(path, page, target)
            images.append({"page": page, "path": str(target.resolve()), "width": width, "height": height, "source_name": None})
    except pdf.DocumentError as error:
        raise Unreadable(error.reason, f"PDF를 열 수 없다: {error.reason}") from error
    except OSError as error:
        raise Unreadable("io", f"그림을 쓸 수 없다: {error}. `--out-dir`로 쓸 곳을 준다") from error
    return {"kind": "pdf", "source": "render", "page_basis": "physical", "images": images, "skipped": []}


def _render_hancom(path: str, kind: str, pages, folder_of, prefix: str) -> dict:
    if not pages:
        raise UsageProblem(f"{kind}는 --pages가 필요하다. 쪽 번호는 `read` 머리 줄의 page_count와 `search`의 pages로 안다")
    error_reason = extract.extract(path).error
    if error_reason:
        raise Unreadable(error_reason, f"읽을 수 없다: {error_reason}")
    binary = rhwp.find()
    if binary is None:
        raise ToolUnavailable(f"쪽 그림에는 rhwp가 필요하다. {_DOCTOR_HINT}")
    try:
        _check_pages(pages, rhwp.page_count(path, binary))
        folder = folder_of()
        images = []
        for page in pages:
            target = folder / f"{prefix}_p{page:04}.png"
            if not (target.exists() and target.stat().st_size):
                rhwp.render_page(path, binary, page, target)
            width, height = _size_of(target)
            images.append({"page": page, "path": str(target.resolve()), "width": width, "height": height, "source_name": None})
    except rhwp.ToolError as error:
        raise ToolUnavailable(f"rhwp 실패: {error}. {_DOCTOR_HINT}") from error
    except OSError as error:
        raise Unreadable("io", f"그림을 쓸 수 없다: {error}. `--out-dir`로 쓸 곳을 준다") from error
    return {"kind": kind, "source": "render", "page_basis": "estimated", "images": images, "skipped": []}


def _safe_stem(name: str) -> str:
    stem = _BAD_NAME.sub("_", os.path.basename(name).rsplit(".", 1)[0]) or "image"
    encoded = stem.encode("utf-8", "ignore")
    return encoded[:_NAME_BYTES_LIMIT].decode("utf-8", "ignore") or "image"


def _write_member(path: str, member: str, page, folder: Path, prefix: str, used: set) -> dict:
    """그림 항목 하나를 쓴다. 쓸 수 없으면 사유를 담은 skipped 항목을 준다."""
    from PIL import Image

    source_name = os.path.basename(member)
    data = ooxml.read_member(path, member, _MEMBER_LIMIT)
    if data is None:
        return {"skipped": {"page": page, "source_name": source_name, "reason": "too_large"}}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                image.load()
                size = image.size
                suffix = _KEPT_FORMATS.get(image_format, ".png")
                stem = f"{prefix}_{_safe_stem(member)}"
                candidate = stem + suffix
                counter = 1
                while candidate in used:
                    counter += 1
                    candidate = f"{stem}_{counter}{suffix}"
                used.add(candidate)
                target = folder / candidate
                if image_format in _KEPT_FORMATS:
                    target.write_bytes(data)
                else:
                    image.save(target, "PNG")  # BMP 등은 Read가 못 여니 PNG로 바꾼다
    except OSError as error:
        if isinstance(error, PermissionError):
            raise Unreadable("io", f"그림을 쓸 수 없다: {error}. `--out-dir`로 쓸 곳을 준다") from error
        return {"skipped": {"page": page, "source_name": source_name, "reason": "unsupported_format"}}
    except (ValueError, SyntaxError, Image.DecompressionBombError):
        return {"skipped": {"page": page, "source_name": source_name, "reason": "unsupported_format"}}
    return {"image": {"page": page, "path": str(target.resolve()), "width": size[0], "height": size[1], "source_name": source_name}}


def _collect(path: str, members, folder_of, prefix: str) -> tuple:
    images, skipped = [], []
    if not members:
        return images, skipped
    folder = folder_of()
    used = set()
    for member, page in members:
        outcome = _write_member(path, member, page, folder, prefix, used)
        if "image" in outcome:
            images.append(outcome["image"])
        else:
            skipped.append(outcome["skipped"])
    return images, skipped


def _embedded(path: str, kind: str, pages, folder_of, prefix: str) -> dict:
    try:
        if kind == "pptx":
            slides = ooxml.slide_media(path)
            if pages:
                _check_pages(pages, len(slides))
            members = [(member, number) for number, names in enumerate(slides, 1) if not pages or number in pages for member in names]
            basis = "slide"
        else:
            if pages:
                raise UsageProblem(f"{kind}에는 쪽이 없다. 내장 그림을 모두 꺼낸다")
            members = [(member, None) for member in ooxml.media_names(path, kind)]
            basis = "none"
        images, skipped = _collect(path, members, folder_of, prefix)
    except ooxml.DocumentError as error:
        raise Unreadable(error.reason, f"읽을 수 없다: {error.reason}") from error
    return {"kind": kind, "source": "embedded", "page_basis": basis, "images": images, "skipped": skipped}


def make_images(path: str, *, pages=None, out_dir=None) -> dict:
    if not os.path.exists(path):
        raise UsageProblem(f"없는 경로: {path}")
    if os.path.isdir(path):
        raise UsageProblem(f"폴더는 받지 않는다. 파일을 준다: {path}")
    try:
        kind = extract.sniff(path)
    except OSError as error:
        raise Unreadable("io", f"파일을 읽을 수 없다: {error}") from error
    if kind is None or kind in ("hwp3", "hml", "xls"):
        if kind is None and extract.has_document_extension(path):
            reason = extract.extract(path).error or "not_a_document"
            raise Unreadable(reason, f"읽을 수 없다: {reason}")
        raise UsageProblem(f"대상 형식이 아니다(hwp·hwpx·pdf·docx·pptx·xlsx). 그림 파일은 Read로 바로 연다: {path}")

    def folder_of() -> Path:
        if out_dir is not None:
            folder = Path(out_dir)
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise Unreadable("io", f"--out-dir에 쓸 수 없다: {error}") from error
            return folder
        folder = _prepare_default_folder()
        _drop_old_pictures(folder)
        return folder

    prefix = _prefix(path)
    if kind == "pdf":
        return _render_pdf(path, pages, folder_of, prefix)
    if kind in ("hwp", "hwpx"):
        return _render_hancom(path, kind, pages, folder_of, prefix)
    return _embedded(path, kind, pages, folder_of, prefix)
