"""extract 유닛의 약속: 내용으로 형식을 가르고, 부분별 글을 주고, 예외를 밖으로 내지 않는다.

기대값의 출처: 손으로 쓴 합성 파일의 입력 문자열. 실물은 rhwp와 pdftotext로 본 값(시험 옆 주석).
"""
import datetime as dt
from pathlib import Path
from xml.etree import ElementTree

import pytest

from documents import extract
from tests import builders as b

FIXTURES = Path(__file__).parent / "fixtures"


def body(result):
    return "\n".join(text for _, text in result.parts)


def paragraph(text):
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


# ---------- HWPX ----------
def test_hwpx_reads_paragraphs_in_section_order(tmp_path):
    path = b.write_hwpx(tmp_path / "a.hwpx", [b.hwpx_section(paragraph("첫째")), b.hwpx_section(paragraph("둘째"))])
    result = extract.extract(path)
    assert result.error is None and result.kind == "hwpx" and result.line_wrapped is False
    assert result.parts == [(None, "첫째\n둘째")]


def test_hwpx_prefix_does_not_matter(tmp_path):
    xml = b.hwpx_section("<x:p><x:run><x:t>접두어가 달라도 같은 글</x:t></x:run></x:p>", prefix="x")
    assert body(extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [xml]))) == "접두어가 달라도 같은 글"


def test_hwpx_nested_paragraph_is_not_merged_with_outer(tmp_path):
    # 표가 든 문단. 문자열로 자르면 `의무화 노사정`이 한 문단으로 합쳐져 가짜 일치가 난다.
    xml = paragraph("의무화") .replace("</hp:run></hp:p>", "<hp:tbl><hp:tc><hp:subList><hp:p><hp:run><hp:t>노사정 합의안</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tbl></hp:run></hp:p>")
    result = extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [b.hwpx_section(xml)]))
    assert body(result).split("\n") == ["의무화", "노사정 합의안"]


def test_hwpx_text_continues_after_inline_children(tmp_path):
    # 실물 `…제2차 마약류대책협의회…`의 모양: t 안에 lineBreak가 끼어 있다. 그 뒤의 글을 잃으면 안 된다.
    xml = b.hwpx_section('<hp:p><hp:run><hp:t>18개 의원 의료쇼핑<hp:lineBreak/>**일반적으로<hp:tab/>성인<hp:nbSpace/>기준<hp:fwSpace/>1시간<hp:markpenEnd/>수면</hp:t></hp:run></hp:p>')
    assert body(extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [xml]))) == "18개 의원 의료쇼핑 **일반적으로 성인 기준 1시간수면"


def test_hwpx_reads_equation_script(tmp_path):
    xml = b.hwpx_section("<hp:p><hp:run><hp:equation><hp:script>보증금액 = a over b</hp:script></hp:equation></hp:run></hp:p>")
    assert body(extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [xml]))) == "보증금액 = a over b"


def test_hwpx_ignores_text_outside_t_and_script(tmp_path):
    xml = b.hwpx_section("<hp:p><hp:run><hp:pic><hp:shapeComment>그림입니다 원본 이름</hp:shapeComment></hp:pic><hp:t>본문</hp:t></hp:run></hp:p>")
    assert body(extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [xml]))) == "본문"


def test_hwpx_newline_inside_t_does_not_split_a_paragraph(tmp_path):
    xml = b.hwpx_section("<hp:p><hp:run><hp:t>의료\n쇼핑</hp:t></hp:run></hp:p>")
    assert body(extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [xml]))) == "의료 쇼핑"


def test_hwpx_counts_embedded_images(tmp_path):
    path = b.write_hwpx(tmp_path / "a.hwpx", [b.hwpx_section(paragraph("x"))], bindata=["image1.png", "image2.bmp"])
    assert extract.extract(path).embedded_images == 2


def test_hwpx_encryption_marker_is_encrypted(tmp_path):
    manifest = '<manifest xmlns:odf="urn:x"><odf:file-entry><odf:encryption-data/></odf:file-entry></manifest>'
    path = b.write_hwpx(tmp_path / "a.hwpx", [], manifest=manifest, raw_section=b"\x00\x01binary")
    assert extract.extract(path).error == "encrypted"


