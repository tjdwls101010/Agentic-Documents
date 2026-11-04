"""read의 계약. 빠른 시험은 가짜 도구로 분기와 후처리를 보고, 진짜 도구의 계약은 test_real_tools.py가 본다."""
import datetime as dt
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests import builders as b
from tests.conftest import kordoc_result
from tests.test_extract import make_pptx, make_xlsx
from tests.test_search_cli import LONG, docx, hwp, hwpx, pdf


def split(result):
    head, _, body = result.out.partition("\n")
    return json.loads(head), body


# ---------- xlsx: 도구 없이 읽는다 ----------
def test_xlsx_table_keeps_addresses_and_labels(cli, tmp_path):
    path = make_xlsx(tmp_path / "표.xlsx")
    result = cli("read", path)
    assert result.code == 0, result.err
    head, body = split(result)
    assert head["kind"] == "xlsx" and head["page_basis"] == "sheet" and head["page_count"] == 2
    assert head["sheets"][1]["name"] == "Sheet1" and head["sheets"][1]["n"] == 2
    assert "## 1: 요약 (A1:A1)" in body and "## 2: Sheet1 (B3:K9)" in body
    lines = body.splitlines()
    row3 = next(line for line in lines if line.startswith("| 3 |"))
    assert "쿠팡 변동" in row3 and "12.5% [값 0.125]" in row3 and "1,234,567 [값 1234567]" in row3 and "[=SUM(1,2)]" in row3
    assert any(line.startswith("| 9 |") and "먼 칸" in line for line in lines)
    assert not any(line.startswith("| 4 |") for line in lines)  # 빈 행은 빼고 번호는 원래대로
    header = next(line for line in lines if " B " in line and " J " in line)
    assert " C " not in header  # 값이 없는 열은 뺀다


def test_xlsx_pages_selects_sheets(cli, tmp_path):
    head, body = split(cli("read", make_xlsx(tmp_path / "표.xlsx"), "--pages", "2"))
    assert head["pages_returned"] == [2] and "쿠팡 변동" in body and "머리" not in body
    assert len(head["sheets"]) == 2


def test_xlsx_pipe_in_cell_is_escaped(cli, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active["A1"] = "가|나"
    workbook.save(tmp_path / "표.xlsx")
    assert "가\\|나" in split(cli("read", tmp_path / "표.xlsx"))[1]


def test_stale_formula_cache_warning(cli, tmp_path):
    stale = split(cli("read", make_xlsx(tmp_path / "낡음.xlsx", full_calc=True)))[0]
    assert [w["code"] for w in stale["warnings"]] == ["stale_formula_cache"]
    fresh = split(cli("read", make_xlsx(tmp_path / "새.xlsx")))[0]
    assert fresh["warnings"] == []


# ---------- 이어 읽기 ----------
def test_chunks_join_back_to_the_whole(cli, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    for row in range(1, 400):
        workbook.active[f"A{row}"] = f"긴 글자 {row} " + "가" * 50
    workbook.save(tmp_path / "표.xlsx")
    head, whole = split(cli("read", tmp_path / "표.xlsx", "--max-chars", "1000000"))
    assert head["next_offset"] is None and head["chars_total"] == len(whole)
    pieces, offset = [], 0
    while offset is not None:
        head, piece = split(cli("read", tmp_path / "표.xlsx", "--offset", offset, "--max-chars", "3000"))
        assert len(piece) <= 3000
        pieces.append(piece)
        offset = head["next_offset"]
    assert "".join(pieces) == whole and len(pieces) > 3


def test_single_page_over_budget_can_be_continued(cli, tmp_path, node_path, fake_kordoc):
    page = "가" * 5000
    path = pdf(tmp_path / "a.pdf", "text " + LONG)
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, page)])}
    first = split(cli("read", path, "--pages", "1", "--max-chars", "2000", path_prefix=node_path, env=env))
    second = split(cli("read", path, "--pages", "1", "--max-chars", "4000", "--offset", first[0]["next_offset"], path_prefix=node_path, env=env))
    assert first[1] + second[1] == "[p.1]\n" + page and second[0]["next_offset"] is None
    assert first[0]["pages_returned"] == [1] and second[0]["pages_returned"] == [1]


