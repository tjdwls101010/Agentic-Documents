"""read: 형식별 경로, 이어 읽기, 경고."""
import os
import re
import unicodedata
from typing import NamedTuple

from . import extract, kordoc, normalize, ooxml, pdf, rhwp
from .extract import ToolUnavailable, Unreadable, UsageProblem

__all__ = ["Reading", "read", "ToolUnavailable", "Unreadable", "UsageProblem"]

DEFAULT_MAX_CHARS = 20000
_OCR_SECONDS_PER_PAGE = 5
_OCR_SECONDS_BASE = 120

_IMAGE_MARK = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_EMPHASIS = re.compile(r"\*\*|</?u>")
_BLANK_LINES = re.compile(r"\n{3,}")
_SEPARATOR_CELL = re.compile(r":?-+:?")

# 성진: 값이 그림인 표를 잡는 휴리스틱이다. 머리 행을 뺀 데이터 행의 빈 칸이 이만큼이고 쪽에 그림이 있을 때, 그림이 쪽의 일정 비율을 넘거나 표의 칸이 많이 비어 있으면 경고한다.
# 400개 PDF 약 2,030쪽 중 89쪽(4.4%)에서 울린다. 면적·비율 없이 그림과 빈 칸만 보면 128쪽이고, 줄어든 39쪽은 대부분 연락처 표와 작은 로고였다. 바디워시 17쪽, 자사우대 35쪽, 국민성장펀드 7쪽을 잡는다. 놓치거나 오경보가 쌓이면 다시 잰다.
_EMPTY_CELLS_MIN = 2
_IMAGE_AREA_MIN = 0.02
_EMPTY_RATIO_MIN = 0.3
_TABLE_ROWS_MIN = 3
# 성진: 2열 표를 문장으로 오인한 쪽을 잡는 휴리스틱이다. 행이 이 수 이상이고 두 칸 모두 이 길이 이상인 행이 절반을 넘으면 경고한다(같은 표본 6쪽). 사례집 27쪽을 잡는다.
_SENTENCE_TABLE_ROWS_MIN = 4
_SENTENCE_CELL_CHARS = 20

_KORDOC_DROPPED_WARNINGS = {"HIDDEN_TEXT_FILTERED"}
_KORDOC_CODES_TO_REASON = {
    "ENCRYPTED": "encrypted",
    "DRM_PROTECTED": "encrypted",
    "EMPTY_INPUT": "not_a_document",
    "UNSUPPORTED_FORMAT": "unsupported",
    "DECOMPRESSION_BOMB": "too_large",
    "ZIP_BOMB": "too_large",
    "OUTPUT_TOO_LARGE": "too_large",
    "FILE_NOT_FOUND": "io",
}
# kordoc의 문구는 자기 CLI 옵션을 말한다. 이 도구에서 실행할 수 있는 길로 바꾼다.
_KORDOC_MESSAGES = {
    "NEEDS_OCR": "글자층이 없다. `image`로 보거나 `--ocr --pages`로 읽는다",
    "SKIPPED_IMAGE": "그림 영역의 내용이 글자로 나오지 않았다. `image`로 본다",
}
_DOCTOR_HINT = "`doctor --fix`로 설치하거나 상태를 확인한다"
_REASON_TEXT = {
    "encrypted": "암호가 걸려 있다",
    "distribution": "배포용 문서라 본문이 암호화되어 있다",
    "corrupt": "파일이 손상되었다",
    "not_a_document": "확장자와 달리 문서가 아니다",
    "unsupported": "지원하지 않는 형식이다",
    "too_large": "풀면 너무 커서 읽지 않았다",
    "io": "파일을 읽을 수 없다",
}


class Reading(NamedTuple):
    header: dict
    text: str


def _warning(code: str, pages, message: str) -> dict:
    return {"code": code, "pages": pages, "message": message}


def _unreadable(code: str) -> Unreadable:
    return Unreadable(code, _REASON_TEXT.get(code, code))


def clean_markdown(markdown: str) -> str:
    """그림 표기를 `[그림]`으로 바꾸고 강조 표기를 지운다."""
    text = _IMAGE_MARK.sub("[그림]", markdown)
    text = _EMPHASIS.sub("", text)
    return _BLANK_LINES.sub("\n\n", text).strip("\n")


