"""docx·pptx·xlsx 원본에서 글자, 슬라이드 순서, 셀 표시값, 내장 그림을 읽는다."""
import datetime as dt
import itertools
import posixpath
import re
import warnings
import zipfile
import zlib
from dataclasses import dataclass
from typing import NamedTuple
from xml.parsers import expat

TOTAL_LIMIT = 512 * 1024 * 1024

_WORD = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
_DRAWING = (
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
)
_MATH = (
    "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "http://purl.oclc.org/ooxml/officeDocument/math",
)
_COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_RELATIONSHIP_ID = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships id",
    "http://purl.oclc.org/ooxml/officeDocument/relationships id",
)

_TEXT = frozenset(f"{uri} t" for uri in (*_WORD, *_DRAWING, *_MATH))
_PARAGRAPH = frozenset(f"{uri} p" for uri in (*_WORD, *_DRAWING))
_BREAK = frozenset({*(f"{uri} tab" for uri in _WORD), *(f"{uri} br" for uri in _WORD), *(f"{uri} br" for uri in _DRAWING)})
_TAB_STOPS = frozenset(f"{uri} tabs" for uri in _WORD)
_FALLBACK = f"{_COMPATIBILITY} Fallback"

_NUMBERED_PART = re.compile(r"(\d+)\.xml$")


class DocumentError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Text(NamedTuple):
    paragraphs: list
    images: int


class Slides(NamedTuple):
    texts: list  # 표시 순서. 슬라이드마다 문단을 줄바꿈으로 이은 글
    images: int


@dataclass
class Cell:
    address: str
    display: str | None
    raw: str | None
    formula: str | None

    @property
    def line(self) -> str:
        """표시값, 다르면 `[값 …]`, 수식이면 `[=…]`을 한 줄로 만든다."""
        pieces = []
        if self.display is not None:
            pieces.append(self.display)
            if self.raw is not None and self.raw != self.display:
                pieces.append(f"[값 {self.raw}]")
        if self.formula is not None:
            pieces.append(f"[{self.formula}]")
        return " ".join(pieces)


@dataclass
class Sheet:
    number: int
    name: str
    cells: list
    dimension: str | None


class Workbook(NamedTuple):
    sheets: list
    images: int
    stale_formula_cache: bool


class _Paragraphs:
    """w:p와 a:p를 문단으로 삼는다. 글상자의 중첩 문단은 바깥 글과 합치지 않는다."""

    def __init__(self):
        self.paragraphs = []
        self._stack = []
        self._text_depth = 0
        self._fallback_depth = 0
        self._tab_stops_depth = 0

    def _flush(self, buffer):
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            self.paragraphs.append(text)

    def start(self, name, _attrs):
        if name == _FALLBACK:
            self._fallback_depth += 1
        if self._fallback_depth:
            return
        if name in _PARAGRAPH:
            if self._stack:
                self._flush(self._stack[-1])
            self._stack.append([])
        elif name in _TEXT:
            self._text_depth += 1
        elif name in _TAB_STOPS:
            self._tab_stops_depth += 1
        elif name in _BREAK and self._stack and not self._tab_stops_depth:
            self._stack[-1].append(" ")

    def end(self, name):
        if name == _FALLBACK:
            self._fallback_depth -= 1
            return
        if self._fallback_depth:
            return
        if name in _PARAGRAPH:
            if self._stack:
                self._flush(self._stack.pop())
        elif name in _TEXT:
            self._text_depth -= 1
        elif name in _TAB_STOPS:
            self._tab_stops_depth -= 1

    def data(self, text):
        if self._text_depth and self._stack and not self._fallback_depth:
            self._stack[-1].append(text.replace("\r", " ").replace("\n", " "))


def _refuse_doctype(*_args):
    raise DocumentError("corrupt")


def _parser(start=None, end=None, data=None):
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = _refuse_doctype
    if start:
        parser.StartElementHandler = start
    if end:
        parser.EndElementHandler = end
    if data:
        parser.CharacterDataHandler = data
    return parser