def test_offset_past_the_end_is_empty_not_an_error(cli, tmp_path):
    head, body = split(cli("read", make_xlsx(tmp_path / "표.xlsx"), "--offset", "9999999"))
    assert body == "" and head["next_offset"] is None


# ---------- pdf ----------
def test_pdf_pages_get_headers_and_options_are_explicit(cli, tmp_path, node_path, fake_kordoc):
    path = pdf(tmp_path / "a.pdf", "one " + LONG, "two " + LONG, "three " + LONG)
    log = tmp_path / "log.json"
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "첫 **굵은** 글 ![image](image_001.png)"), (2, "<u>둘</u>")], page_count=3), "FAKE_KORDOC_LOG": str(log)}
    head, body = split(cli("read", path, path_prefix=node_path, env=env))
    assert head["kind"] == "pdf" and head["page_count"] == 3 and head["page_basis"] == "physical"
    assert head["pages_returned"] == [1, 2]
    assert body == "[p.1]\n첫 굵은 글 [그림]\n\n[p.2]\n둘"
    logged = json.loads(log.read_text())
    assert logged["request"]["options"] == {"ocr": False, "images": False, "tableFormat": "gfm"}
    assert logged["request"]["file"] == str(path)
    assert not os.listdir(logged["cwd"]) if os.path.exists(logged["cwd"]) else True


def test_pdf_pages_option_goes_to_kordoc(cli, tmp_path, node_path, fake_kordoc):
    path = pdf(tmp_path / "a.pdf", "one " + LONG, "two " + LONG, "three " + LONG)
    log = tmp_path / "log.json"
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(2, "둘"), (3, "셋")]), "FAKE_KORDOC_LOG": str(log)}
    head, _ = split(cli("read", path, "--pages", "2-3", "--no-tables", path_prefix=node_path, env=env))
    options = json.loads(log.read_text())["request"]["options"]
    assert options["pages"] == [2, 3] and options["tables"] is False
    assert head["pages_returned"] == [2, 3]


def test_pdf_ocr_needs_pages_and_is_passed_on(cli, tmp_path, node_path, fake_kordoc):
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    refused = cli("read", path, "--ocr", path_prefix=node_path)
    assert refused.code == 2 and "--pages" in refused.err
    log = tmp_path / "log.json"
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "둘")]), "FAKE_KORDOC_LOG": str(log)}
    assert cli("read", path, "--pages", "1", "--ocr", path_prefix=node_path, env=env).code == 0
    assert json.loads(log.read_text())["request"]["options"]["ocr"] is True


TABLE_WITH_EMPTY_CELLS = "| 제품 | 값 | 비고 |\n| --- | --- | --- |\n| 가 |  |  |\n| 나 |  | x |\n| 다 | 1 | y |\n"


def test_empty_cells_with_images_warns_only_when_both_are_on_the_page(cli, tmp_path, node_path, fake_kordoc):
    with_image = b.write_pdf_with_image(tmp_path / "그림.pdf")
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, TABLE_WITH_EMPTY_CELLS)])}
    head = split(cli("read", with_image, path_prefix=node_path, env=env))[0]
    warning = next(w for w in head["warnings"] if w["code"] == "empty_cells_with_images")
    assert warning["pages"] == [1] and "image" in warning["message"]
    assert head["image_pages"][0]["page"] == 1 and head["image_pages"][0]["images"] == 1 and head["image_pages"][0]["image_area"] > 0

    without_image = pdf(tmp_path / "글.pdf", "text " + LONG)
    head = split(cli("read", without_image, path_prefix=node_path, env=env))[0]
    assert head["warnings"] == [] and head["image_pages"] == []

    no_empty = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n")])}
    assert split(cli("read", with_image, path_prefix=node_path, env=no_empty))[0]["warnings"] == []


CONTACT_TABLE = "| 담당 부서 | 부서 | 직책 | 이름 |\n| --- | --- | --- | --- |\n" + "| 공통 | 과 | 사무관 | 홍길동 |\n" * 8 + "|  |  | 사무관 | 홍길동 |\n"
ICON_TABLE = "| 그룹 | 라벨 | 공시 |\n| --- | --- | --- |\n| 1 |  |  |\n| 2 |  |  |\n| 3 |  |  |\n"


