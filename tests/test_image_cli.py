"""image의 계약. 만든 PNG를 Pillow로 다시 열어 본다."""
import json
import os
import stat
import struct
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from tests import builders as b
from tests.test_extract import make_pptx
from tests.test_search_cli import LONG, docx, hwp, hwpx, pdf


def png_bytes(size=(10, 10), kind="PNG"):
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size).save(buffer, kind)
    return buffer.getvalue()


def test_pdf_page_is_rendered_with_long_side_1568(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG, "two " + LONG)
    work = tmp_path / "작업"
    work.mkdir()
    result = cli("image", path, "--pages", "2", cwd=work)
    assert result.code == 0, result.err
    data = result.json
    assert data["kind"] == "pdf" and data["source"] == "render" and data["page_basis"] == "physical"
    entry = data["images"][0]
    assert entry["page"] == 2 and os.path.isabs(entry["path"]) and entry["path"].endswith("_p0002.png")
    with Image.open(entry["path"]) as image:
        assert max(image.size) == 1568 and image.size == (entry["width"], entry["height"])
    assert Path(entry["path"]).parent == (work / ".documents/pages").resolve()
    assert (work / ".documents/.gitignore").read_text().strip() == "*"


def test_pdf_requires_pages(cli, tmp_path):
    assert cli("image", pdf(tmp_path / "a.pdf", "one " + LONG)).code == 2


def test_out_of_range_says_the_count(cli, tmp_path):
    result = cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "5")
    assert result.code == 2 and "1쪽" in result.err


def test_several_pages_get_unique_paths(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG, "two " + LONG, "three " + LONG)
    images = cli("image", path, "--pages", "1,3", cwd=tmp_path).json["images"]
    assert [i["page"] for i in images] == [1, 3] and len({i["path"] for i in images}) == 2


def test_hwp_page_goes_through_rhwp(cli, tmp_path, fake_rhwp):
    path = hwp(tmp_path / "a.hwp", "x " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["1", "2", "3"])}
    data = cli("image", path, "--pages", "2", cwd=tmp_path, path_prefix=fake_rhwp, env=env).json
    assert data["kind"] == "hwp" and data["source"] == "render" and data["page_basis"] == "estimated"
    with Image.open(data["images"][0]["path"]) as image:
        assert image.size == (121, 80)


def test_hwp_without_rhwp_is_exit_4(cli, tmp_path):
    result = cli("image", hwp(tmp_path / "a.hwp", "x " + LONG), "--pages", "1", cwd=tmp_path)
    assert result.code == 4 and "doctor --fix" in result.err


def test_long_file_name_goes_through_a_short_link(cli, tmp_path, fake_rhwp):
    path = hwpx(tmp_path / ("가" * 78 + ".hwpx"), "x " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["1"]), "FAKE_RHWP_MAX_NAME": "32"}
    result = cli("image", path, "--pages", "1", cwd=tmp_path, path_prefix=fake_rhwp, env=env)
    assert result.code == 0, result.err
    assert os.path.exists(result.json["images"][0]["path"])


# 실제 EMF 머리의 모양이다. Pillow가 형식은 알아보지만 윈도 밖에서는 그리지 못한다.
EMF_HEADER = bytes.fromhex("01000000") + b"\x6c\0\0\0" + struct.pack("<4i", 0, 0, 100, 100) + struct.pack("<4i", 0, 0, 2000, 2000) + b" EMF" + b"\0" * 64


def make_docx_with_media(path, **media):
    members = b.docx_members("<w:p><w:r><w:t>글</w:t></w:r></w:p>")
    members.update({f"word/media/{name.replace('__', '.')}": data for name, data in media.items()})
    return b.write_zip(path, members)


def test_docx_media_are_extracted_with_unique_paths_and_bmp_becomes_png(cli, tmp_path):
    path = make_docx_with_media(tmp_path / "a.docx", image1__png=png_bytes(), image2__jpeg=png_bytes(kind="JPEG"), image3__bmp=png_bytes(kind="BMP"), image4__emf=EMF_HEADER, image5__png=b"broken")
    data = cli("image", path, cwd=tmp_path).json
    assert data["kind"] == "docx" and data["source"] == "embedded" and data["page_basis"] == "none"
    names = {entry["source_name"]: entry for entry in data["images"]}
    assert set(names) == {"image1.png", "image2.jpeg", "image3.bmp"}
    assert all(entry["page"] is None for entry in names.values())
    assert names["image3.bmp"]["path"].endswith(".png")
    with Image.open(names["image3.bmp"]["path"]) as image:
        assert image.format == "PNG" and image.size == (10, 10) == (names["image3.bmp"]["width"], names["image3.bmp"]["height"])
    assert len({entry["path"] for entry in data["images"]}) == 3
    skipped = {entry["source_name"] for entry in data["skipped"]}
    assert skipped == {"image4.emf", "image5.png"}


def test_docx_refuses_pages(cli, tmp_path):
    assert cli("image", make_docx_with_media(tmp_path / "a.docx", image1__png=png_bytes()), "--pages", "1").code == 2


