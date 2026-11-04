"""PDF를 pypdfium2로 읽는다. pdfium은 프로세스 안에서만 연다(작업자 격리의 전제)."""
from typing import NamedTuple

# 공백을 뺀 글자가 이보다 적은 쪽은 사실상 그림이다(쪽 번호 같은 조각은 글로 치지 않는다).
IMAGE_ONLY_BELOW = 20


class DocumentError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Pages(NamedTuple):
    texts: list
    image_only: list


def _open(path):
    import pypdfium2

    try:
        return pypdfium2.PdfDocument(str(path))
    except pypdfium2.PdfiumError as error:
        raise DocumentError("encrypted" if "password" in str(error).lower() else "corrupt") from error


def _page_text(document, index: int) -> str:
    import pypdfium2

    try:
        page = document[index]
    except pypdfium2.PdfiumError:
        return ""
    try:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
        finally:
            textpage.close()
    except pypdfium2.PdfiumError:
        return ""
    finally:
        page.close()


def read_pages(path) -> Pages:
    document = _open(path)
    try:
        texts = [_page_text(document, index) for index in range(len(document))]
    finally:
        document.close()
    image_only = [number for number, text in enumerate(texts, 1) if sum(not c.isspace() for c in text) < IMAGE_ONLY_BELOW]
    return Pages(texts, image_only)