def test_small_logo_beside_a_merged_contact_table_does_not_warn(cli, tmp_path, node_path, fake_kordoc):
    # 쪽 끝의 작은 로고와 병합 칸이 있는 연락처 표는 값이 그림인 표가 아니다.
    logo = b.write_pdf_with_image(tmp_path / "로고.pdf", image_size=(30, 30))
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, CONTACT_TABLE)])}
    assert split(cli("read", logo, path_prefix=node_path, env=env))[0]["warnings"] == []


def test_small_icons_in_a_mostly_empty_table_still_warn(cli, tmp_path, node_path, fake_kordoc):
    # 작은 아이콘이 값인 표(자사우대 35쪽의 모양)는 그림 면적이 작아도 칸이 대부분 비어 있다.
    icons = b.write_pdf_with_image(tmp_path / "아이콘.pdf", image_size=(30, 30))
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, ICON_TABLE)])}
    assert [w["code"] for w in split(cli("read", icons, path_prefix=node_path, env=env))[0]["warnings"]] == ["empty_cells_with_images"]


SENTENCE = "이 칸은 표로 오인된 문장의 한 조각으로 충분히 길다"


def test_layout_suspect_on_two_column_sentence_table(cli, tmp_path, node_path, fake_kordoc):
    table = "| 머리 | 머리 |\n| --- | --- |\n" + f"| {SENTENCE} | {SENTENCE} |\n" * 5
    path = pdf(tmp_path / "a.pdf", "text " + LONG)
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, table)])}
    warning = next(w for w in split(cli("read", path, path_prefix=node_path, env=env))[0]["warnings"] if w["code"] == "layout_suspect")
    assert warning["pages"] == [1] and "--no-tables" in warning["message"] and "image" in warning["message"]


def test_layout_suspect_on_two_column_page(cli, tmp_path, node_path, fake_kordoc):
    path = b.write_two_column_pdf(tmp_path / "단.pdf")
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "글")])}
    assert [w["code"] for w in split(cli("read", path, path_prefix=node_path, env=env))[0]["warnings"]] == ["layout_suspect"]


def test_many_column_page_is_not_multi_column(cli, tmp_path, node_path, fake_kordoc):
    # 세로 틈이 여럿이면 다단이 아니라 표다.
    lines = []
    for row in range(14):
        y = 650 - row * 20
        lines.append(b"BT /F1 11 Tf 40 %d Td (a long first column text value here %d) Tj ET" % (y, row))
        for x in (300, 400, 480):
            lines.append(b"BT /F1 11 Tf %d %d Td (cell %d) Tj ET" % (x, y, row))
    path = b.write_pdf_stream(tmp_path / "표.pdf", b" ".join(lines))
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "글")])}
    assert split(cli("read", path, path_prefix=node_path, env=env))[0]["warnings"] == []


def test_short_two_column_table_is_not_suspect(cli, tmp_path, node_path, fake_kordoc):
    table = "| 이름 | 값 |\n| --- | --- |\n" + "| 짧다 | 1 |\n" * 5
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, table)])}
    assert split(cli("read", pdf(tmp_path / "a.pdf", "text " + LONG), path_prefix=node_path, env=env))[0]["warnings"] == []


def test_kordoc_warnings_are_moved_and_hidden_filter_is_dropped(cli, tmp_path, node_path, fake_kordoc):
    warnings = [
        {"code": "SKIPPED_IMAGE", "message": "그림 건너뜀", "page": 1},
        {"code": "SKIPPED_IMAGE", "message": "그림 건너뜀", "page": 2},
        {"code": "NEEDS_OCR", "message": "글자 없음", "page": 2},
        {"code": "HIDDEN_TEXT_FILTERED", "message": "1개 머리글 제거"},
    ]
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "글"), (2, "")], warnings=warnings)}
    head = split(cli("read", pdf(tmp_path / "a.pdf", "a " + LONG, "b " + LONG), path_prefix=node_path, env=env))[0]
    codes = {w["code"]: w for w in head["warnings"]}
    assert set(codes) == {"SKIPPED_IMAGE", "NEEDS_OCR"}
    assert codes["SKIPPED_IMAGE"]["pages"] == [1, 2]


