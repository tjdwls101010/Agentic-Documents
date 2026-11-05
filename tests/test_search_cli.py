"""search·text의 계약을 실제 진입점으로 본다. 쌍둥이 없는 트리에서도 같은 계약이어야 한다(D7)."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from tests import builders as b
from tests.conftest import CLI


def para(text):
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def hwpx(path: Path, *paragraphs, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    return b.write_hwpx(path, [b.hwpx_section("".join(para(text) for text in paragraphs))], **options)


def hwp(path: Path, *paragraphs, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    return b.write_hwp(path, [[b.hwp_paragraph(text) for text in paragraphs]], **options)


def pdf(path: Path, *pages):
    path.parent.mkdir(parents=True, exist_ok=True)
    return b.write_pdf(path, list(pages))


def docx(path: Path, *paragraphs):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return b.write_zip(path, b.docx_members(body))


def by_name(result):
    return {os.path.basename(f["path"]): f for doc in result["results"] for f in doc["files"]}


LONG = "x" * 60  # no_text 문턱(50자)을 넘기는 채움


# ---------- 쌍둥이 없는 트리 ----------
def test_single_hwp_argument(cli, tmp_path):
    path = hwp(tmp_path / "문서.hwp", "담합 제재 " + LONG)
    result = cli("search", "담합", path)
    assert result.code == 0
    data = result.json
    assert data["summary"]["files_scanned"] == 1 and data["summary"]["documents_hit"] == 1
    doc = data["results"][0]
    assert doc["other_files"] == [] and len(doc["files"]) == 1
    assert doc["files"][0]["kind"] == "hwp" and doc["files"][0]["exact"] == 1


def test_flat_docx_only_folder(cli, tmp_path):
    docx(tmp_path / "a.docx", "찾는 말 " + LONG)
    docx(tmp_path / "b.docx", "다른 글 " + LONG)
    data = cli("search", "찾는 말", tmp_path).json
    assert data["summary"]["files_scanned"] == 2
    assert [os.path.basename(doc["files"][0]["path"]) for doc in data["results"]] == ["a.docx"]
    assert data["results"][0]["files"][0]["page_basis"] == "none"


def test_deep_folder_and_default_is_current_folder(cli, tmp_path):
    hwpx(tmp_path / "가" / "나" / "다" / "깊은.hwpx", "깊은 말 " + LONG)
    data = cli("search", "깊은 말", cwd=tmp_path).json
    assert data["results"][0]["files"][0]["path"] == "가/나/다/깊은.hwpx"
    assert data["summary"]["by_folder"] == {"가": 1}


def test_zero_hits_is_success(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "다른 글 " + LONG)
    result = cli("search", "없는말", tmp_path)
    assert result.code == 0
    assert result.json["results"] == [] and result.json["summary"]["documents_hit"] == 0


# ---------- 표기와 변형 ----------
def test_exact_and_variants_are_separated(cli, tmp_path):
    hwpx(tmp_path / "표기.hwpx", "주민등록번호 처리제한 " + LONG)
    hwpx(tmp_path / "변형.hwpx", "주민 등록번호 처리제한 " + LONG)
    data = cli("search", "주민등록번호 처리제한", tmp_path).json
    assert data["summary"]["exact_documents"] == 1 and data["summary"]["spacing_only_documents"] == 1
    files = by_name(data)
    assert (files["표기.hwpx"]["exact"], files["표기.hwpx"]["spacing_variants"]) == (1, 0)
    assert (files["변형.hwpx"]["exact"], files["변형.hwpx"]["spacing_variants"]) == (0, 1)
    assert [doc["files"][0]["path"].endswith("표기.hwpx") for doc in data["results"]] == [True, False]


def test_damhap_false_friend_is_only_a_variant(cli, tmp_path):
    hwpx(tmp_path / "오탐.hwpx", "고객 부담 합리화 방안 " + LONG)
    data = cli("search", "담합", tmp_path).json
    file = data["results"][0]["files"][0]
    assert file["exact"] == 0 and file["spacing_variants"] == 1
    assert data["results"][0]["snippets"][0]["exact"] is False


def test_paragraph_boundary_is_not_a_match_but_pdf_line_is(cli, tmp_path):
    hwpx(tmp_path / "문단.hwpx", "의무화", "노사정 합의안 " + LONG)
    assert cli("search", "의무화 노사정", tmp_path).json["results"] == []


def test_folding_finds_circled_and_keeps_bullet_jamo(cli, tmp_path):
    hwpx(tmp_path / "주식회사.hwpx", "㈜코스모 " + LONG, "ㅇ 글머리표")
    data = cli("search", "(주)코스모", tmp_path).json
    assert data["results"][0]["files"][0]["exact"] == 1
    assert "(주)코스모" in data["results"][0]["snippets"][0]["text"]
    assert cli("search", "ㅇ 글머리표", tmp_path).json["results"][0]["files"][0]["exact"] == 1


def test_regex_counts_as_exact_and_is_not_folded(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "제12조 위반 " + LONG, "제3조 위반", "㈜회사")
    data = cli("search", r"^제\d+조", tmp_path, "--regex").json
    file = data["results"][0]["files"][0]
    assert file["exact"] == 2 and file["spacing_variants"] == 0
    assert cli("search", "㈜", tmp_path, "--regex").json["results"] == []
    assert cli("search", r"\(주\)회사", tmp_path, "--regex").json["results"][0]["files"][0]["exact"] == 1


def test_regex_empty_matches_are_not_counted(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "글 " + LONG)
    assert cli("search", "z*", tmp_path, "--regex").json["results"] == []


def test_and_requires_every_word_in_the_same_file(cli, tmp_path):
    hwpx(tmp_path / "둘다.hwpx", "과징금 처분", "담합 사건 " + LONG)
    hwpx(tmp_path / "하나.hwpx", "과징금 처분 " + LONG)
    data = cli("search", "과징금", tmp_path, "--and", "담합").json
    assert [os.path.basename(doc["files"][0]["path"]) for doc in data["results"]] == ["둘다.hwpx"]
    assert data["results"][0]["files"][0]["exact"] == 1  # 주 질의만 센다


# ---------- 파일 이름 ----------
def test_filename_match_with_no_body_hit(cli, tmp_path):
    hwpx(tmp_path / "담합 제재 자료.hwpx", "본문에는 없음 " + LONG)
    data = cli("search", "담합제재", tmp_path).json
    doc = data["results"][0]
    assert doc["filename_match"] is True and doc["files"] == []
    assert [os.path.basename(p) for p in doc["other_files"]] == ["담합 제재 자료.hwpx"]
    assert data["summary"]["filename_only_documents"] == 1


def test_filename_is_ignored_with_and(cli, tmp_path):
    hwpx(tmp_path / "담합.hwpx", "본문에는 없음 " + LONG)
    assert cli("search", "담합", tmp_path, "--and", "본문").json["results"] == []


def test_body_hit_ranks_before_filename_only(cli, tmp_path):
    hwpx(tmp_path / "a 담합.hwpx", "없음 " + LONG)
    hwpx(tmp_path / "z.hwpx", "담합 " + LONG)
    data = cli("search", "담합", tmp_path).json
    assert [os.path.basename(d["document"]) for d in data["results"]] == ["z", "a 담합"]


# ---------- 묶음 ----------
def test_twins_are_each_searched_and_grouped_for_display(cli, tmp_path):
    hwpx(tmp_path / "보도.hwpx", "남운수 상병 " + LONG)
    pdf(tmp_path / "보도.pdf", "Namwoonsu sergeant first class " + LONG)
    hwp(tmp_path / "보도.hwp", "남운수 상병 " + LONG)
    data = cli("search", "상병", tmp_path).json
    assert data["summary"]["documents_hit"] == 1
    doc = data["results"][0]
    assert sorted(os.path.basename(f["path"]) for f in doc["files"]) == ["보도.hwp", "보도.hwpx"]
    assert [os.path.basename(p) for p in doc["other_files"]] == ["보도.pdf"]
    # 다른 내용이면 다른 쪽이 걸린다.
    other = cli("search", "sergeant", tmp_path).json["results"][0]
    assert [os.path.basename(f["path"]) for f in other["files"]] == ["보도.pdf"]
    assert sorted(os.path.basename(p) for p in other["other_files"]) == ["보도.hwp", "보도.hwpx"]


def test_same_text_snippet_appears_once_in_a_group(cli, tmp_path):
    hwpx(tmp_path / "보도.hwpx", "같은 문장을 찾는다 " + LONG)
    hwp(tmp_path / "보도.hwp", "같은 문장을 찾는다 " + LONG)
    doc = cli("search", "같은 문장", tmp_path).json["results"][0]
    assert len(doc["files"]) == 2 and len(doc["snippets"]) == 1


def test_different_folders_are_not_grouped(cli, tmp_path):
    hwpx(tmp_path / "a" / "같은이름.hwpx", "찾는말 " + LONG)
    hwpx(tmp_path / "b" / "같은이름.hwpx", "찾는말 " + LONG)
    assert cli("search", "찾는말", tmp_path).json["summary"]["documents_hit"] == 2


# ---------- 형식별 위치 ----------
def test_pdf_pages_are_physical(cli, tmp_path):
    pdf(tmp_path / "a.pdf", "first page text " + LONG, None, "needle in third " + LONG)
    data = cli("search", "needle", tmp_path).json
    file = data["results"][0]["files"][0]
    assert file["pages"] == [3] and file["page_basis"] == "physical"
    assert data["results"][0]["snippets"][0]["page"] == 3


def test_pdf_query_crosses_typeset_lines(cli, tmp_path):
    # 한 쪽 안의 조판 줄바꿈은 공백 묶음으로 넘는다. 합성 PDF는 Td로 줄을 나눈다.
    path = tmp_path / "줄.pdf"
    stream = b"BT /F1 12 Tf 50 700 Td (alpha) Tj 0 -20 Td (omega) Tj ET " + b"BT /F1 12 Tf 50 600 Td (" + LONG.encode() + b") Tj ET"
    b.write_pdf_stream(path, stream)
    file = cli("search", "alpha omega", tmp_path).json["results"][0]["files"][0]
    assert file["exact"] == 1


def test_xlsx_cells_give_sheet_and_address_and_cap(cli, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active.title = "표지"
    sheet = workbook.create_sheet("Sheet1")
    for row in range(1, 13):
        sheet[f"C{row}"] = "쿠팡 변동"
    workbook.save(tmp_path / "표.xlsx")
    file = cli("search", "쿠팡", tmp_path).json["results"][0]["files"][0]
    assert file["kind"] == "xlsx" and file["page_basis"] == "sheet" and file["exact"] == 12
    assert file["cells"] == [f"2:Sheet1!C{row}" for row in range(1, 11)]
    assert file["cells_more"] == 2
    assert "pages" not in file


def test_pptx_page_is_display_order(cli, tmp_path):
    from tests.test_extract import make_pptx

    make_pptx(tmp_path / "발표.pptx", ["첫 파일 " + LONG, "찾을 말 " + LONG, "셋째 " + LONG], order=[1, 2, 0])
    file = cli("search", "찾을 말", tmp_path).json["results"][0]["files"][0]
    assert file["pages"] == [1] and file["page_basis"] == "slide"


def test_hwp_pages_come_from_rhwp(cli, tmp_path, fake_rhwp):
    hwp(tmp_path / "a.hwp", "담합 사건 " + LONG)
    pages = json.dumps(["표지", "담\n합 사건", "무관", "또 담합"])
    file = cli("search", "담합", tmp_path, path_prefix=fake_rhwp, env={"FAKE_RHWP_PAGES": pages}).json
    entry = file["results"][0]["files"][0]
    assert entry["pages"] == [2, 4] and entry["page_basis"] == "estimated"
    assert file["results"][0]["snippets"][0]["page"] is None
    assert file["summary"]["notes"] == []


def test_hwp_page_undetermined_is_empty_list(cli, tmp_path, fake_rhwp):
    hwpx(tmp_path / "a.hwpx", "담합 사건 " + LONG)
    entry = cli("search", "담합", tmp_path, path_prefix=fake_rhwp, env={"FAKE_RHWP_PAGES": json.dumps(["다른 글"])}).json["results"][0]["files"][0]
    assert entry["pages"] == [] and entry["page_basis"] == "estimated"


def test_regex_page_uses_same_regex_and_and_words_do_not_mark_pages(cli, tmp_path, fake_rhwp):
    hwp(tmp_path / "a.hwp", "제12조 위반", "과징금 " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["제12조 위반", "과징금", "줄\n제12조"])}
    entry = cli("search", r"^제\d+조", tmp_path, "--regex", path_prefix=fake_rhwp, env=env).json["results"][0]["files"][0]
    assert entry["pages"] == [1, 3]
    anded = cli("search", "위반", tmp_path, "--and", "과징금", path_prefix=fake_rhwp, env=env).json["results"][0]["files"][0]
    assert anded["pages"] == [1]


@pytest.mark.parametrize("mode", ["fail", "garbage", "truncated"])
def test_rhwp_failure_gives_null_pages_and_a_note(cli, tmp_path, fake_rhwp, mode):
    hwp(tmp_path / "a.hwp", "담합 " + LONG)
    data = cli("search", "담합", tmp_path, path_prefix=fake_rhwp, env={"FAKE_RHWP_MODE": mode, "FAKE_RHWP_PAGES": "[]"}).json
    entry = data["results"][0]["files"][0]
    assert entry["pages"] is None and entry["page_basis"] == "none"
    assert any("doctor --fix" in note for note in data["summary"]["notes"])


def test_missing_rhwp_gives_null_pages_and_a_note(cli, tmp_path):
    hwp(tmp_path / "a.hwp", "담합 " + LONG)
    data = cli("search", "담합", tmp_path).json
    assert data["results"][0]["files"][0]["pages"] is None
    assert any("doctor --fix" in note for note in data["summary"]["notes"])


def test_other_rhwp_version_is_not_used(cli, tmp_path, fake_rhwp):
    hwp(tmp_path / "a.hwp", "담합 " + LONG)
    env = {"FAKE_RHWP_VERSION": "0.7.0", "FAKE_RHWP_PAGES": json.dumps(["담합"])}
    assert cli("search", "담합", tmp_path, path_prefix=fake_rhwp, env=env).json["results"][0]["files"][0]["pages"] is None


# ---------- 내용 판별과 범위 보고 ----------
def test_extension_mismatch_and_truncated_names(cli, tmp_path):
    hwp(tmp_path / "속은.hwpx", "찾는말 " + LONG)
    hwpx(tmp_path / "속은2.hwp", "찾는말 " + LONG)
    pdf(tmp_path / "잘린.pd", "needle " + LONG)
    pdf(tmp_path / "확장자없음", "needle " + LONG)
    files = by_name(cli("search", "찾는말", tmp_path).json)
    assert files["속은.hwpx"]["kind"] == "hwp" and files["속은2.hwp"]["kind"] == "hwpx"
    files = by_name(cli("search", "needle", tmp_path).json)
    assert files["잘린.pd"]["kind"] == "pdf" and files["확장자없음"]["kind"] == "pdf"


def test_coverage_reports_every_kind(cli, tmp_path):
    hwpx(tmp_path / "정상.hwpx", "정상 글 " + LONG, bindata=["i1.png", "i2.png"])
    hwp(tmp_path / "암호.hwp", "x", encrypted=True)
    hwp(tmp_path / "배포.hwp", "x", distribution=True)
    (tmp_path / "깨짐.hwpx").write_bytes(b"PK\x03\x04broken")
    (tmp_path / "가짜.pdf").write_bytes(b"<html>")
    hwpx(tmp_path / "빈글.hwpx", "짧다")
    pdf(tmp_path / "그림쪽.pdf", "text page " + LONG, None)
    b.write_zip(tmp_path / "압축.docx", {"x.txt": "y"})
    (tmp_path / "자료.zip").write_bytes(b"PK")
    (tmp_path / "사진.jpg").write_bytes(b"x")
    (tmp_path / "확장자없음").write_bytes(b"plain")
    (tmp_path / "깨진링크.pdf").symlink_to(tmp_path / "없음.pdf")
    coverage = cli("search", "정상", tmp_path).json["coverage"]
    all_records = cli("search", "정상", tmp_path, "--coverage-only").lines
    reasons = {os.path.basename(i["path"]): i["reason"] for i in all_records if i["type"] == "unreadable"}
    assert reasons == {"암호.hwp": "encrypted", "배포.hwp": "distribution", "깨짐.hwpx": "corrupt", "가짜.pdf": "not_a_document", "압축.docx": "unsupported", "깨진링크.pdf": "io"}
    assert coverage["unreadable"]["count"] == 6 and len(coverage["unreadable"]["items"]) == 5 and coverage["unreadable"]["more"] == 1
    assert [os.path.basename(p) for p in coverage["no_text"]["items"]] == ["빈글.hwpx"]
    assert coverage["image_only_pages"]["files"] == 1 and coverage["image_only_pages"]["pages"] == 1
    assert coverage["image_only_pages"]["items"][0]["pages"] == [2]
    assert coverage["embedded_images"] == {"files": 1, "images": 2}
    assert coverage["not_read"] == {"zip": 1, "jpg": 1, "(없음)": 1}


def test_coverage_only_is_ndjson_without_cap(cli, tmp_path):
    for number in range(25):
        hwp(tmp_path / f"암호{number:02}.hwp", "x", encrypted=True)
    (tmp_path / "자료.zip").write_bytes(b"PK")
    capped = cli("search", "x", tmp_path).json["coverage"]["unreadable"]
    assert capped["count"] == 25 and len(capped["items"]) == 5 and capped["more"] == 20
    lines = cli("search", "x", tmp_path, "--coverage-only").lines
    assert sum(line["type"] == "unreadable" for line in lines) == 25
    assert [line for line in lines if line["type"] == "not_read"][0]["extension"] == "zip"
    assert all("results" not in line for line in lines)


# ---------- 경로 ----------
def test_nfd_path_and_glob(cli, tmp_path):
    folder = unicodedata.normalize("NFD", "공정위")
    hwpx(tmp_path / folder / "보도.hwpx", "찾는말 " + LONG)
    hwpx(tmp_path / "다른" / "보도.hwpx", "찾는말 " + LONG)
    data = cli("search", "찾는말", tmp_path, "--glob", "공정위/*").json
    assert data["summary"]["files_scanned"] == 1
    # 출력 경로는 디스크 그대로라 그대로 열 수 있다.
    assert os.path.exists(data["results"][0]["files"][0]["path"])


def test_symlinks_in_a_tree(cli, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "밖"
    hwpx(root / "진짜.hwpx", "찾는말 " + LONG)
    hwpx(outside / "밖.hwpx", "찾는말 " + LONG)
    (root / "순환").symlink_to(root, target_is_directory=True)
    (root / "z복제.hwpx").symlink_to(root / "진짜.hwpx")
    (root / "밖으로").symlink_to(outside, target_is_directory=True)
    data = cli("search", "찾는말", root).json
    assert data["summary"]["files_scanned"] == 2


def test_hidden_files_are_skipped_unless_named(cli, tmp_path):
    hwpx(tmp_path / ".documents" / "x.hwpx", "찾는말 " + LONG)
    hwpx(tmp_path / ".숨김.hwpx", "찾는말 " + LONG)
    assert cli("search", "찾는말", tmp_path).json["summary"]["files_scanned"] == 0
    assert cli("search", "찾는말", tmp_path / ".숨김.hwpx").json["summary"]["documents_hit"] == 1


def tree_state(root):
    state = {}
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            state[path] = (info.st_mtime_ns, info.st_size, hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.isfile(path) else None)
    return state


def test_source_tree_is_untouched_and_nothing_is_left_behind(cli, tmp_path):
    work = tmp_path / "작업"
    work.mkdir()
    hwpx(work / "a.hwpx", "찾는말 " + LONG)
    pdf(work / "b.pdf", "needle " + LONG)
    before = tree_state(work)
    cli("search", "찾는말", work, cwd=work)
    with open(os.devnull, "w"):
        cli("text", work, cwd=work)
    assert tree_state(work) == before


# ---------- 이어 받기 ----------
def test_limit_offset_cover_all_results_once(cli, tmp_path):
    for number in range(7):
        hwpx(tmp_path / f"문서{number}.hwpx", "찾는말 " * (number + 1) + LONG)
    seen = []
    offset = 0
    while offset is not None:
        data = cli("search", "찾는말", tmp_path, "--limit", "3", "--offset", offset).json
        seen += [os.path.basename(doc["document"]) for doc in data["results"]]
        assert data["summary"]["returned"] == len(data["results"])
        offset = data["summary"]["next_offset"]
    assert seen == [f"문서{n}" for n in range(6, -1, -1)]  # exact 내림차순


def test_output_is_cut_by_size_with_next_offset(cli, tmp_path):
    for number in range(30):
        hwpx(tmp_path / f"문서{number:02}.hwpx", ("찾는말 " + "가" * 300 + " ") * 3)
    data = cli("search", "찾는말", tmp_path, "--limit", "30", "--context", "300").json
    size = len(json.dumps(data["results"], ensure_ascii=False))
    assert size <= 20000
    assert 0 < data["summary"]["returned"] < 30
    assert data["summary"]["next_offset"] == data["summary"]["returned"]
    next_page = cli("search", "찾는말", tmp_path, "--limit", "30", "--context", "300", "--offset", data["summary"]["next_offset"]).json
    assert next_page["results"]


def test_single_result_larger_than_the_cap_still_progresses(cli, tmp_path):
    hwpx(tmp_path / "큰결과.hwpx", ("찾는말 " + "가" * 3000 + " ") * 4)
    hwpx(tmp_path / "작은결과.hwpx", "찾는말 " + LONG)
    data = cli("search", "찾는말", tmp_path, "--snippets", "4", "--context", "30000").json
    assert data["summary"]["returned"] >= 1
    assert data["summary"]["next_offset"] is None or data["summary"]["next_offset"] >= 1


# ---------- 종료 코드와 도움말 ----------
def test_exit_codes_for_bad_arguments(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "글 " + LONG)
    assert cli("search", "말", tmp_path / "없는경로").code == 2
    assert cli("search", "(", tmp_path, "--regex").code == 2
    assert cli("search", "   ", tmp_path).code == 2
    assert cli("search", "말", tmp_path, "--limit", "0").code == 2
    assert cli("search", "말", tmp_path, "--offset", "-1").code == 2
    assert cli("text", tmp_path / "없는경로").code == 2
    assert cli("없는명령").code == 2
    assert cli().code == 2


def test_broken_file_is_exit_zero(cli, tmp_path):
    (tmp_path / "깨짐.pdf").write_bytes(b"%PDF-1.4 garbage")
    assert cli("search", "말", tmp_path).code == 0


@pytest.mark.parametrize("command", ["search", "text"])
def test_help_states_arguments_output_and_failures(cli, command):
    result = cli(command, "--help")
    assert result.code == 0
    assert "종료 코드" in result.out and "출력" in result.out
    assert "옵션" in result.out


def test_search_help_explains_what_the_output_means(cli):
    text = cli("search", "--help").out
    for word in ["exact", "spacing_variants", "physical", "estimated", "slide", "sheet", "coverage", "next_offset", "대소문자"]:
        assert word in text, word


def test_help_is_not_rewrapped(cli):
    # 줄 단위로 읽으므로 설명이 폭에 맞춰 잘리면 안 된다.
    wide = cli("search", "--help", env={"COLUMNS": "40"}).out
    assert any(len(line) > 60 for line in wide.splitlines())


def test_text_help_warns_that_pipes_need_permission(cli):
    assert "권한" in cli("text", "--help").out


def test_top_level_help_lists_commands(cli):
    text = cli("--help").out
    assert "search" in text and "text" in text


# ---------- text ----------
def parse_text(output):
    return [line.split("\t") for line in output.splitlines()]


def test_text_records_for_each_format(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "첫 문단", "둘째 문단")
    pdf(tmp_path / "b.pdf", "pdf page text", None)
    docx(tmp_path / "c.docx", "워드 문단")
    result = cli("text", ".", cwd=tmp_path)
    assert result.code == 0
    records = parse_text(result.out)
    assert ["a.hwpx", "", "첫 문단"] in records and ["a.hwpx", "", "둘째 문단"] in records
    assert ["b.pdf", "1", "pdf page text"] in [[a, b, c.strip()] for a, b, c in records]
    assert ["c.docx", "", "워드 문단"] in records
    report = json.loads(result.err.strip().splitlines()[-1])
    assert report["coverage"]["image_only_pages"]["files"] == 1


def test_text_xlsx_unit_is_cell_address(cli, tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.active.title = "표지"
    workbook.active["B2"] = "칸 글"
    workbook.save(tmp_path / "표.xlsx")
    assert ["표.xlsx", "1:표지!B2", "칸 글"] in parse_text(cli("text", ".", cwd=tmp_path).out)


def test_text_escapes_tabs_newlines_and_backslashes_in_path_and_text(cli, tmp_path):
    name = "탭\t줄\n바꿈\\경로.hwpx"
    b.write_hwpx(tmp_path / name, [b.hwpx_section("<hp:p><hp:run><hp:t>글\\자<hp:tab/>칸</hp:t></hp:run></hp:p>")])
    output = cli("text", ".", cwd=tmp_path).out
    assert output.count("\n") == 1 and output.count("\t") == 2
    path, unit, text = output.rstrip("\n").split("\t")
    assert path == "탭\\t줄\\n바꿈\\\\경로.hwpx" and unit == "" and text == "글\\\\자 칸"


def test_text_is_folded(cli, tmp_path):
    hwpx(tmp_path / "a.hwpx", "㈜회사")
    assert parse_text(cli("text", ".", cwd=tmp_path).out)[0][2] == "(주)회사"


def test_text_glob(cli, tmp_path):
    hwpx(tmp_path / "가" / "a.hwpx", "첫째")
    hwpx(tmp_path / "나" / "b.hwpx", "둘째")
    assert [r[2] for r in parse_text(cli("text", ".", "--glob", "나/*", cwd=tmp_path).out)] == ["둘째"]


def test_text_into_a_closed_pipe_ends_quietly(tmp_path, home):
    for number in range(30):
        hwpx(tmp_path / f"{number}.hwpx", "글 " + LONG)
    env = {**os.environ, "HOME": str(home)}
    process = subprocess.Popen([sys.executable, str(CLI), "text", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    process.stdout.readline()
    process.stdout.close()
    _, err = process.communicate(timeout=60)
    assert b"Traceback" not in err
    assert process.returncode in (0, 141)


# ---------- 중단 ----------
def test_interrupt_ends_with_130(tmp_path, home):
    for number in range(400):
        pdf(tmp_path / f"{number}.pdf", "text " + LONG)
    env = {**os.environ, "HOME": str(home)}
    process = subprocess.Popen([sys.executable, str(CLI), "search", "x", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    time.sleep(0.5)
    os.killpg(process.pid, signal.SIGINT)
    _, err = process.communicate(timeout=60)
    assert process.returncode == 130, err.decode(errors="replace")
    assert b"Traceback" not in err


# ---------- 실제 환경 ----------
def test_pep723_environment_runs_the_real_entry_point(tmp_path):
    hwpx(tmp_path / "a.hwpx", "찾는말 " + LONG)
    completed = subprocess.run(["uv", "run", str(CLI), "search", "찾는말", str(tmp_path)], capture_output=True, text=True, cwd=tmp_path, timeout=300)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["summary"]["documents_hit"] == 1


# ---------- 독립 검토에서 재현한 사례 ----------
def test_and_regex_matching_only_empty_is_not_met(cli, tmp_path):
    docx(tmp_path / "a.docx", "hello " + LONG)
    assert cli("search", "hello", tmp_path, "--regex", "--and", "q*").json["results"] == []


def test_page_marking_ignores_empty_regex_matches(cli, tmp_path, fake_rhwp):
    hwp(tmp_path / "a.hwp", "aaa " + LONG)
    entry = cli("search", "a*", tmp_path, "--regex", path_prefix=fake_rhwp, env={"FAKE_RHWP_PAGES": json.dumps(["bbb", "aaa"])}).json["results"][0]["files"][0]
    assert entry["pages"] == [2]


@pytest.mark.parametrize(
    "body, query, expected",
    [
        ("담합 담 합 담합", "담합", (2, 1)),  # 표기 둘, 변형 하나
        ("a aa", "aa", (1, 0)),  # 앞의 `a a`가 뒤의 표기 일치를 삼키면 안 된다
        ("aa aa", "aa", (2, 0)),
        ("a aa a", "aa", (1, 0)),  # 변형을 따로 세면 표기 일치 하나가 변형 하나로도 세진다
        ("a a", "aa", (0, 1)),
        ("aa a a aa", "aa", (2, 1)),
    ],
)
def test_exact_is_found_first_and_variants_are_counted_outside_it(cli, tmp_path, body, query, expected):
    docx(tmp_path / "a.docx", body + " " + LONG.replace("x", "z"))
    file = cli("search", query, tmp_path).json["results"][0]["files"][0]
    assert (file["exact"], file["spacing_variants"]) == expected


# ---------- 출력 크기: 스킬의 비용 대부분은 문맥에 쌓인 출력이다 ----------
def test_long_paths_are_not_repeated_in_a_result(cli, tmp_path):
    folder = tmp_path / ("긴폴더이름" * 8)
    hwpx(folder / "보도.hwpx", "찾는말 " + LONG)
    pdf(folder / "보도.pdf", "other " + LONG)
    hwp(folder / "보도.hwp", "찾는말 " + LONG)
    doc = cli("search", "찾는말", folder, cwd=tmp_path).json["results"][0]
    assert doc["document"] == "보도"
    assert all("path" not in snippet for snippet in doc["snippets"])
    assert doc["files"][doc["snippets"][0]["file"]]["kind"] in ("hwp", "hwpx")
    assert sum(json.dumps(doc, ensure_ascii=False).count("긴폴더이름" * 8) for _ in [0]) == 3  # 파일 셋의 경로에만


def test_snippet_points_to_its_file_even_with_two_of_the_same_kind(cli, tmp_path):
    # 이름이 공백만 달라 한 묶음이 된 같은 형식 둘(독립 검토 재현).
    docx(tmp_path / "same.docx", "needle alpha " + LONG)
    docx(tmp_path / "same .docx", "needle beta " + LONG)
    doc = cli("search", "needle", tmp_path).json["results"][0]
    assert len(doc["files"]) == 2 and len(doc["snippets"]) == 2
    for snippet in doc["snippets"]:
        word = "alpha" if "alpha" in snippet["text"] else "beta"
        expected = "same.docx" if word == "alpha" else "same .docx"
        assert os.path.basename(doc["files"][snippet["file"]]["path"]) == expected


def test_coverage_lists_only_on_the_first_page(cli, tmp_path):
    for number in range(4):
        hwpx(tmp_path / f"{number}.hwpx", "찾는말 " + LONG)
    hwp(tmp_path / "암호.hwp", "x", encrypted=True)
    first = cli("search", "찾는말", tmp_path, "--limit", "2").json
    second = cli("search", "찾는말", tmp_path, "--limit", "2", "--offset", "2").json
    assert len(first["coverage"]["unreadable"]["items"]) == 1
    assert second["coverage"]["unreadable"] == {"count": 1, "items": [], "more": 1}


def test_max_chars_lets_one_call_return_everything(cli, tmp_path):
    for number in range(30):
        hwpx(tmp_path / f"문서{number:02}.hwpx", ("찾는말 " + "가" * 300 + " ") * 3)
    capped = cli("search", "찾는말", tmp_path, "--limit", "30", "--context", "300", "--max-chars", "3000").json
    assert 0 < capped["summary"]["returned"] < 30
    everything = cli("search", "찾는말", tmp_path, "--limit", "30", "--context", "300", "--max-chars", "1000000").json
    assert everything["summary"]["returned"] == 30 and everything["summary"]["next_offset"] is None


def test_argument_errors_are_in_korean(cli, tmp_path):
    result = cli("search", "말", tmp_path, "--limit", "0")
    assert result.code == 2 and "argument" not in result.err
