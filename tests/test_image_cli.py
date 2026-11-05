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
    # 쪽을 pdf와 같은 해상도로 보려고 2배로 그린다(기본 794px로는 차트를 읽지 못해 스크립트를 다시 짠다).
    with Image.open(data["images"][0]["path"]) as image:
        assert image.size == (242, 160)


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


def test_default_folder_drops_only_its_own_day_old_pictures(cli, tmp_path):
    folder = tmp_path / ".documents/pages"
    folder.mkdir(parents=True)
    old = time.time() - 2 * 86400
    (old_picture := folder / "0123456789ab_p0001.png").write_bytes(b"x")
    (recent := folder / "0123456789ac_p0001.png").write_bytes(b"x")
    (note := folder / "메모.txt").write_bytes(b"x")
    (other := folder / "내그림.png").write_bytes(b"x")
    for path in (old_picture, note, other):
        os.utime(path, (old, old))
    assert cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", cwd=tmp_path).code == 0
    assert not old_picture.exists()
    assert recent.exists() and note.exists() and other.exists()


@pytest.mark.parametrize("linked", [".documents", ".documents/pages"])
def test_symlinked_default_folder_is_refused_and_nothing_is_deleted(cli, tmp_path, linked):
    # 작업 폴더 밖 파일을 지우거나 쓰면 안 된다(독립 검토 재현).
    outside = tmp_path / "바깥"
    (outside / "pages").mkdir(parents=True)
    old = time.time() - 2 * 86400
    valuable = outside / "0123456789ab_p0001.png"
    valuable.write_bytes(b"x")
    os.utime(valuable, (old, old))
    work = tmp_path / "작업"
    (work / ".documents").mkdir(parents=True)
    link = work / linked
    if linked == ".documents":
        (work / ".documents").rmdir()
        target = outside
    else:
        target = outside
    link.symlink_to(target, target_is_directory=True)
    result = cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", cwd=work)
    assert result.code == 3 and "--out-dir" in result.json["error"]["message"]
    assert valuable.exists()
    assert [p.name for p in outside.iterdir() if p.is_file()] == [valuable.name]


def test_symlink_in_the_folder_is_not_written_through(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    first = cli("image", path, "--pages", "1", cwd=tmp_path).json["images"][0]["path"]
    os.unlink(first)
    victim = tmp_path / "남의파일.txt"
    victim.write_text("지키기")
    os.symlink(victim, first)
    assert cli("image", path, "--pages", "1", cwd=tmp_path).code == 0
    assert victim.read_text() == "지키기"
    assert not os.path.islink(first)


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


def test_dangling_gitignore_link_is_not_written_through(cli, tmp_path):
    outside = tmp_path / "밖에생길파일"
    (tmp_path / "작업/.documents").mkdir(parents=True)
    (tmp_path / "작업/.documents/.gitignore").symlink_to(outside)
    assert cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", cwd=tmp_path / "작업").code == 0
    assert not outside.exists()


def test_hard_link_at_the_output_name_is_not_overwritten(cli, tmp_path):
    path = make_docx_with_media(tmp_path / "a.docx", image1__png=png_bytes())
    work = tmp_path / "작업"
    work.mkdir()
    made = cli("image", path, cwd=work).json["images"][0]["path"]
    os.unlink(made)
    victim = tmp_path / "남의파일.txt"
    victim.write_text("지키기")
    os.link(victim, made)
    assert cli("image", path, cwd=work).code == 0
    assert victim.read_text() == "지키기"
    with Image.open(made) as image:
        assert image.format == "PNG"


def test_no_partial_file_is_left_behind(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    cli("image", path, "--pages", "1", cwd=tmp_path)
    assert not [name for name in os.listdir(tmp_path / ".documents/pages") if name.endswith(".part")]


def rendered(cli, path, *args, cwd, **options):
    result = cli("image", path, *args, cwd=cwd, **options)
    assert result.code == 0, result.err
    return result.json["images"]


def test_pdf_crop_zooms_a_region_to_the_long_side(cli, tmp_path):
    path = pdf(tmp_path / "a.pdf", "one " + LONG)
    whole = rendered(cli, path, "--pages", "1", cwd=tmp_path)[0]
    half = rendered(cli, path, "--pages", "1", "--crop", "0,0,1,0.5", cwd=tmp_path)[0]
    assert half["path"] != whole["path"]
    # 612x792쪽의 위 절반은 612x396이라 가로가 긴 변이 된다.
    assert half["width"] == 1568 and abs(half["height"] - 1568 * 396 / 612) <= 2
    with Image.open(half["path"]) as image:
        assert image.size == (half["width"], half["height"])


def test_crop_keeps_top_left_origin(cli, tmp_path):
    # 합성 PDF의 글은 위쪽에 있고 아래쪽은 비어 있다. 원점이 뒤집히면 비어 있는 쪽을 자른다.
    path = pdf(tmp_path / "a.pdf", "dark text " * 8)
    top = rendered(cli, path, "--pages", "1", "--crop", "0,0,1,0.2", cwd=tmp_path)[0]
    bottom = rendered(cli, path, "--pages", "1", "--crop", "0,0.8,1,1", cwd=tmp_path)[0]
    with Image.open(top["path"]) as image:
        assert image.convert("L").getextrema()[0] < 128
    with Image.open(bottom["path"]) as image:
        assert image.convert("L").getextrema()[0] > 128


def test_hwp_crop_renders_larger_then_cuts(cli, tmp_path, fake_rhwp):
    path = hwp(tmp_path / "a.hwp", "x " + LONG)
    env = {"FAKE_RHWP_PAGES": json.dumps(["1"])}
    entry = rendered(cli, path, "--pages", "1", "--crop", "0,0,0.5,0.5", cwd=tmp_path, path_prefix=fake_rhwp, env=env)[0]
    # 가로세로 절반이므로 배율을 두 배(4배)로 올려 120x80쪽의 480x320에서 240x160을 자른다.
    assert (entry["width"], entry["height"]) == (240, 160)


@pytest.mark.parametrize("value", ["1,2,3", "a,b,c,d", "0.5,0,0.5,1", "0,0,1,1.5", "-0.1,0,1,1", "0.9,0,0.1,1", "0.5,0.5,0.5000000001,0.5000000001", "0.1,0.1,0.100001,0.5", "nan,0,1,1", "0,0,inf,1"])
def test_bad_crop_is_exit_2(cli, tmp_path, value):
    assert cli("image", pdf(tmp_path / "a.pdf", "one " + LONG), "--pages", "1", f"--crop={value}", cwd=tmp_path).code == 2


def test_smallest_allowed_crop_shows_the_page_content(cli, tmp_path):
    # 극단적인 배율에서 렌더러가 빈 그림을 내면 안 된다(독립 검토 재현). 쪽 전체가 빨간 PDF로 본다.
    path = b.write_pdf_stream(tmp_path / "빨강.pdf", b"1 0 0 rg 0 0 612 792 re f")
    entry = rendered(cli, path, "--pages", "1", "--crop", "0.5,0.5,0.51,0.51", cwd=tmp_path)[0]
    with Image.open(entry["path"]) as image:
        assert image.convert("RGB").getpixel((image.width // 2, image.height // 2)) == (255, 0, 0)


def test_image_help_states_the_zoom_limit(cli):
    assert "8배" in cli("image", "--help").out


def test_crop_on_embedded_formats_is_exit_2(cli, tmp_path):
    result = cli("image", make_docx_with_media(tmp_path / "a.docx", image1__png=png_bytes()), "--crop", "0,0,1,1", cwd=tmp_path)
    assert result.code == 2 and "pdf" in result.err