def test_kordoc_ocr_messages_only_suggest_what_can_run(cli, tmp_path, node_path, fake_kordoc):
    # 진짜 kordoc의 문구는 `--ocr`만 말하는데 그렇게 부르면 종료 코드 2다(독립 검토 재현).
    warnings = [
        {"code": "NEEDS_OCR", "message": "이미지 기반 PDF — ocr: true (CLI --ocr) 로 다시 파싱하면 읽습니다"},
        {"code": "SKIPPED_IMAGE", "message": "그림 속 글은 ocr: true / CLI --ocr 로 읽습니다", "page": 2},
    ]
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, ""), (2, "글")], warnings=warnings)}
    head = split(cli("read", pdf(tmp_path / "a.pdf", "a " + LONG, "b " + LONG), path_prefix=node_path, env=env))[0]
    codes = {w["code"]: w for w in head["warnings"]}
    assert codes["NEEDS_OCR"]["pages"] == [1, 2]  # 쪽 없는 경고는 읽은 쪽 전체를 말한다
    for warning in codes.values():
        assert "image" in warning["message"]
        assert "CLI --ocr" not in warning["message"] and "ocr: true" not in warning["message"]
    assert "--ocr --pages" in codes["NEEDS_OCR"]["message"]


@pytest.mark.parametrize("command", ["read", "image"])
def test_wide_page_range_is_refused_quickly(cli, tmp_path, command):
    path = pdf(tmp_path / "a.pdf", "a " + LONG, "b " + LONG)
    started = time.monotonic()
    env = {}
    result = cli(command, path, "--pages", "1-100000", cwd=tmp_path, timeout=20)
    assert result.code in (2, 4)
    assert time.monotonic() - started < 10
    huge = cli(command, path, "--pages", "1-99999999999", cwd=tmp_path, timeout=20)
    assert huge.code == 2


def test_pages_out_of_range_says_the_count(cli, tmp_path, node_path, fake_kordoc):
    result = cli("read", pdf(tmp_path / "a.pdf", "a " + LONG, "b " + LONG), "--pages", "3", path_prefix=node_path)
    assert result.code == 2 and "2쪽" in result.err


@pytest.mark.parametrize("value", ["0", "x", "3-1", "1,,2", ""])
def test_bad_page_syntax(cli, tmp_path, value):
    assert cli("read", make_xlsx(tmp_path / "표.xlsx"), "--pages", value).code == 2