def test_hwpx_broken_xml_without_marker_is_corrupt(tmp_path):
    path = b.write_hwpx(tmp_path / "a.hwpx", [], manifest="<manifest/>", raw_section=b"<hp:p><unclosed")
    assert extract.extract(path).error == "corrupt"


def test_hwpx_entity_declarations_are_refused(tmp_path):
    bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa">]><sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:p><hp:t>&a;</hp:t></hp:p></sec>'
    assert extract.extract(b.write_hwpx(tmp_path / "a.hwpx", [], raw_section=bomb)).error == "corrupt"


def test_hwpx_entry_over_the_limit_is_too_large(tmp_path):
    xml = b.hwpx_section(paragraph("x" * 10))
    path = b.write_hwpx(tmp_path / "a.hwpx", [xml])
    b.append_zeros_member(path, "Contents/section1.xml", 257 * 1024 * 1024)
    assert extract.extract(path).error == "too_large"


def test_hwpx_total_over_the_limit_is_too_large(tmp_path):
    path = b.write_hwpx(tmp_path / "a.hwpx", [b.hwpx_section(paragraph("x"))])
    for number in range(1, 4):
        b.append_zeros_member(path, f"BinData/big{number}.bin", 200 * 1024 * 1024)
    assert extract.extract(path).error == "too_large"


# ---------- HWP ----------
def test_hwp_reads_paragraphs_and_skips_control_payloads(tmp_path):
    records = [
        b.hwp_paragraph("앞", (9,), "뒤"),  # 탭: 16바이트 제어이고 공백이 된다
        b.hwp_paragraph("표 앞", (11,), "표 뒤"),  # 표 컨트롤: 16바이트이고 글이 없다
        b.hwp_paragraph("줄", 10, "바꿈"),  # 2바이트 제어이고 공백이 된다
        b.hwp_paragraph("묶음", 30, "빈칸", 31, "고정", 24, "하이픈"),
    ]
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [records]))
    assert result.error is None and result.kind == "hwp" and result.line_wrapped is False
    assert body(result).split("\n") == ["앞 뒤", "표 앞표 뒤", "줄 바꿈", "묶음 빈칸 고정하이픈"]


def test_hwp_rejoins_surrogate_pairs(tmp_path):
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_paragraph("가𠀋나")]]))
    assert body(result) == "가𠀋나"


def test_hwp_reads_long_records_and_all_sections(tmp_path):
    long_text = "가" * 3000  # 6000바이트: 크기 칸이 0xfff를 넘어 확장 크기를 쓴다
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_paragraph(long_text)], [b.hwp_paragraph("둘째 구역")]]))
    assert body(result).split("\n") == [long_text, "둘째 구역"]


def test_hwp_reads_equation_record(tmp_path):
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_equation("보증금액 = x over y")]]))
    assert body(result) == "보증금액 = x over y"


def test_hwp_uncompressed_body(tmp_path):
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_paragraph("압축 없음")]], compressed=False))
    assert body(result) == "압축 없음"


def test_hwp_counts_embedded_images(tmp_path):
    result = extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_paragraph("x")]], bindata=["BIN0001.png", "BIN0002.bmp"]))
    assert result.embedded_images == 2


@pytest.mark.parametrize(
    "flags, reason",
    [({"encrypted": True}, "encrypted"), ({"distribution": True}, "distribution")],
)
def test_hwp_flags(tmp_path, flags, reason):
    assert extract.extract(b.write_hwp(tmp_path / "a.hwp", [[b.hwp_paragraph("x")]], **flags)).error == reason


def test_hwp_decompression_bomb_is_too_large(tmp_path):
    bomb = b.deflate(b"\0" * (257 * 1024 * 1024))
    tree = {"FileHeader": b.hwp_header(), "BodyText": {"Section0": bomb}}
    assert extract.extract(b.write_cfb(tmp_path / "a.hwp", tree)).error == "too_large"


def test_hwp_garbage_section_is_corrupt(tmp_path):
    tree = {"FileHeader": b.hwp_header(), "BodyText": {"Section0": b"\xff" * 5000}}
    assert extract.extract(b.write_cfb(tmp_path / "a.hwp", tree)).error == "corrupt"