def _tables(markdown: str) -> list:
    tables, current = [], []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            current.append([cell.strip() for cell in stripped[1:-1].split("|")])
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return [[row for row in table if not all(_SEPARATOR_CELL.fullmatch(cell) for cell in row)] for table in tables]


def _empty_cell_stats(markdown: str) -> tuple:
    """데이터 행의 빈 칸 수와, 표 가운데 가장 큰 빈 칸 비율을 준다."""
    total = 0
    highest = 0.0
    for rows in _tables(markdown):
        if len(rows) < _TABLE_ROWS_MIN or len(rows[0]) < 2:
            continue
        cells = [cell for row in rows[1:] for cell in row]
        empty = sum(1 for cell in cells if not cell)
        total += empty
        if cells:
            highest = max(highest, empty / len(cells))
    return total, highest


def _values_may_be_pictures(markdown: str, image_entry: dict | None) -> bool:
    if not image_entry:
        return False
    empty, ratio = _empty_cell_stats(markdown)
    if empty < _EMPTY_CELLS_MIN:
        return False
    area = image_entry.get("image_area")
    return area is None or area >= _IMAGE_AREA_MIN or ratio >= _EMPTY_RATIO_MIN


def _has_sentence_table(markdown: str) -> bool:
    for rows in _tables(markdown):
        if not rows or len(rows[0]) != 2 or len(rows) < _SENTENCE_TABLE_ROWS_MIN:
            continue
        long_rows = sum(1 for row in rows if len(row) == 2 and len(row[0]) >= _SENTENCE_CELL_CHARS and len(row[1]) >= _SENTENCE_CELL_CHARS)
        if long_rows * 2 >= len(rows):
            return True
    return False


def _heuristic_warnings(markdown_by_page: dict, images_by_page: dict, multi_column_pages=()) -> list:
    empty_pages = sorted(page for page, text in markdown_by_page.items() if _values_may_be_pictures(text, images_by_page.get(page)))
    layout_pages = sorted({*multi_column_pages, *(page for page, text in markdown_by_page.items() if _has_sentence_table(text))})
    warnings = []
    if empty_pages:
        warnings.append(_warning("empty_cells_with_images", empty_pages, "쪽에 그림이 있고 표에 빈 칸이 있다. 값이 그림일 수 있으니 `image`로 본다"))
    if layout_pages:
        warnings.append(_warning("layout_suspect", layout_pages, "다단이나 2쪽 펼침을 표로 오인했거나 읽기 순서가 섞였을 수 있다. `--no-tables`나 `image`로 본다"))
    return warnings


def _kordoc_warnings(raw: list, pages_read=()) -> list:
    merged = {}
    for item in raw:
        code = item.get("code") or "KORDOC"
        if code in _KORDOC_DROPPED_WARNINGS:
            continue
        entry = merged.setdefault(code, _warning(code, [], _KORDOC_MESSAGES.get(code, item.get("message", ""))))
        page = item.get("page")
        if isinstance(page, int) and page not in entry["pages"]:
            entry["pages"].append(page)
    for entry in merged.values():
        entry["pages"].sort()
    # 문서 전체에 대한 OCR 경고는 쪽을 달지 않는다. 읽은 쪽 전체에 대한 말이다.
    if "NEEDS_OCR" in merged and not merged["NEEDS_OCR"]["pages"]:
        merged["NEEDS_OCR"]["pages"] = sorted(pages_read)
    return list(merged.values())


def _same_name_pdf(path: str) -> str | None:
    folder, name = os.path.split(path)
    stem = normalize.document_stem(name)
    try:
        names = os.listdir(folder or os.curdir)
    except OSError:
        return None
    for other in sorted(names):
        if other != name and other.lower().endswith(".pdf") and normalize.document_stem(other) == stem:
            return os.path.join(folder, other)
    return None


def _check_pages(pages, count: int) -> None:
    beyond = [page for page in pages if page > count]
    if beyond:
        raise UsageProblem(f"--pages가 범위 밖이다: {beyond[0]}. 이 문서는 {count}쪽이다")