def test_xlsx_media(cli, tmp_path):
    path = b.write_zip(tmp_path / "a.xlsx", {"xl/workbook.xml": "<workbook/>", "xl/media/image1.png": png_bytes()})
    data = cli("image", path, cwd=tmp_path).json
    assert data["kind"] == "xlsx" and data["page_basis"] == "none" and data["images"][0]["source_name"] == "image1.png"


def make_pptx_with_pictures(path):
    import io

    import pptx
    from pptx.util import Inches

    presentation = pptx.Presentation()
    for colour_size in [(11, 11), (22, 22), (33, 33)]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.add_picture(io.BytesIO(png_bytes(colour_size)), Inches(1), Inches(1))
    ids = presentation.slides._sldIdLst
    items = list(ids)
    for element in items:
        ids.remove(element)
    for position in [2, 0, 1]:
        ids.append(items[position])
    presentation.save(path)
    return path


def test_pptx_images_follow_display_order_and_pages_select(cli, tmp_path):
    path = make_pptx_with_pictures(tmp_path / "a.pptx")
    data = cli("image", path, cwd=tmp_path).json
    assert data["kind"] == "pptx" and data["source"] == "embedded" and data["page_basis"] == "slide"
    assert [(entry["page"], entry["width"]) for entry in data["images"]] == [(1, 33), (2, 11), (3, 22)]
    second = cli("image", path, "--pages", "2", cwd=tmp_path).json["images"]
    assert [(entry["page"], entry["width"]) for entry in second] == [(2, 11)]
    assert cli("image", path, "--pages", "9", cwd=tmp_path).code == 2


def test_media_name_cannot_escape_the_folder(cli, tmp_path):
    work = tmp_path / "작업"
    work.mkdir()
    members = b.docx_members("<w:p/>")
    members["word/media/../../../탈출.png"] = png_bytes()
    path = b.write_zip(tmp_path / "a.docx", members)
    data = cli("image", path, cwd=work).json
    assert [entry["source_name"] for entry in data["images"]] == ["탈출.png"]
    for entry in data["images"]:
        assert Path(entry["path"]).resolve().is_relative_to((work / ".documents").resolve())
    assert not (tmp_path / "탈출.png").exists()


def test_out_dir_is_used_and_old_files_are_kept(cli, tmp_path):
    out = tmp_path / "그림"
    out.mkdir()
    mine = out / "사용자의 옛 파일.png"
    mine.write_bytes(b"x")
    old = time.time() - 3 * 86400
    os.utime(mine, (old, old))
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    work = tmp_path / "작업"
    work.mkdir()
    data = cli("image", path, "--pages", "1", "--out-dir", out, cwd=work).json
    assert Path(data["images"][0]["path"]).parent == out.resolve()
    assert mine.exists()
    assert not (work / ".documents").exists() and not (out / ".gitignore").exists()


def test_default_folder_drops_day_old_pictures_only(cli, tmp_path):
    folder = tmp_path / ".documents/pages"
    folder.mkdir(parents=True)
    (old_file := folder / "옛그림.png").write_bytes(b"x")
    (recent := folder / "새그림.png").write_bytes(b"x")
    old = time.time() - 2 * 86400
    os.utime(old_file, (old, old))
    cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", cwd=tmp_path)
    assert not old_file.exists() and recent.exists()


def test_existing_gitignore_is_left_alone(cli, tmp_path):
    (tmp_path / ".documents").mkdir()
    (ignore := tmp_path / ".documents/.gitignore").write_text("내 규칙\n")
    assert cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", cwd=tmp_path).code == 0
    assert ignore.read_text() == "내 규칙\n"


def test_nothing_is_created_on_failure(cli, tmp_path):
    cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "9", cwd=tmp_path)
    assert not (tmp_path / ".documents").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한을 무시한다")
def test_unwritable_work_folder_is_exit_3_and_mentions_out_dir(cli, tmp_path):
    work = tmp_path / "잠김"
    work.mkdir()
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    work.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = cli("image", path, "--pages", "1", cwd=work)
    finally:
        work.chmod(stat.S_IRWXU)
    assert result.code == 3 and "--out-dir" in result.json["error"]["message"]


def test_exit_codes(cli, tmp_path):
    assert cli("image", tmp_path / "없다.pdf", "--pages", "1").code == 2
    (tmp_path / "메모.md").write_text("글")
    assert cli("image", tmp_path / "메모.md").code == 2
    encrypted = Path(__file__).parent / "fixtures/encrypted.pdf"
    result = cli("image", encrypted, "--pages", "1", cwd=tmp_path)
    assert result.code == 3 and result.json["error"]["code"] == "encrypted"


def test_source_is_untouched(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    before = (path.stat().st_mtime_ns, path.read_bytes())
    cli("image", path, "--pages", "1", cwd=tmp_path)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_image_help_states_the_contract(cli):
    text = cli("image", "--help").out
    for word in ["--out-dir", ".documents/pages", "render", "embedded", "Read", "종료 코드", "권한"]:
        assert word in text, word


def test_docx_with_no_media_is_an_empty_list(cli, tmp_path):
    data = cli("image", docx(tmp_path / "a.docx", "글 " + LONG), cwd=tmp_path).json
    assert data["images"] == [] and data["skipped"] == []
