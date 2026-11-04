"""한컴 원본(hwp·hwpx)에서 글자, 수식, 그림 개수를 읽는다."""
import re
import sys
import zipfile
import zlib
from array import array
from typing import NamedTuple
from xml.parsers import expat

import olefile

ENTRY_LIMIT = 256 * 1024 * 1024
TOTAL_LIMIT = 512 * 1024 * 1024

_SECTION = re.compile(r"Contents/section(\d+)\.xml$")
_HWP3_HEAD = b"HWP Document File"

_INLINE_SPACE_ELEMENTS = {"tab", "lineBreak", "nbSpace", "fwSpace"}

_TAG_PARA_TEXT = 67
_TAG_EQUATION = 88
# 이 코드들만 2바이트이고, 나머지 32 미만 제어 코드는 뒤에 값을 달고 16바이트를 차지한다.
_NARROW_CONTROLS = frozenset({0, 10, 13, *range(24, 32)})
_SPACE_CONTROLS = frozenset({9, 10, 30, 31})


class DocumentError(Exception):
    """닫힌 사유(encrypted, distribution, corrupt, unsupported, too_large)로 못 읽는다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Text(NamedTuple):
    paragraphs: list
    images: int


def is_hwpx(names) -> bool:
    return any(_SECTION.match(name) for name in names)


class _HwpxParagraphs:
    """중첩 문단을 만나면 바깥 글을 먼저 내보내 두 문단이 합쳐지지 않게 한다."""

    def __init__(self):
        self.paragraphs = []
        self._stack = []
        self._text_depth = 0
        self._locals = {}

    def _local(self, name: str):
        # 접두어를 믿지 않고 이름공간 URI의 끝(`…/paragraph`)으로 판별한다. 연도가 바뀐 이름공간도 받는다.
        local = self._locals.get(name)
        if local is None:
            uri, _, tail = name.rpartition(" ")
            local = tail if uri.endswith("/paragraph") else ""
            self._locals[name] = local
        return local

    def _flush(self, buffer):
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            self.paragraphs.append(text)

    def start(self, name, _attrs):
        local = self._local(name)
        if local == "p":
            if self._stack:
                self._flush(self._stack[-1])
            self._stack.append([])
        elif local in ("t", "script"):
            self._text_depth += 1
        elif local in _INLINE_SPACE_ELEMENTS and self._text_depth and self._stack:
            self._stack[-1].append(" ")

    def end(self, name):
        local = self._local(name)
        if local == "p":
            if self._stack:
                self._flush(self._stack.pop())
        elif local in ("t", "script"):
            self._text_depth -= 1

    def data(self, text):
        if self._text_depth and self._stack:
            self._stack[-1].append(text.replace("\r", " ").replace("\n", " "))


def _refuse_doctype(*_args):
    raise DocumentError("corrupt")


def _parse_hwpx_section(stream, handler: _HwpxParagraphs) -> None:
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.StartElementHandler = handler.start
    parser.EndElementHandler = handler.end
    parser.CharacterDataHandler = handler.data
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.ParseFile(stream)


def hwpx_text(path) -> Text:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            sizes = [info.file_size for info in infos]
            if sizes and (max(sizes) > ENTRY_LIMIT or sum(sizes) > TOTAL_LIMIT):
                raise DocumentError("too_large")
            names = {info.filename for info in infos}
            # 배포용 문서는 본문이 암호화되어 XML로 읽히지 않는다. 손상과 가르려고 표지를 먼저 본다.
            if "META-INF/manifest.xml" in names and b"encryption-data" in archive.read("META-INF/manifest.xml"):
                raise DocumentError("encrypted")
            sections = sorted((int(m.group(1)), name) for name in names if (m := _SECTION.match(name)))
            handler = _HwpxParagraphs()
            for _, name in sections:
                with archive.open(name) as stream:
                    _parse_hwpx_section(stream, handler)
            images = sum(1 for name in names if name.startswith("BinData/") and not name.endswith("/"))
    except (zipfile.BadZipFile, expat.ExpatError, RuntimeError, zlib.error, EOFError) as error:
        raise DocumentError("corrupt") from error
    return Text(handler.paragraphs, images)


def _paragraph_text(payload: bytes) -> str:
    units = array("H")
    units.frombytes(payload[: len(payload) & ~1])
    if sys.byteorder == "big":
        units.byteswap()
    pieces = []
    start = 0
    index = 0
    count = len(units)
    while index < count:
        code = units[index]
        if code >= 32:
            index += 1
            continue
        if index > start:
            pieces.append(payload[start * 2 : index * 2].decode("utf-16-le", "replace"))
        if code in _SPACE_CONTROLS:
            pieces.append(" ")
        index += 1 if code in _NARROW_CONTROLS else 8
        start = index
    if start < count:
        pieces.append(payload[start * 2 : count * 2].decode("utf-16-le", "replace"))
    return "".join(pieces)


def _equation_text(payload: bytes) -> str:
    if len(payload) < 6:
        return ""
    length = int.from_bytes(payload[4:6], "little")
    return payload[6 : 6 + length * 2].decode("utf-16-le", "replace")


def _records(data: bytes):
    position = 0
    size_of_data = len(data)
    while position + 4 <= size_of_data:
        head = int.from_bytes(data[position : position + 4], "little")
        position += 4
        tag = head & 0x3FF
        size = head >> 20
        if size == 0xFFF:
            if position + 4 > size_of_data:
                raise DocumentError("corrupt")
            size = int.from_bytes(data[position : position + 4], "little")
            position += 4
        if position + size > size_of_data:
            raise DocumentError("corrupt")
        yield tag, data[position : position + size]
        position += size


def _inflate(raw: bytes, budget: int) -> bytes:
    inflater = zlib.decompressobj(-15)
    data = inflater.decompress(raw, budget + 1)
    if len(data) > budget:
        raise DocumentError("too_large")
    return data


def hwp_text(path) -> Text:
    """CFB 파일을 읽는다. HWP가 아닌 CFB는 사유를 말한다."""
    try:
        ole = olefile.OleFileIO(str(path))
    except olefile.OleFileError as error:
        raise DocumentError("corrupt") from error
    try:
        entries = ole.listdir()
        if ["FileHeader"] not in entries:
            raise DocumentError("encrypted" if ["EncryptedPackage"] in entries else "unsupported")
        header = ole.openstream("FileHeader").read()
        if len(header) < 40:
            raise DocumentError("corrupt")
        flags = int.from_bytes(header[36:40], "little")
        if flags & 2:
            raise DocumentError("encrypted")
        if flags & 4:
            raise DocumentError("distribution")
        compressed = bool(flags & 1)
        sections = sorted(
            (int(entry[1][7:]), entry)
            for entry in entries
            if len(entry) == 2 and entry[0] == "BodyText" and re.fullmatch(r"Section\d+", entry[1])
        )
        if not sections:
            raise DocumentError("corrupt")
        paragraphs = []
        spent = 0
        for _, entry in sections:
            raw = ole.openstream(entry).read()
            data = _inflate(raw, ENTRY_LIMIT) if compressed else raw
            spent += len(data)
            if spent > TOTAL_LIMIT:
                raise DocumentError("too_large")
            for tag, payload in _records(data):
                if tag == _TAG_PARA_TEXT:
                    text = _paragraph_text(payload)
                elif tag == _TAG_EQUATION:
                    text = _equation_text(payload)
                else:
                    continue
                if text.strip():
                    paragraphs.append(text)
        images = sum(1 for entry in entries if entry[0] == "BinData" and len(entry) > 1)
        return Text(paragraphs, images)
    except (zlib.error, EOFError, ValueError, IndexError) as error:
        raise DocumentError("corrupt") from error
    finally:
        ole.close()


def cfb_kind(path) -> str | None:
    """CFB 파일이 HWP(`hwp`)인지 구형 엑셀(`xls`)인지 본다. 그 밖과 손상은 None이다."""
    try:
        ole = olefile.OleFileIO(str(path))
    except olefile.OleFileError:
        return None
    try:
        entries = ole.listdir()
    finally:
        ole.close()
    if ["FileHeader"] in entries:
        return "hwp"
    if ["Workbook"] in entries or ["Book"] in entries:
        return "xls"
    return None


def is_hwp3(head: bytes) -> bool:
    """HWP 3.x 바이너리는 CFB가 아니고 머리가 글자로 시작한다."""
    return head.startswith(_HWP3_HEAD)