def _kordoc_parse(path: str, options: dict, *, timeout: float = kordoc.DEFAULT_TIMEOUT) -> kordoc.Parsed:
    try:
        return kordoc.parse(path, {"ocr": False, "images": False, "tableFormat": "gfm", **options}, timeout=timeout)
    except kordoc.ToolError as error:
        raise ToolUnavailable(f"{error}. {_DOCTOR_HINT}") from error


def _failure_reason(failure: kordoc.ParseFailure) -> Unreadable:
    return Unreadable(_KORDOC_CODES_TO_REASON.get(failure.code, "corrupt"), failure.message)


def _join_pages(markdown_by_page: dict) -> tuple:
    """쪽 머리를 붙여 조립한다. 쪽마다 조립된 글의 (시작, 끝) 위치도 준다."""
    pieces = []
    spans = {}
    position = 0
    for page in sorted(markdown_by_page):
        piece = f"[p.{page}]\n{markdown_by_page[page]}"
        if pieces:
            position += 2
        spans[page] = (position, position + len(piece))
        pieces.append(piece)
        position += len(piece)
    return "\n\n".join(pieces), spans


class _Assembled(NamedTuple):
    kind: str
    page_count: int | None
    page_basis: str
    text: str
    spans: dict  # 쪽 -> 조립된 글의 위치. 쪽이 없는 문서는 비어 있다
    images: dict  # 쪽 -> {"images", "image_area"}, 모르면 None
    warnings: list
    extra: dict


def _read_pdf(path: str, pages, no_tables: bool, ocr: bool) -> _Assembled:
    try:
        count = pdf.page_count(path)
    except pdf.DocumentError as error:
        raise _unreadable(error.reason) from error
    if pages:
        _check_pages(pages, count)
    options = {}
    if pages:
        options["pages"] = list(pages)
    if no_tables:
        options["tables"] = False
    if ocr:
        options["ocr"] = True
    timeout = _OCR_SECONDS_PER_PAGE * len(pages) + _OCR_SECONDS_BASE if ocr else kordoc.DEFAULT_TIMEOUT
    try:
        parsed = _kordoc_parse(path, options, timeout=timeout)
    except kordoc.ParseFailure as failure:
        if failure.code != "IMAGE_BASED_PDF":
            raise _failure_reason(failure) from failure
        # 글자층이 없는 PDF는 못 읽은 것이 아니라 그림으로 볼 문서다.
        selected = list(pages) if pages else list(range(1, count + 1))
        warning = _warning("NEEDS_OCR", selected, "글자층이 없다. `image`로 보거나 `--ocr --pages`로 읽는다")
        return _Assembled("pdf", count, "physical", "", {}, {}, [warning], {})
    markdown = {number: clean_markdown(text) for number, text in parsed.pages}
    raw_markdown = dict(parsed.pages)
    text, spans = _join_pages(markdown)
    try:
        layouts = pdf.page_layouts(path, sorted(raw_markdown))
    except pdf.DocumentError as error:
        raise _unreadable(error.reason) from error
    images = {page: {"images": layout.images, "image_area": layout.image_area} for page, layout in layouts.items() if layout.images}
    multi_column = [page for page, layout in layouts.items() if layout.multi_column]
    warnings = _heuristic_warnings(raw_markdown, images, multi_column)
    warnings += _kordoc_warnings(parsed.warnings, raw_markdown)
    return _Assembled("pdf", count, "physical", text, spans, images, warnings, {})


def _validate_by_extract(path: str) -> None:
    extracted = extract.extract(path)
    if extracted.error:
        raise _unreadable(extracted.error)


def _image_counts(markdown_by_page: dict) -> dict:
    counts = {}
    for page, text in markdown_by_page.items():
        count = len(_IMAGE_MARK.findall(text))
        if count:
            counts[page] = {"images": count, "image_area": None}
    return counts