def test_page_syntax_matches_read(cli, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    for number in range(1, 8):
        workbook.create_sheet(f"S{number}")["A1"] = f"시트{number}"
    workbook.save(tmp_path / "표.xlsx")
    assert split(cli("read", tmp_path / "표.xlsx", "--pages", "1,3,5-7"))[0]["pages_returned"] == [1, 3, 5, 6, 7]


# ---------- docx, pptx ----------
def test_docx_reads_without_page_marks_and_refuses_pages(cli, tmp_path, node_path, fake_kordoc):
    path = docx(tmp_path / "a.docx", "본문 " + LONG)
    env = {"FAKE_KORDOC_RESULT": kordoc_result(markdown="본문 글")}
    head, body = split(cli("read", path, path_prefix=node_path, env=env))
    assert head["kind"] == "docx" and head["page_basis"] == "none" and head["page_count"] is None and body == "본문 글"
    refused = cli("read", path, "--pages", "1", path_prefix=node_path, env=env)
    assert refused.code == 2 and "--offset" in refused.err


def test_pptx_slides_get_headers_and_picture_counts(cli, tmp_path, node_path, fake_kordoc):
    path = make_pptx(tmp_path / "a.pptx", ["하나", "둘"])
    env = {"FAKE_KORDOC_RESULT": kordoc_result(pages=[(1, "하나 ![image](image_001.png)"), (2, "둘")], page_count=2)}
    head, body = split(cli("read", path, path_prefix=node_path, env=env))
    assert head["page_basis"] == "slide" and head["page_count"] == 2
    assert body == "[p.1]\n하나 [그림]\n\n[p.2]\n둘"
    assert head["image_pages"] == [{"page": 1, "images": 1, "image_area": None}]


# ---------- hwp, hwpx ----------
def test_hwp_whole_uses_kordoc_and_counts_from_rhwp(cli, tmp_path, node_path, fake_kordoc, fake_rhwp):
    path = hwp(tmp_path / "a.hwp", "본문 " + LONG)
    env = {"FAKE_KORDOC_RESULT": kordoc_result(markdown="전체 **글**"), "FAKE_RHWP_PAGES": json.dumps(["쪽1", "쪽2 ![image 1](x_assets/x.bin)"])}
    head, body = split(cli("read", path, path_prefix=f"{node_path}:{fake_rhwp}", env=env))
    assert body == "전체 글"
    assert head["kind"] == "hwp" and head["page_count"] == 2 and head["page_basis"] == "estimated"
    assert head["image_pages"] == [{"page": 2, "images": 1, "image_area": None}]
    assert head["pages_returned"] == []


def test_hwp_whole_without_rhwp_still_reads(cli, tmp_path, node_path, fake_kordoc):
    env = {"FAKE_KORDOC_RESULT": kordoc_result(markdown="전체 글")}
    head, body = split(cli("read", hwpx(tmp_path / "a.hwpx", "본문 " + LONG), path_prefix=node_path, env=env))
    assert body == "전체 글" and head["page_count"] is None and head["page_basis"] == "none" and head["image_pages"] is None
    assert any("doctor --fix" in w["message"] for w in head["warnings"])


def test_hwp_pages_use_rhwp_markdown_with_headers(cli, tmp_path, fake_rhwp):
    path = hwp(tmp_path / "a.hwp", "본문 " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["쪽1", "쪽2 **굵게** ![image 1](x_assets/x.bin)", "쪽3"])}
    head, body = split(cli("read", path, "--pages", "2-3", path_prefix=fake_rhwp, env=env))
    assert body == "[p.2]\n쪽2 굵게 [그림]\n\n[p.3]\n쪽3"
    assert head["page_basis"] == "estimated" and head["pages_returned"] == [2, 3] and head["page_count"] == 3
    assert head["image_pages"] == [{"page": 2, "images": 1, "image_area": None}]
    assert any(w["code"] == "table_may_span_pages" for w in head["warnings"])
    assert cli("read", path, "--pages", "4", path_prefix=fake_rhwp, env=env).code == 2


def test_hwp_pages_without_rhwp_is_exit_4(cli, tmp_path):
    result = cli("read", hwp(tmp_path / "a.hwp", "본문 " + LONG), "--pages", "1")
    assert result.code == 4 and "doctor --fix" in result.err


def test_long_file_name_is_read_through_a_short_link(cli, tmp_path, fake_rhwp):
    # 어떤 파일 시스템에서는 rhwp의 출력 이름이 한도를 넘는다. 긴 이름을 도구에 넘기지 않는다.
    name = "가" * 78 + ".hwp"  # 약 240바이트
    path = hwp(tmp_path / name, "본문 " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["쪽1"]), "FAKE_RHWP_MAX_NAME": "32"}
    result = cli("read", path, "--pages", "1", path_prefix=fake_rhwp, env=env)
    assert result.code == 0, result.err
    head = split(result)[0]
    assert head["path"] == str(path)


# ---------- 경로를 몰래 안 바꾼다 ----------
def test_given_path_is_read_and_same_name_pdf_is_only_pointed_to(cli, tmp_path, node_path, fake_kordoc):
    source = hwpx(tmp_path / "보도.hwpx", "본문 " + LONG)
    twin = pdf(tmp_path / "보도.pdf", "pdf " + LONG)
    log = tmp_path / "log.json"
    env = {"FAKE_KORDOC_RESULT": kordoc_result(markdown="글"), "FAKE_KORDOC_LOG": str(log)}
    head = split(cli("read", source, path_prefix=node_path, env=env))[0]
    assert json.loads(log.read_text())["request"]["file"] == str(source)
    assert head["path"] == str(source) and head["pdf"] == str(twin)
    alone = split(cli("read", hwpx(tmp_path / "외톨이.hwpx", "x " + LONG), path_prefix=node_path, env=env))[0]
    assert "pdf" not in alone


