"""파일 내용으로 형식을 가르고 부분별 글을 준다. 예외를 밖으로 내지 않는다."""
import os
import zipfile
from dataclasses import dataclass, field

from . import hancom, ooxml, pdf

_DOCUMENT_ERRORS = (hancom.DocumentError, pdf.DocumentError, ooxml.DocumentError)
_CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass
class Extracted:
    """parts는 (단위, 글) 목록이다. 대조 단위: hwp·hwpx·docx는 파일 전체(단위 None), PDF는 쪽 번호, pptx는 슬라이드 번호, xlsx는 칸(`시트번호:시트이름!셀`).

    줄바꿈은 문단 경계이고 line_wrapped가 참인 PDF만 조판 줄이다.
    """

    kind: str | None = None
    parts: list = field(default_factory=list)
    line_wrapped: bool = False
    image_only_pages: list = field(default_factory=list)
    embedded_images: int = 0
    error: str | None = None


def looks_like_document(head: bytes) -> bool:
    """확장자를 모르는 파일을 열어 볼 값이 있는지 머리 바이트로 본다."""
    return head.startswith((b"PK\x03\x04", _CFB_MAGIC)) or b"%PDF-" in head[:1024]


def _whole_file(kind: str, text) -> Extracted:
    return Extracted(kind=kind, parts=[(None, "\n".join(text.paragraphs))], embedded_images=text.images)


def _from_zip(path) -> Extracted:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return Extracted(error="corrupt")
    if hancom.is_hwpx(names):
        return _whole_file("hwpx", hancom.hwpx_text(path))
    if "word/document.xml" in names:
        return _whole_file("docx", ooxml.docx_text(path))
    if "ppt/presentation.xml" in names:
        slides = ooxml.pptx_slides(path)
        return Extracted(kind="pptx", parts=list(enumerate(slides.texts, 1)), embedded_images=slides.images)
    if "xl/workbook.xml" in names:
        workbook = ooxml.xlsx_workbook(path)
        parts = [(f"{sheet.number}:{sheet.name}!{cell.address}", cell.line) for sheet in workbook.sheets for cell in sheet.cells]
        return Extracted(kind="xlsx", parts=parts, embedded_images=workbook.images)
    return Extracted(error="unsupported")


def _from_pdf(path) -> Extracted:
    pages = pdf.read_pages(path)
    return Extracted(kind="pdf", parts=list(enumerate(pages.texts, 1)), line_wrapped=True, image_only_pages=pages.image_only)


def _route(path) -> Extracted:
    with open(path, "rb") as stream:
        head = stream.read(1024)
    if head.startswith(b"PK"):
        return _from_zip(path)
    if head.startswith(_CFB_MAGIC):
        return _whole_file("hwp", hancom.hwp_text(path))
    if b"%PDF-" in head:
        return _from_pdf(path)
    if hancom.is_hwp3(head):
        return Extracted(error="unsupported")
    return Extracted(error="not_a_document")


def extract(path) -> Extracted:
    path = os.fspath(path)
    try:
        return _route(path)
    except _DOCUMENT_ERRORS as error:
        return Extracted(error=error.reason)
    except OSError:
        return Extracted(error="io")
    except Exception:  # 낯선 파일 하나가 전체 검색을 멈추지 못하게 한다
        return Extracted(error="corrupt")