def _read_hancom(path: str, kind: str, pages) -> _Assembled:
    _validate_by_extract(path)
    binary = rhwp.find()
    if pages:
        if binary is None:
            raise ToolUnavailable(f"쪽을 지정해 읽으려면 rhwp가 필요하다. {_DOCTOR_HINT}")
        try:
            count = rhwp.page_count(path, binary)
            _check_pages(pages, count)
            found = rhwp.markdown_pages(path, binary, pages)
        except rhwp.ToolError as error:
            raise ToolUnavailable(f"rhwp 실패: {error}. {_DOCTOR_HINT}") from error
        raw = {page: found.pages.get(page, "") for page in pages}
        text, spans = _join_pages({page: clean_markdown(value) for page, value in raw.items()})
        images = _image_counts(raw)
        warnings = [_warning("table_may_span_pages", list(pages), "쪽에 걸친 표는 쪽마다 통째로 나올 수 있다")]
        warnings += _heuristic_warnings(raw, images)
        return _Assembled(kind, count, "estimated", text, spans, images, warnings, {})
    try:
        parsed = _kordoc_parse(path, {})
    except kordoc.ParseFailure as failure:
        raise _failure_reason(failure) from failure
    text = clean_markdown(parsed.markdown)
    warnings = _kordoc_warnings(parsed.warnings)
    if binary is None:
        warnings.append(_warning("page_count_unavailable", [], f"쪽 수와 그림 쪽을 모른다. {_DOCTOR_HINT}"))
        return _Assembled(kind, None, "none", text, {}, None, warnings, {})
    try:
        found = rhwp.markdown_pages(path, binary)
    except rhwp.ToolError as error:
        warnings.append(_warning("page_count_unavailable", [], f"쪽 수와 그림 쪽을 모른다: {error}. {_DOCTOR_HINT}"))
        return _Assembled(kind, None, "none", text, {}, None, warnings, {})
    return _Assembled(kind, found.page_count, "estimated", text, {}, _image_counts(found.pages), warnings, {})


def _read_docx(path: str, pages) -> _Assembled:
    if pages:
        raise UsageProblem("docx에는 쪽이 없다. `--offset`으로 이어 읽는다")
    _validate_by_extract(path)
    try:
        parsed = _kordoc_parse(path, {})
    except kordoc.ParseFailure as failure:
        raise _failure_reason(failure) from failure
    return _Assembled("docx", None, "none", clean_markdown(parsed.markdown), {}, None, _kordoc_warnings(parsed.warnings), {})


def _read_pptx(path: str, pages) -> _Assembled:
    try:
        count = len(ooxml.slide_media(path))
    except ooxml.DocumentError as error:
        raise _unreadable(error.reason) from error
    if pages:
        _check_pages(pages, count)
    try:
        parsed = _kordoc_parse(path, {"pages": list(pages)} if pages else {})
    except kordoc.ParseFailure as failure:
        raise _failure_reason(failure) from failure
    raw = dict(parsed.pages)
    text, spans = _join_pages({page: clean_markdown(value) for page, value in raw.items()})
    images = _image_counts(raw)
    warnings = _heuristic_warnings(raw, images) + _kordoc_warnings(parsed.warnings, raw)
    return _Assembled("pptx", count, "slide", text, spans, images, warnings, {})


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _sheet_markdown(sheet) -> str:
    title = f"## {sheet.number}: {sheet.name}" + (f" ({sheet.dimension})" if sheet.dimension else " (빈 시트)")
    if not sheet.cells:
        return title
    rows = {}
    for cell in sheet.cells:
        letters = re.match(r"[A-Z]+", cell.address).group()
        rows.setdefault(int(cell.address[len(letters) :]), {})[letters] = _escape_cell(cell.line)
    columns = sorted({letters for row in rows.values() for letters in row}, key=lambda letters: (len(letters), letters))
    lines = [title, "| 행 | " + " | ".join(columns) + " |", "| --- | " + " | ".join("---" for _ in columns) + " |"]
    for number in sorted(rows):
        lines.append(f"| {number} | " + " | ".join(rows[number].get(letters, "") for letters in columns) + " |")
    return "\n".join(lines)


