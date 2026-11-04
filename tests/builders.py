"""시험 중에 손으로 합성 문서를 만든다. 기대값이 구현에 닿지 않게 바이트를 직접 쓴다."""
import io
import struct
import zipfile
import zlib
from pathlib import Path

PARAGRAPH_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"


# ---------- HWPX ----------
def hwpx_section(body: str, prefix: str = "hp") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><sec xmlns:{prefix}="{PARAGRAPH_NS}">{body}</sec>'


def write_hwpx(path: Path, sections, *, manifest: str | None = None, bindata=(), raw_section: bytes | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        if manifest is not None:
            zf.writestr("META-INF/manifest.xml", manifest)
        for name in bindata:
            zf.writestr(f"BinData/{name}", b"x")
        if raw_section is not None:
            zf.writestr("Contents/section0.xml", raw_section)
        for number, section in enumerate(sections):
            zf.writestr(f"Contents/section{number}.xml", section)
    return path


# ---------- CFB(HWP) ----------
_FREE = 0xFFFFFFFF
_END = 0xFFFFFFFE
_FAT = 0xFFFFFFFD
_SECTOR = 512


def write_cfb(path: Path, tree: dict) -> Path:
    """폴더는 dict, 스트림은 bytes인 tree로 CFB v3 파일을 쓴다.

    짧은 스트림도 일반 섹터에 넣으려고 4096바이트까지 0으로 채운다(미니 스트림을 쓰지 않는다).
    """
    entries = []  # (name, type, children or data)

    def add(name, node):
        index = len(entries)
        entries.append([name, node])
        return index

    root = add("Root Entry", tree)
    children = {}
    queue = [root]
    while queue:
        index = queue.pop(0)
        node = entries[index][1]
        if isinstance(node, dict):
            children[index] = []
            for name, child in node.items():
                child_index = add(name, child)
                children[index].append(child_index)
                queue.append(child_index)

    sectors = []
    chains = {}
    fat = []

    def place(data: bytes) -> int:
        count = (len(data) + _SECTOR - 1) // _SECTOR
        start = len(fat)
        for i in range(count):
            sectors.append(data[i * _SECTOR : (i + 1) * _SECTOR].ljust(_SECTOR, b"\0"))
            fat.append(start + i + 1 if i + 1 < count else _END)
        return start

    sizes = {}
    for index, (_, node) in enumerate(entries):
        if isinstance(node, bytes):
            data = node.ljust(4096, b"\0")
            chains[index] = place(data)
            sizes[index] = len(data)

    directory = bytearray()
    for index, (name, node) in enumerate(entries):
        encoded = name.encode("utf-16-le") + b"\0\0"
        kind = 5 if index == root else (1 if isinstance(node, dict) else 2)
        right = 0xFFFFFFFF
        child = 0xFFFFFFFF
        if isinstance(node, dict) and children[index]:
            child = children[index][0]
        # 형제는 오른쪽으로 잇는다. olefile은 색과 정렬을 따지지 않는다.
        for siblings in children.values():
            if index in siblings:
                position = siblings.index(index)
                if position + 1 < len(siblings):
                    right = siblings[position + 1]
        entry = encoded.ljust(64, b"\0")
        entry += struct.pack("<HBB", len(encoded), kind, 1)
        entry += struct.pack("<III", 0xFFFFFFFF, right, child)
        entry += b"\0" * 16 + b"\0" * 4 + b"\0" * 16
        entry += struct.pack("<I", chains.get(index, _END if isinstance(node, dict) else 0))
        entry += struct.pack("<II", sizes.get(index, 0), 0)
        directory += entry
    while len(directory) % _SECTOR:
        directory += b"\0" * 128 if len(directory) % _SECTOR else b""
    # 빈 자리 디렉터리 항목은 형식 0이어야 한다.
    directory_start = place(bytes(directory))

    # FAT 섹터 자신을 맨 끝에 둔다.
    per_sector = _SECTOR // 4
    fat_count = 1
    while (len(fat) + fat_count) > fat_count * per_sector:
        fat_count += 1
    fat_start = len(fat)
    fat.extend([_FAT] * fat_count)
    fat.extend([_FREE] * (fat_count * per_sector - len(fat)))
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    if fat_count > 109:
        raise ValueError("시험용 CFB는 DIFAT 확장을 쓰지 않는다")
    for i in range(fat_count):
        sectors.append(fat_bytes[i * _SECTOR : (i + 1) * _SECTOR])

    header = bytearray(_SECTOR)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 44, fat_count)
    struct.pack_into("<I", header, 48, directory_start)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, _END)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, _END)
    struct.pack_into("<I", header, 72, 0)
    difat = [fat_start + i for i in range(fat_count)] + [_FREE] * (109 - fat_count)
    struct.pack_into("<109I", header, 76, *difat)
    path.write_bytes(bytes(header) + b"".join(sectors))
    return path


