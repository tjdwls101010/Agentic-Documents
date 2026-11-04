"""scan의 약속: 탐색 규칙, 묶음 키, 파일 하나가 전체를 멈추지 못하는 격리 실행."""
import os
import sys
import unicodedata
from pathlib import Path

import pytest

from documents import scan
from tests import builders as b
from tests import workers


def rels(discovery):
    return sorted(target.relative for target in discovery.targets)


def touch(path: Path, data: bytes = b"%PDF-1.4\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_document_extensions_are_targets_and_known_others_are_not_read(tmp_path):
    for name in ["a.hwp", "b.HWPX", "c.pdf", "d.docx", "e.pptx", "f.xlsx", "g.hwtx", "h.xlsm"]:
        touch(tmp_path / name)
    # 내용이 PDF라도 알려진 비대상은 열지 않는다.
    for name in ["x.zip", "y.jpg", "z.md", "w.doc", "v.xls"]:
        touch(tmp_path / name)
    found = scan.discover([str(tmp_path)])
    assert rels(found) == ["a.hwp", "b.HWPX", "c.pdf", "d.docx", "e.pptx", "f.xlsx", "g.hwtx", "h.xlsm"]
    assert found.not_read == {"zip": 1, "jpg": 1, "md": 1, "doc": 1, "xls": 1}


def test_unknown_extension_is_decided_by_content(tmp_path):
    touch(tmp_path / "잘린.pd", b"%PDF-1.4\n")
    touch(tmp_path / "잘린.hw", bytes.fromhex("D0CF11E0A1B11AE1"))
    touch(tmp_path / "확장자없음", b"PK\x03\x04rest")
    touch(tmp_path / "메모.xyz", b"just text")
    touch(tmp_path / "빈이름", b"plain")
    found = scan.discover([str(tmp_path)])
    assert rels(found) == ["잘린.hw", "잘린.pd", "확장자없음"]
    assert found.not_read == {"xyz": 1, "(없음)": 1}


def test_fifo_is_not_opened(tmp_path):
    os.mkfifo(tmp_path / "파이프.xyz")
    found = scan.discover([str(tmp_path)])  # 열면 여기서 영원히 멈춘다
    assert found.targets == []


def test_hidden_entries_are_skipped_inside_folders_but_opened_when_named(tmp_path):
    touch(tmp_path / ".숨김.pdf")
    touch(tmp_path / ".숨김폴더" / "안.pdf")
    touch(tmp_path / ".documents" / "pages" / "x.pdf")
    touch(tmp_path / "보임.pdf")
    assert rels(scan.discover([str(tmp_path)])) == ["보임.pdf"]
    assert rels(scan.discover([str(tmp_path / ".숨김.pdf")])) == [".숨김.pdf"]
    assert rels(scan.discover([str(tmp_path / ".숨김폴더")])) == ["안.pdf"]


def test_symlink_cycle_duplicates_outside_and_broken(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    touch(root / "b" / "진짜.pdf")
    touch(outside / "밖.pdf")
    (root / "순환").symlink_to(root, target_is_directory=True)
    (root / "a_같은파일.pdf").symlink_to(root / "b" / "진짜.pdf")
    (root / "밖으로").symlink_to(outside, target_is_directory=True)
    (root / "깨진.pdf").symlink_to(root / "없다.pdf")
    found = scan.discover([str(root)])
    assert rels(found) == ["a_같은파일.pdf", "밖으로/밖.pdf"]
    assert [os.path.basename(path) for path in found.broken] == ["깨진.pdf"]


def test_same_file_through_two_arguments_is_read_once(tmp_path):
    touch(tmp_path / "sub" / "a.pdf")
    found = scan.discover([str(tmp_path), str(tmp_path / "sub"), str(tmp_path / "sub" / "a.pdf")])
    assert len(found.targets) == 1


def test_missing_argument_is_refused(tmp_path):
    with pytest.raises(scan.MissingPath):
        scan.discover([str(tmp_path / "없다")])


def test_glob_matches_nfc_relative_path_and_star_crosses_slash(tmp_path):
    nfd = unicodedata.normalize("NFD", "공정위")
    touch(tmp_path / nfd / "깊이" / "보도.pdf")
    touch(tmp_path / "다른" / "보도.pdf")
    touch(tmp_path / nfd / "메모.txt")
    found = scan.discover([str(tmp_path)], glob="공정위*")
    assert [unicodedata.normalize("NFC", rel) for rel in rels(found)] == ["공정위/깊이/보도.pdf"]
    assert found.not_read == {"txt": 1}
    # 출력 경로는 디스크 그대로다.
    assert nfd in found.targets[0].path


def test_paths_keep_the_form_the_caller_gave(tmp_path, monkeypatch):
    touch(tmp_path / "폴더" / "a.pdf")
    monkeypatch.chdir(tmp_path)
    assert [t.path for t in scan.discover([]).targets] == ["폴더/a.pdf"]
    assert [t.path for t in scan.discover(["폴더"]).targets] == ["폴더/a.pdf"]


def test_document_key_groups_same_stem_in_same_folder_only():
    key = scan.document_key
    assert key("f/이름.hwp") == key("f/이름.pdf")
    assert key("f/이름  둘.hwp") == key("f/이름 둘.pdf")
    assert key("f/합니다..pdf") == key("f/합니다.hwp")
    assert key(unicodedata.normalize("NFD", "f/이름.hwp")) == key("f/이름.pdf")
    assert key("f/이름.hwp") != key("g/이름.pdf")
    assert key("f/이름.hwp") != key("f/이름2.hwp")


def outcomes(fn, items, **options):
    return {o.item: (o.value, o.failure) for o in scan.run_isolated(fn, items, **options)}


def test_all_items_come_back():
    items = [f"item{n}" for n in range(30)]
    result = outcomes(workers.echo, items)
    assert result == {item: (item.upper(), None) for item in items}


def test_crash_does_not_stop_the_rest():
    result = outcomes(workers.echo, ["a", "crash", "b", "crash2", "c"])
    assert result["crash"] == (None, "crashed")
    assert result["a"] == ("A", None) and result["b"] == ("B", None) and result["c"] == ("C", None)


def test_hang_times_out_and_the_rest_finishes():
    result = outcomes(workers.echo, ["hang", "a", "b"], timeout=2)
    assert result["hang"] == (None, "timeout")
    assert result["a"] == ("A", None) and result["b"] == ("B", None)


def test_worker_exception_is_reported_not_raised():
    assert outcomes(workers.echo, ["raise", "a"])["raise"] == (None, "crashed")


def test_empty_input_starts_nothing():
    assert list(scan.run_isolated(workers.echo, [])) == []


def test_children_do_not_inherit_the_parent_state(monkeypatch):
    # fork로 바꾸면 빨강이 된다. macOS에서 fork 자식은 objc 런타임에서 죽고 Pool은 영원히 기다린다.
    monkeypatch.setattr(workers, "INHERITED_FROM_PARENT", True)
    assert outcomes(workers.inherited, ["x"])["x"] == (False, None)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 시스템 프레임워크 경로")
def test_system_framework_is_usable_in_workers():
    assert outcomes(workers.objc_runtime, ["x", "y", "z"], timeout=30) == {k: ("ok", None) for k in "xyz"}


def test_coverage_summary_lists_and_caps(tmp_path):
    coverage = scan.Coverage(item_limit=2)
    coverage.add_failure("c.pdf", "timeout")
    coverage.add_failure("a.pdf", "encrypted")
    coverage.add_failure("b.pdf", "corrupt")
    coverage.add_facts(scan.FileFacts("t.pdf", no_text=True, image_only_pages=[1, 2], embedded_images=0))
    coverage.add_facts(scan.FileFacts("u.hwp", no_text=False, image_only_pages=[], embedded_images=3))
    coverage.not_read.update({"zip": 2})
    summary = coverage.summary()
    assert summary["unreadable"]["count"] == 3 and summary["unreadable"]["more"] == 1
    assert summary["unreadable"]["items"] == [{"path": "a.pdf", "reason": "encrypted"}, {"path": "b.pdf", "reason": "corrupt"}]
    assert summary["no_text"] == {"count": 1, "items": ["t.pdf"], "more": 0}
    assert summary["image_only_pages"] == {"files": 1, "pages": 2, "items": [{"path": "t.pdf", "pages": [1, 2]}], "more": 0}
    assert summary["embedded_images"] == {"files": 1, "images": 3}
    assert summary["not_read"] == {"zip": 2}


def test_facts_from_extracted(tmp_path):
    long_text = "Hello searchable pdf text body and it goes on past fifty letters here"
    path = b.write_pdf(tmp_path / "a.pdf", [long_text, None])
    from documents import extract

    facts = scan.facts_of(str(path), extract.extract(path))
    assert facts.image_only_pages == [2] and facts.no_text is False
    short = b.write_pdf(tmp_path / "b.pdf", ["short"])
    assert scan.facts_of(str(short), extract.extract(short)).no_text is True