def _read_xlsx(path: str, pages) -> _Assembled:
    try:
        workbook = ooxml.xlsx_workbook(path)
    except ooxml.DocumentError as error:
        raise _unreadable(error.reason) from error
    count = len(workbook.sheets)
    if pages:
        _check_pages(pages, count)
    chosen = [sheet for sheet in workbook.sheets if not pages or sheet.number in pages]
    pieces = []
    spans = {}
    position = 0
    for sheet in chosen:
        piece = _sheet_markdown(sheet)
        if pieces:
            position += 2
        spans[sheet.number] = (position, position + len(piece))
        pieces.append(piece)
        position += len(piece)
    warnings = []
    if workbook.stale_formula_cache:
        warnings.append(_warning("stale_formula_cache", [], "통합 문서가 열 때 다시 계산하도록 표시되어 있다. 수식 칸의 값은 저장된 캐시이고 계산 결과로 단정할 수 없다"))
    extra = {"sheets": [{"n": sheet.number, "name": sheet.name, "dimension": sheet.dimension} for sheet in workbook.sheets]}
    return _Assembled("xlsx", count, "sheet", "\n\n".join(pieces), spans, {}, warnings, extra)


def _read_by_kordoc_only(path: str, kind: str, pages) -> _Assembled:
    if pages:
        raise UsageProblem(f"{kind}에는 쪽을 정할 도구가 없다. `--offset`으로 이어 읽는다")
    try:
        parsed = _kordoc_parse(path, {})
    except kordoc.ParseFailure as failure:
        raise _failure_reason(failure) from failure
    return _Assembled(kind, None, "none", clean_markdown(parsed.markdown), {}, None, _kordoc_warnings(parsed.warnings), {})


def _kind_of(path: str) -> str:
    if not os.path.exists(path):
        raise UsageProblem(f"없는 경로: {path}")
    if os.path.isdir(path):
        raise UsageProblem(f"폴더는 읽지 않는다. 파일을 준다: {path}")
    try:
        kind = extract.sniff(path)
    except OSError as error:
        raise _unreadable("io") from error
    if kind is not None:
        return kind
    if extract.has_document_extension(path):
        # 확장자는 문서인데 내용이 아니다. 왜 못 읽는지 판별기에게 듣는다.
        raise _unreadable(extract.extract(path).error or "not_a_document")
    raise UsageProblem(f"대상 형식이 아니다(hwp·hwpx·pdf·docx·pptx·xlsx). 평문이나 그림은 Read로 연다: {path}")


def read(path: str, *, pages=None, no_tables: bool = False, ocr: bool = False, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> Reading:
    kind = _kind_of(path)
    if no_tables and kind != "pdf":
        raise UsageProblem("--no-tables는 pdf 전용이다")
    if ocr and kind != "pdf":
        raise UsageProblem("--ocr은 pdf 전용이다. 다른 형식의 그림 속 글자는 `image`로 본다")
    if ocr and not pages:
        raise UsageProblem("--ocr에는 --pages가 필요하다(쪽당 1~3초가 든다)")
    if kind == "pdf":
        assembled = _read_pdf(path, pages, no_tables, ocr)
    elif kind in ("hwp", "hwpx"):
        assembled = _read_hancom(path, kind, pages)
    elif kind == "docx":
        assembled = _read_docx(path, pages)
    elif kind == "pptx":
        assembled = _read_pptx(path, pages)
    elif kind == "xlsx":
        assembled = _read_xlsx(path, pages)
    else:
        assembled = _read_by_kordoc_only(path, kind, pages)
    return _finish(path, assembled, offset, max_chars)


def _finish(path: str, assembled: _Assembled, offset: int, max_chars: int) -> Reading:
    total = len(assembled.text)
    end = offset + max_chars
    chunk = assembled.text[offset:end]
    returned = [page for page, (start, stop) in sorted(assembled.spans.items()) if start < end and stop > offset]
    if assembled.images is None:
        image_pages = None
    else:
        # 쪽이 있는 형식은 이번 조각의 쪽만 말해 머리 줄이 쪽 수에 비례해 자라지 않게 한다.
        visible = returned if assembled.spans else sorted(assembled.images)
        image_pages = [{"page": page, **assembled.images[page]} for page in visible if page in assembled.images]
    header = {
        "path": path,
        "kind": assembled.kind,
        "page_count": assembled.page_count,
        "page_basis": assembled.page_basis,
        "pages_returned": returned,
        "chars_total": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        "image_pages": image_pages,
        "warnings": assembled.warnings,
        **assembled.extra,
    }
    if assembled.kind != "pdf":
        twin = _same_name_pdf(path)
        if twin:
            header["pdf"] = twin
    return Reading(header, chunk)
