"""PDF를 pypdfium2로 읽는다. pdfium은 프로세스 안에서만 연다(작업자 격리의 전제)."""
from typing import NamedTuple

# 공백을 뺀 글자가 이보다 적은 쪽은 사실상 그림이다(쪽 번호 같은 조각은 글로 치지 않는다).
IMAGE_ONLY_BELOW = 20
# 다단 판별: 쪽의 위아래 10%(머리·꼬리)를 빼고 본 글자 위치에서, 가운데 띠 안의 빈 세로 틈 하나로 좌우가 갈리는가.
_LAYOUT_MARGIN = 0.1
_GUTTER_MIN = 0.012
_GUTTER_BAND = (0.3, 0.75)
_EDGE_BAND = (0.15, 0.85)
_SIDE_SEGMENTS_MIN = 6
_SEGMENTS_MIN = 8


class DocumentError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Pages(NamedTuple):
    texts: list
    image_only: list


class PageLayout(NamedTuple):
    images: int
    image_area: float  # 쪽 면적 대비 그림 비율
    multi_column: bool


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


def page_count(path) -> int:
    document = _open(path)
    try:
        return len(document)
    finally:
        document.close()


def _gutters(segments, width: float) -> list:
    occupied = bytearray(int(width) + 2)
    for left, _, right, _ in segments:
        occupied[max(0, int(left)) : min(int(width) + 1, int(right) + 1)] = b"\x01" * (min(int(width) + 1, int(right) + 1) - max(0, int(left)))
    columns = [index for index, value in enumerate(occupied) if value]
    if not columns:
        return []
    gaps = []
    start = None
    for index in range(columns[0], columns[-1] + 1):
        if not occupied[index]:
            start = index if start is None else start
        elif start is not None:
            if index - start >= width * _GUTTER_MIN:
                gaps.append((start, index))
            start = None
    return gaps


def _is_multi_column(segments, width: float, height: float) -> bool:
    # 성진: 글자 위치만 보는 휴리스틱이다. 좌우 단을 가로지르는 제목이 있으면 놓치고 칸이 넓은 2열 표에 오경보를 낸다(400개 PDF 2,032쪽 중 약 1.5%). 실제 사례가 쌓이면 문턱을 다시 잰다.
    body = [s for s in segments if s[2] > s[0] and height * _LAYOUT_MARGIN < (s[1] + s[3]) / 2 < height * (1 - _LAYOUT_MARGIN)]
    if len(body) < _SEGMENTS_MIN:
        return False
    # 쪽 끝의 책갈피 탭 같은 틈은 세지 않는다.
    gaps = [(a, b) for a, b in _gutters(body, width) if b > width * _EDGE_BAND[0] and a < width * _EDGE_BAND[1]]
    if len(gaps) != 1:
        return False
    start, end = gaps[0]
    if start < width * _GUTTER_BAND[0] or end > width * _GUTTER_BAND[1]:
        return False
    left = sum(1 for s in body if s[2] <= end)
    right = sum(1 for s in body if s[0] >= start)
    return left >= _SIDE_SEGMENTS_MIN and right >= _SIDE_SEGMENTS_MIN


def _layout_of(page) -> PageLayout:
    from pypdfium2 import raw

    width, height = page.get_size()
    objects = list(page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=5))
    area = 0.0
    for item in objects:
        try:
            left, bottom, right, top = item.get_bounds()
        except Exception:  # 상자가 없는 객체는 면적에서 뺀다
            continue
        area += max(0.0, min(right, width) - max(left, 0.0)) * max(0.0, min(top, height) - max(bottom, 0.0))
    textpage = page.get_textpage()
    try:
        segments = [textpage.get_rect(index) for index in range(textpage.count_rects())]
    finally:
        textpage.close()
    return PageLayout(len(objects), round(min(1.0, area / (width * height)), 2) if width and height else 0.0, _is_multi_column(segments, width, height))


def page_layouts(path, pages) -> dict:
    """쪽 번호(1기준)마다 그림 수, 면적 비율, 다단 여부를 준다. kordoc의 그림 표기는 쪽 끝에 몰려 쓸 수 없다."""
    import pypdfium2

    document = _open(path)
    try:
        layouts = {}
        for number in pages:
            try:
                page = document[number - 1]
            except pypdfium2.PdfiumError:
                continue
            try:
                layouts[number] = _layout_of(page)
            finally:
                page.close()
        return layouts
    finally:
        document.close()


def render_page(path, number: int, target, long_side: int = 1568) -> tuple:
    """쪽을 긴 변 long_side로 그려 PNG로 쓰고 (너비, 높이)를 준다."""
    import pypdfium2

    document = _open(path)
    try:
        try:
            page = document[number - 1]
        except pypdfium2.PdfiumError as error:
            raise DocumentError("corrupt") from error
        try:
            scale = long_side / max(page.get_size())
            image = page.render(scale=scale).to_pil()
        finally:
            page.close()
    finally:
        document.close()
    if max(image.size) != long_side:  # 반올림 오차를 맞춘다
        ratio = long_side / max(image.size)
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))))
    image.save(target, "PNG")
    return image.size