def test_cfb_without_header_with_encrypted_package_is_encrypted(tmp_path):
    # 암호 걸린 Office 파일의 실제 모양이다.
    path = b.write_cfb(tmp_path / "a.docx", {"EncryptionInfo": b"x", "EncryptedPackage": b"y"})
    assert extract.extract(path).error == "encrypted"


def test_other_cfb_is_unsupported(tmp_path):
    assert extract.extract(b.write_cfb(tmp_path / "a.doc", {"WordDocument": b"x"})).error == "unsupported"


def test_hwp3_header_is_unsupported(tmp_path):
    path = tmp_path / "old.hwp"
    path.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 100)
    assert extract.extract(path).error == "unsupported"


def test_real_hwp_equation_matches_rhwp():
    # 기대값 출처: `rhwp export-text`가 낸 수식 `보증금액= {하도급계약금액-계약상선급금} over {공사기간(개월수)} TIMES  4`
    text = body(extract.extract(FIXTURES / "equation_real.hwp"))
    assert "보증금액= {하도급계약금액-계약상선급금} over {공사기간(개월수)} TIMES  4" in text
    assert "유강종합건설㈜가" in text  # 접기는 extract의 일이 아니다


# ---------- PDF ----------
def test_pdf_pages_are_units_and_marks_image_only_pages(tmp_path):
    long_text = "Hello searchable pdf text body"
    result = extract.extract(b.write_pdf(tmp_path / "a.pdf", [long_text, None, "short", long_text + " again"]))
    assert result.kind == "pdf" and result.line_wrapped is True and result.error is None
    assert [unit for unit, _ in result.parts] == [1, 2, 3, 4]
    assert result.parts[0][1].strip() == long_text
    assert result.image_only_pages == [2, 3]  # 공백 뺀 글자 20 미만


def test_real_pdf_pages_match_pdftotext():
    # 기대값 출처: `pdftotext -f N -l N two_pages_real.pdf -`
    result = extract.extract(FIXTURES / "two_pages_real.pdf")
    assert [unit for unit, _ in result.parts] == [1, 2]
    assert "소비자생활협동조합법 소관," in result.parts[0][1]
    assert "\r" not in result.parts[0][1]
    assert "실질적인 기업지원 혜택을 받을 수 있게" in result.parts[1][1]
    assert "실질적인 기업지원" not in result.parts[0][1]


def test_encrypted_pdf(tmp_path):
    assert extract.extract(FIXTURES / "encrypted.pdf").error == "encrypted"