def hwp_record(tag: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (size << 20)) + payload


def hwp_text(*items) -> bytes:
    """글과 제어 문자를 HWP 문단 글 바이트로 만든다. int는 2바이트 제어 코드, (코드,)는 16바이트 제어 코드다."""
    out = bytearray()
    for item in items:
        if isinstance(item, str):
            out += item.encode("utf-16-le")
        elif isinstance(item, int):
            out += struct.pack("<H", item)
        else:
            code = item[0]
            # 뒤따르는 14바이트는 글자처럼 보이는 쓰레기 값이다. 글로 새면 안 된다.
            out += struct.pack("<H", code) + "쓰레기값쓰레기값".encode("utf-16-le")[:12] + struct.pack("<H", code)
    return bytes(out)


def hwp_paragraph(*items) -> bytes:
    return hwp_record(67, hwp_text(*items) + struct.pack("<H", 13))


def hwp_equation(script: str) -> bytes:
    encoded = script.encode("utf-16-le")
    return hwp_record(88, struct.pack("<I", 0) + struct.pack("<H", len(script)) + encoded)


def hwp_header(*, compressed=True, encrypted=False, distribution=False) -> bytes:
    flags = (1 if compressed else 0) | (2 if encrypted else 0) | (4 if distribution else 0)
    return b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000000, flags) + b"\0" * 216


def deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def write_hwp(path: Path, sections, *, compressed=True, encrypted=False, distribution=False, bindata=()) -> Path:
    body = {}
    for number, records in enumerate(sections):
        data = b"".join(records)
        body[f"Section{number}"] = deflate(data) if compressed else data
    tree = {"FileHeader": hwp_header(compressed=compressed, encrypted=encrypted, distribution=distribution), "BodyText": body}
    if bindata:
        tree["BinData"] = {name: b"x" for name in bindata}
    return write_cfb(path, tree)


# ---------- OOXML ----------
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = f'xmlns:w="{W}" xmlns:a="{A}" xmlns:m="{M}" xmlns:mc="{MC}" xmlns:r="{R}" xmlns:p="{P}"'


def write_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def docx_members(body: str, **extra) -> dict:
    members = {"[Content_Types].xml": "<Types/>", "word/document.xml": f'<w:document {NS}><w:body>{body}</w:body></w:document>'}
    for name, data in extra.items():
        members[name.replace("__", "/").replace("_xml", ".xml")] = data
    return members


def rewrite_zip(path: Path, change) -> Path:
    """zip 안 항목을 change(이름, 바이트) -> 바이트로 바꿔 다시 쓴다."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info.filename, change(info.filename, source.read(info.filename)))
    path.write_bytes(buffer.getvalue())
    return path


# ---------- PDF ----------
def write_pdf(path: Path, pages) -> Path:
    """쪽마다 Helvetica로 ASCII 글을 쓴 작은 PDF. None인 쪽은 글자가 없다."""
    objects = []
    kids = []
    font_id = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # Pages 자리
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in pages:
        stream = b"" if text is None else b"BT /F1 12 Tf 50 700 Td (" + text.encode("ascii") + b") Tj ET"
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content_id = len(objects)
        page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>" % (content_id, font_id)
        objects.append(page)
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def append_zeros_member(path: Path, name: str, size: int) -> None:
    """0으로 채운 큰 항목을 zip에 더한다. 압축되어 파일은 작게 유지된다."""
    chunk = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        with zf.open(zipfile.ZipInfo(name), "w", force_zip64=True) as member:
            for _ in range(size // len(chunk)):
                member.write(chunk)


def write_pdf_stream(path: Path, stream: bytes) -> Path:
    """콘텐츠 스트림을 그대로 넣은 한 쪽짜리 PDF."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 3 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def write_pdf_with_image(path: Path, text: str = "text beside the image", image_size=(200, 200)) -> Path:
    """글과 그림 객체가 함께 있는 한 쪽 PDF."""
    width, height = image_size
    content = b"q %d 0 0 %d 50 400 cm /Im1 Do Q BT /F1 12 Tf 50 700 Td (%s) Tj ET" % (width, height, text.encode("ascii"))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 3 0 R >> /XObject << /Im1 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 4 >>\nstream\n\x00\xff\xff\x00\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def write_two_column_pdf(path: Path) -> Path:
    """좌우 단에 줄이 나뉜 한 쪽 PDF."""
    lines = []
    for row in range(12):
        y = 650 - row * 20
        lines.append(b"BT /F1 11 Tf 40 %d Td (left column line of plain text %d) Tj ET" % (y, row))
        lines.append(b"BT /F1 11 Tf 330 %d Td (right column line of plain text %d) Tj ET" % (y, row))
    return write_pdf_stream(path, b" ".join(lines))