def _part_paragraphs(archive, name) -> list:
    handler = _Paragraphs()
    with archive.open(name) as stream:
        _parser(handler.start, handler.end, handler.data).ParseFile(stream)
    return handler.paragraphs


def _open_archive(path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    if sum(info.file_size for info in infos) > TOTAL_LIMIT:
        archive.close()
        raise DocumentError("too_large")
    if any(info.flag_bits & 1 for info in infos):
        archive.close()
        raise DocumentError("encrypted")
    return archive


def _numbered(names, pattern: str) -> list:
    compiled = re.compile(pattern)

    def key(name):
        match = _NUMBERED_PART.search(name)
        return (int(match.group(1)) if match else 0, name)

    return sorted((name for name in names if compiled.fullmatch(name)), key=key)


def _count_media(names, prefix: str) -> int:
    return sum(1 for name in names if name.startswith(prefix) and not name.endswith("/"))


_FORMAT_ERRORS = (zipfile.BadZipFile, expat.ExpatError, KeyError, RuntimeError, zlib.error, EOFError)


def docx_text(path) -> Text:
    try:
        with _open_archive(path) as archive:
            names = archive.namelist()
            parts = ["word/document.xml"]
            for pattern in (
                r"word/footnotes\d*\.xml",
                r"word/endnotes\d*\.xml",
                r"word/header\d*\.xml",
                r"word/footer\d*\.xml",
                # 다이어그램의 같은 글이 drawing 파트에도 있어 data만 읽는다.
                r"word/diagrams/data\d*\.xml",
                r"word/charts/chart\d*\.xml",
            ):
                parts.extend(_numbered(names, pattern))
            paragraphs = []
            for part in parts:
                paragraphs.extend(_part_paragraphs(archive, part))
            return Text(paragraphs, _count_media(names, "word/media/"))
    except _FORMAT_ERRORS as error:
        raise DocumentError("corrupt") from error


def _relationships(archive, part: str) -> dict:
    """파트의 관계를 {Id: (형식 끝말, 대상 파트 경로)}로 읽는다. 외부 대상은 뺀다."""
    folder, _, name = part.rpartition("/")
    rels = f"{folder}/_rels/{name}.rels"
    found = {}
    if rels not in archive.NameToInfo:
        return found

    def start(element, attrs):
        if element.rpartition(" ")[2] != "Relationship" or attrs.get("TargetMode") == "External":
            return
        target = attrs.get("Target", "")
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        found[attrs.get("Id")] = (attrs.get("Type", "").rpartition("/")[2], resolved)

    with archive.open(rels) as stream:
        _parser(start).ParseFile(stream)
    return found


def _slide_order(archive) -> list:
    relationships = _relationships(archive, "ppt/presentation.xml")
    ids = []

    def start(element, attrs):
        if element.rpartition(" ")[2] == "sldId":
            ids.append(next((attrs[key] for key in _RELATIONSHIP_ID if key in attrs), None))

    with archive.open("ppt/presentation.xml") as stream:
        _parser(start).ParseFile(stream)
    # 슬라이드 ID가 가리키는 파트를 못 찾으면 그 슬라이드를 조용히 버리지 않고 손상으로 보고한다.
    if any(rid not in relationships for rid in ids):
        raise DocumentError("corrupt")
    return [relationships[rid][1] for rid in ids]


_SLIDE_TEXT_RELATIONS = {"notesSlide", "diagramData", "chart"}


def pptx_slides(path) -> Slides:
    try:
        with _open_archive(path) as archive:
            texts = []
            for slide in _slide_order(archive):
                paragraphs = _part_paragraphs(archive, slide)
                for kind, target in _relationships(archive, slide).values():
                    if kind in _SLIDE_TEXT_RELATIONS and target in archive.NameToInfo:
                        paragraphs.extend(_part_paragraphs(archive, target))
                texts.append("\n".join(paragraphs))
            return Slides(texts, _count_media(archive.namelist(), "ppt/media/"))
    except _FORMAT_ERRORS as error:
        raise DocumentError("corrupt") from error


_PERCENT = re.compile(r"%\s*(?:_.)?\s*$")
_DECIMALS = re.compile(r"\.(0+)")


def _plain_number(value) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return str(value)


def _raw_text(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, (dt.date, dt.time, dt.timedelta)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if isinstance(value, (int, float)):
        return _plain_number(value)
    return str(value).replace("\r", " ").replace("\n", " ")


def _display_text(value, number_format: str) -> str:
    """파일이 보여 주는 값을 되살린다. 저장하면 형식 ID가 사용자 정의 번호로 바뀌므로 형식 문자열로 판단한다."""
    # 성진: 날짜·백분율·천 단위만 되살리고 통화 기호나 조건부 형식은 원시값으로 둔다. 표시값 검색이 빠지는 실제 사례가 나오면 늘린다.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _raw_text(value)
    section = (number_format or "").split(";")[0]
    decimals = _DECIMALS.search(section)
    places = len(decimals.group(1)) if decimals else 0
    if _PERCENT.search(section):
        return f"{value * 100:.{places}f}%"
    if "#,##0" in section:
        return f"{value:,.{places}f}"
    return _plain_number(value)


def _sheet_cells(values, formulas) -> list:
    cells = []
    for value_row, formula_row in itertools.zip_longest(values.iter_rows(), formulas.iter_rows(), fillvalue=()):
        by_column = {cell.column: cell for cell in value_row if cell.value is not None}
        for cell in formula_row:
            if cell.value is None:
                continue
            formula = None
            if cell.data_type == "f":
                formula = getattr(cell.value, "text", cell.value)
                formula = str(formula)
                if not formula.startswith("="):
                    formula = "=" + formula
            cached = by_column.get(cell.column)
            if cached is None:
                cells.append(Cell(cell.coordinate, None, None, formula))
                continue
            cells.append(Cell(cell.coordinate, _display_text(cached.value, cached.number_format), _raw_text(cached.value), formula))
    return cells


def _dimension(cells) -> str | None:
    from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter

    if not cells:
        return None
    columns, rows = [], []
    for cell in cells:
        column, row = coordinate_from_string(cell.address)
        columns.append(column_index_from_string(column))
        rows.append(row)
    return f"{get_column_letter(min(columns))}{min(rows)}:{get_column_letter(max(columns))}{max(rows)}"


def xlsx_workbook(path) -> Workbook:
    """표시값과 수식을 함께 읽는다. 수식 칸의 값은 파일에 저장된 캐시이고 계산하지 않는다."""
    import openpyxl

    with _open_archive(path) as archive:
        images = _count_media(archive.namelist(), "xl/media/")
    # 확장자가 아니라 내용으로 판별했으므로 파일 객체로 넘긴다(openpyxl은 이름의 확장자를 검사한다).
    with open(path, "rb") as value_file, open(path, "rb") as formula_file:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                values = openpyxl.load_workbook(value_file, read_only=True, data_only=True)
                formulas = openpyxl.load_workbook(formula_file, read_only=True, data_only=False)
                try:
                    sheets = []
                    for number, (value_sheet, formula_sheet) in enumerate(zip(values.worksheets, formulas.worksheets), 1):
                        # 파일이 적어 둔 범위가 틀리면 칸이 잘린다. 범위를 믿지 않고 실제 칸을 읽는다.
                        value_sheet.reset_dimensions()
                        formula_sheet.reset_dimensions()
                        cells = _sheet_cells(value_sheet, formula_sheet)
                        sheets.append(Sheet(number, value_sheet.title, cells, _dimension(cells)))
                    stale = bool(getattr(getattr(formulas, "calculation", None), "fullCalcOnLoad", False))
                finally:
                    values.close()
                    formulas.close()
        except Exception as error:  # openpyxl은 깨진 파일에 형식마다 다른 예외를 낸다(OSError 포함)
            raise DocumentError("corrupt") from error
    return Workbook(sheets, images, stale)