def test_garbage_pdf_is_corrupt(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4\ngarbage only")
    assert extract.extract(path).error == "corrupt"


# ---------- 판별 ----------
def test_content_decides_not_the_extension(tmp_path):
    hwpx_named_hwp = b.write_hwpx(tmp_path / "a.hwp", [b.hwpx_section(paragraph("내용은 HWPX"))])
    assert extract.extract(hwpx_named_hwp).kind == "hwpx"
    hwp_named_hwpx = b.write_hwp(tmp_path / "b.hwpx", [[b.hwp_paragraph("내용은 HWP")]])
    assert extract.extract(hwp_named_hwpx).kind == "hwp"
    pdf_named_doc = b.write_pdf(tmp_path / "c.hw", ["content is pdf and long enough"])
    assert extract.extract(pdf_named_doc).kind == "pdf"


def test_empty_file_is_not_a_document(tmp_path):
    path = tmp_path / "a.hwp"
    path.write_bytes(b"")
    assert extract.extract(path).error == "not_a_document"


def test_html_named_hwpx_is_not_a_document(tmp_path):
    path = tmp_path / "a.hwpx"
    path.write_text("<html></html>")
    assert extract.extract(path).error == "not_a_document"


def test_plain_zip_is_unsupported(tmp_path):
    path = b.write_zip(tmp_path / "a.pdf", {"something.txt": "x"})
    assert extract.extract(path).error == "unsupported"


def test_truncated_zip_is_corrupt(tmp_path):
    path = b.write_hwpx(tmp_path / "a.hwpx", [b.hwpx_section(paragraph("가" * 500))])
    path.write_bytes(path.read_bytes()[:60])
    assert extract.extract(path).error == "corrupt"


def test_fake_zip_magic_is_corrupt(tmp_path):
    path = tmp_path / "a.docx"
    path.write_bytes(b"PK\x03\x04 this is not a zip")
    assert extract.extract(path).error == "corrupt"


def test_missing_file_is_io(tmp_path):
    assert extract.extract(tmp_path / "없다.pdf").error == "io"


# ---------- DOCX ----------
def test_docx_reads_body_table_math_and_textbox_once(tmp_path):
    body_xml = (
        "<w:p><w:r><w:t>본문</w:t><w:tab/><w:t>이어짐</w:t><w:br/><w:t>끝</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 칸</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><m:oMath><m:r><m:t>수식말</m:t></m:r></m:oMath></w:p>"
        "<w:p><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:txbxContent><w:p><w:r><w:t>글상자</w:t></w:r></w:p></w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:pict><w:txbxContent><w:p><w:r><w:t>글상자</w:t></w:r></w:p></w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
        "<w:p><w:del><w:r><w:delText>지운 글</w:delText></w:r></w:del></w:p>"
    )
    path = b.write_zip(tmp_path / "a.docx", b.docx_members(body_xml))
    result = extract.extract(path)
    assert result.kind == "docx" and result.line_wrapped is False
    lines = body(result).split("\n")
    assert "본문 이어짐 끝" in lines and "표 칸" in lines and "수식말" in lines
    assert body(result).count("글상자") == 1
    assert "지운 글" not in body(result)


def test_docx_reads_notes_headers_diagrams_and_charts(tmp_path):
    def wrap(tag, text):
        return f'<{tag} {b.NS}><w:p><w:r><w:t>{text}</w:t></w:r></w:p></{tag}>'

    drawing = f'<d {b.NS}><a:p><a:r><a:t>%s</a:t></a:r></a:p></d>'
    members = b.docx_members("<w:p><w:r><w:t>본문</w:t></w:r></w:p>")
    members.update(
        {
            "word/footnotes.xml": wrap("w:footnotes", "각주말"),
            "word/endnotes.xml": wrap("w:endnotes", "미주말"),
            "word/header1.xml": wrap("w:hdr", "머리말말"),
            "word/footer2.xml": wrap("w:ftr", "꼬리말말"),
            "word/diagrams/data1.xml": drawing % "다이어그램말",
            "word/diagrams/drawing1.xml": drawing % "다이어그램말",  # 같은 글의 그림본: 두 번 세지 않는다
            "word/charts/chart1.xml": drawing % "차트말",
            "word/media/image1.png": b"x",
        }
    )
    result = extract.extract(b.write_zip(tmp_path / "a.docx", members))
    text = body(result)
    for word in ["각주말", "미주말", "머리말말", "꼬리말말", "차트말"]:
        assert word in text
    assert text.count("다이어그램말") == 1
    assert result.embedded_images == 1


def test_real_docx_shape_from_python_docx(tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("python-docx 문단")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "python-docx 표"
    path = tmp_path / "a.docx"
    document.save(path)
    text = body(extract.extract(path))
    assert "python-docx 문단" in text and "python-docx 표" in text


# ---------- PPTX ----------
def make_pptx(path, titles, order=None, notes=None, table_slide=None):
    import pptx
    from pptx.util import Inches

    presentation = pptx.Presentation()
    for index, title in enumerate(titles):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = title
        if notes and index in notes:
            slide.notes_slide.notes_text_frame.text = notes[index]
        if table_slide == index:
            table = slide.shapes.add_table(1, 1, Inches(1), Inches(2), Inches(2), Inches(1)).table
            table.cell(0, 0).text = "표 칸 말"
    if order:
        ids = presentation.slides._sldIdLst
        items = list(ids)
        for element in items:
            ids.remove(element)
        for position in order:
            ids.append(items[position])
    presentation.save(path)
    return path


def test_pptx_units_follow_presentation_order(tmp_path):
    path = make_pptx(tmp_path / "a.pptx", ["첫 파일", "둘째 파일", "셋째 파일"], order=[2, 0, 1])
    result = extract.extract(path)
    assert result.kind == "pptx" and result.line_wrapped is False
    assert [(unit, text.strip()) for unit, text in result.parts] == [(1, "셋째 파일"), (2, "첫 파일"), (3, "둘째 파일")]


def test_pptx_reads_notes_and_table_of_that_slide(tmp_path):
    path = make_pptx(tmp_path / "a.pptx", ["하나", "둘"], notes={1: "둘째 노트"}, table_slide=0)
    parts = dict(extract.extract(path).parts)
    assert "표 칸 말" in parts[1] and "둘째 노트" not in parts[1]
    assert "둘째 노트" in parts[2]


# ---------- XLSX ----------
def make_xlsx(path, *, full_calc=False):
    import openpyxl

    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "요약"
    first["A1"] = "머리"
    second = workbook.create_sheet("Sheet1")
    second["B3"] = "쿠팡 변동"
    second["D3"] = dt.datetime(2026, 5, 1)
    second["D3"].number_format = "yyyy-mm-dd"
    second["E3"] = 0.125
    second["E3"].number_format = "0.0%"
    second["F3"] = 1234567
    second["F3"].number_format = "#,##0"
    second["G3"] = 0.5
    second["G3"].number_format = "0%"
    second["H3"] = "=SUM(1,2)"
    second["J9"] = "먼 칸"
    second["K9"] = 2.5
    workbook.calculation.fullCalcOnLoad = full_calc
    workbook.save(path)
    return path


def cells(result):
    return dict(result.parts)


def test_xlsx_cell_addresses_survive_sparse_rows(tmp_path):
    result = extract.extract(make_xlsx(tmp_path / "a.xlsx"))
    assert result.kind == "xlsx" and result.line_wrapped is False
    found = cells(result)
    assert found["1:요약!A1"] == "머리"
    assert found["2:Sheet1!B3"] == "쿠팡 변동"
    assert found["2:Sheet1!J9"] == "먼 칸"


def test_xlsx_display_values_follow_the_format_string(tmp_path):
    found = cells(extract.extract(make_xlsx(tmp_path / "a.xlsx")))
    assert found["2:Sheet1!D3"].startswith("2026-05-01")
    # 표시값과 원시값을 모두 찾을 수 있어야 한다.
    assert found["2:Sheet1!E3"] == "12.5% [값 0.125]"
    assert found["2:Sheet1!F3"] == "1,234,567 [값 1234567]"
    assert found["2:Sheet1!G3"] == "50% [값 0.5]"
    assert found["2:Sheet1!K9"] == "2.5"


def test_xlsx_formula_without_cache_has_only_the_formula(tmp_path):
    found = cells(extract.extract(make_xlsx(tmp_path / "a.xlsx")))
    assert found["2:Sheet1!H3"] == "[=SUM(1,2)]"


def test_xlsx_formula_with_cache_keeps_both(tmp_path):
    path = make_xlsx(tmp_path / "a.xlsx")

    def add_cache(name, data):
        if name == "xl/worksheets/sheet2.xml":
            return data.replace(b"<f>SUM(1,2)</f><v></v>", b"<f>SUM(1,2)</f><v>3</v>").replace(b"<f>SUM(1,2)</f><v />", b"<f>SUM(1,2)</f><v>3</v>")
        return data

    b.rewrite_zip(path, add_cache)
    found = cells(extract.extract(path))
    assert found["2:Sheet1!H3"] == "3 [=SUM(1,2)]"


def test_macro_extension_is_read_by_content(tmp_path):
    path = make_xlsx(tmp_path / "a.xlsx")
    renamed = tmp_path / "b.hwpx"
    renamed.write_bytes(path.read_bytes())
    assert extract.extract(renamed).kind == "xlsx"


def test_xlsx_broken_workbook_is_corrupt(tmp_path):
    path = b.write_zip(tmp_path / "a.xlsx", {"xl/workbook.xml": "<workbook", "[Content_Types].xml": "<Types/>"})
    assert extract.extract(path).error == "corrupt"


def test_zip_over_total_limit_is_too_large_for_ooxml(tmp_path):
    path = b.write_zip(tmp_path / "a.docx", b.docx_members("<w:p/>"))
    for number in range(3):
        b.append_zeros_member(path, f"word/media/big{number}.bin", 200 * 1024 * 1024)
    assert extract.extract(path).error == "too_large"