# ---------- 실패와 종료 코드 ----------
def test_missing_path_and_folder_are_exit_2(cli, tmp_path):
    assert cli("read", tmp_path / "없다.pdf").code == 2
    assert cli("read", tmp_path).code == 2


def test_out_of_scope_file_points_to_read(cli, tmp_path):
    (tmp_path / "메모.md").write_text("글")
    result = cli("read", tmp_path / "메모.md")
    assert result.code == 2 and "Read" in result.err
    (tmp_path / "자료.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    assert cli("read", tmp_path / "자료.zip").code == 2


@pytest.mark.parametrize(
    "maker, code",
    [
        (lambda p: hwp(p / "암호.hwp", "x", encrypted=True), "encrypted"),
        (lambda p: hwp(p / "배포.hwp", "x", distribution=True), "distribution"),
        (lambda p: Path(__import__("shutil").copy(Path(__file__).parent / "fixtures/encrypted.pdf", p / "암호.pdf")), "encrypted"),
        (lambda p: b.write_zip(p / "압축.docx", {"x.txt": "y"}), "unsupported"),
    ],
)
def test_unreadable_target_is_exit_3_with_json(cli, tmp_path, node_path, fake_kordoc, maker, code):
    result = cli("read", maker(tmp_path), path_prefix=node_path, env={"FAKE_KORDOC_RESULT": kordoc_result()})
    assert result.code == 3
    assert result.json["error"]["code"] == code and result.json["error"]["message"]


def test_empty_document_extension_is_exit_3(cli, tmp_path):
    (tmp_path / "빈.hwp").write_bytes(b"")
    result = cli("read", tmp_path / "빈.hwp")
    assert result.code == 3 and result.json["error"]["code"] == "not_a_document"


def test_kordoc_failure_maps_to_exit_3(cli, tmp_path, node_path, fake_kordoc):
    env = {"FAKE_KORDOC_RESULT": json.dumps({"success": False, "error": "손상", "code": "CORRUPTED"})}
    result = cli("read", pdf(tmp_path / "a.pdf", "a " + LONG), path_prefix=node_path, env=env)
    assert result.code == 3 and result.json["error"]["code"] == "corrupt"


def test_image_based_pdf_is_not_a_failure(cli, tmp_path, node_path, fake_kordoc):
    # 글자층이 없는 PDF는 못 읽은 것이 아니라 그림으로 볼 문서다.
    env = {"FAKE_KORDOC_RESULT": json.dumps({"success": False, "error": "이미지 PDF", "code": "IMAGE_BASED_PDF"})}
    result = cli("read", pdf(tmp_path / "a.pdf", None, None), path_prefix=node_path, env=env)
    assert result.code == 0
    head, body = split(result)
    warning = next(w for w in head["warnings"] if w["code"] == "NEEDS_OCR")
    assert warning["pages"] == [1, 2] and "image" in warning["message"]
    assert head["page_count"] == 2 and body == ""


def test_missing_kordoc_is_exit_4(cli, tmp_path, node_path):
    result = cli("read", pdf(tmp_path / "a.pdf", "a " + LONG), path_prefix=node_path)
    assert result.code == 4 and "doctor --fix" in result.err


def test_missing_node_is_exit_4(cli, tmp_path, fake_kordoc):
    result = cli("read", pdf(tmp_path / "a.pdf", "a " + LONG))
    assert result.code == 4 and "doctor --fix" in result.err


def test_other_kordoc_version_is_refused(cli, tmp_path, node_path, fake_kordoc):
    (fake_kordoc / "package.json").write_text(json.dumps({"version": "4.0.0"}))
    result = cli("read", pdf(tmp_path / "a.pdf", "a " + LONG), path_prefix=node_path, env={"FAKE_KORDOC_RESULT": kordoc_result()})
    assert result.code == 4


@pytest.mark.parametrize("mode", ["bad_protocol", "crash"])
def test_kordoc_protocol_problems_are_exit_4(cli, tmp_path, node_path, fake_kordoc, mode):
    env = {"FAKE_KORDOC_MODE": mode, "FAKE_KORDOC_RESULT": kordoc_result()}
    assert cli("read", pdf(tmp_path / "a.pdf", "a " + LONG), path_prefix=node_path, env=env).code == 4


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_hanging_kordoc_is_killed_with_descendants(tmp_path, home, node_path, fake_kordoc):
    from documents import kordoc

    pidfile = tmp_path / "pid"
    environment = {**os.environ, "HOME": str(home), "PATH": f"{node_path}:/usr/bin:/bin", "FAKE_KORDOC_MODE": "hang", "FAKE_KORDOC_PIDFILE": str(pidfile)}
    keys = ("HOME", "PATH", "FAKE_KORDOC_MODE", "FAKE_KORDOC_PIDFILE")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({key: environment[key] for key in keys})
    try:
        started = time.monotonic()
        with pytest.raises(kordoc.ToolError):
            kordoc.parse(str(tmp_path / "x.pdf"), {}, timeout=1.5)
        assert time.monotonic() - started < 15
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    deadline = time.monotonic() + 5
    pids = [int(item) for item in pidfile.read_text().split()]
    assert len(pids) == 2
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(alive(pid) for pid in pids)


def test_hanging_rhwp_is_killed_with_descendants(tmp_path):
    from documents import rhwp

    script = tmp_path / "rhwp"
    pidfile = tmp_path / "pids"
    script.write_text(f"#!/bin/sh\nsleep 301 &\necho $$ $! > {pidfile}\nwait\n")
    script.chmod(0o755)
    with pytest.raises(rhwp.ToolError):
        rhwp.page_texts(tmp_path / "x.hwp", str(script), timeout=1)
    time.sleep(0.2)
    for pid in map(int, pidfile.read_text().split()):
        assert not alive(pid), pid


# ---------- 예외 형식 ----------
def test_kordoc_exceptions_are_tried(cli, tmp_path, node_path, fake_kordoc):
    env = {"FAKE_KORDOC_RESULT": kordoc_result(markdown="옛 문서 글")}
    old = tmp_path / "옛.hwp"
    old.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 100)
    hml = tmp_path / "표.hml"
    hml.write_text('<?xml version="1.0"?><HWPML/>')
    xls = b.write_cfb(tmp_path / "구형.xls", {"Workbook": b"x"})
    for path, kind in [(old, "hwp3"), (hml, "hml"), (xls, "xls")]:
        result = cli("read", path, path_prefix=node_path, env=env)
        assert result.code == 0, result.err
        head, body = split(result)
        assert head["page_basis"] == "none" and head["kind"] == kind and body == "옛 문서 글"


def test_kordoc_exception_failure_is_exit_3(cli, tmp_path, node_path, fake_kordoc):
    xls = b.write_cfb(tmp_path / "구형.xls", {"Workbook": b"x"})
    env = {"FAKE_KORDOC_RESULT": json.dumps({"success": False, "error": "못 읽음", "code": "PARSE_ERROR"})}
    assert cli("read", xls, path_prefix=node_path, env=env).code == 3


def test_search_still_reports_those_as_not_read_or_unsupported(cli, tmp_path):
    (tmp_path / "옛.hwp").write_bytes(b"HWP Document File V3.00 " + b"\0" * 100)
    b.write_cfb(tmp_path / "구형.xls", {"Workbook": b"x"})
    coverage = cli("search", "말", tmp_path).json["coverage"]
    assert coverage["unreadable"]["items"][0]["reason"] == "unsupported" and coverage["not_read"] == {"xls": 1}


def test_options_for_the_wrong_format_are_exit_2(cli, tmp_path):
    path = make_xlsx(tmp_path / "표.xlsx")
    no_tables = cli("read", path, "--no-tables")
    assert no_tables.code == 2 and "pdf" in no_tables.err.lower()
    ocr = cli("read", path, "--ocr", "--pages", "1")
    assert ocr.code == 2 and "image" in ocr.err


def test_read_help_states_the_contract(cli):
    text = cli("read", "--help").out
    for word in ["page_basis", "physical", "estimated", "empty_cells_with_images", "layout_suspect", "--no-tables", "쪽당", "종료 코드", "[p.N]"]:
        assert word in text, word
