"""진짜 rhwp·kordoc으로 여섯 형식을 실제로 읽는다. `uv run --group test pytest -q -m slow`."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tests import builders as b
from tests.conftest import CLI
from tests.test_extract import make_pptx, make_xlsx

pytestmark = pytest.mark.slow
FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def real(*args, cwd=None, env=None, timeout=300):
    completed = subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True, cwd=cwd, env={**os.environ, **(env or {})}, timeout=timeout)
    return completed


def split(completed):
    assert completed.returncode == 0, completed.stderr
    head, _, body = completed.stdout.partition("\n")
    return json.loads(head), body


def test_doctor_says_ok_here():
    data = json.loads(real("doctor").stdout)
    assert data["rhwp"]["ok"] and data["kordoc"]["ok"] and data["node"]["ok"]


def test_real_hwp_whole_and_pages():
    head, body = split(real("read", FIXTURES / "equation_real.hwp"))
    assert head["kind"] == "hwp" and head["page_basis"] == "estimated" and head["page_count"] == 6
    assert "유강종합건설" in body and "![" not in body and "**" not in body
    head, body = split(real("read", FIXTURES / "equation_real.hwp", "--pages", "2"))
    assert body.startswith("[p.2]\n") and head["pages_returned"] == [2]


def test_real_hwpx():
    # 합성 HWPX는 rhwp·kordoc이 받아들이는 전체 형식이 아니라 실물로 본다.
    real_files = sorted(DOCS.glob("공정거래위원회/250722_유강*/*.hwpx"))
    if not real_files:
        pytest.skip("docs가 없다")
    head, body = split(real("read", real_files[0], "--pages", "1"))
    assert head["kind"] == "hwpx" and body.startswith("[p.1]\n") and "유강종합건설" in body
    head, body = split(real("read", real_files[0]))
    assert head["page_count"] and "유강종합건설" in body and "[p." not in body


def test_real_pdf_pages_and_no_tables():
    head, body = split(real("read", FIXTURES / "two_pages_real.pdf"))
    assert head["page_count"] == 2 and "[p.1]\n" in body and "[p.2]\n" in body
    head, body = split(real("read", FIXTURES / "two_pages_real.pdf", "--pages", "2", "--no-tables"))
    assert body.startswith("[p.2]\n") and "실질적인 기업지원 혜택" in " ".join(body.split())


def test_real_docx_math_with_python_docx(tmp_path):
    import docx
    from docx.oxml import parse_xml

    document = docx.Document()
    document.add_paragraph("워드 본문 글")
    math = parse_xml(f'<w:p xmlns:w="{b.W}" xmlns:m="{b.M}"><m:oMath><m:r><m:t>수식안말</m:t></m:r></m:oMath></w:p>')
    document.element.body.insert(0, math)
    path = tmp_path / "수식.docx"
    document.save(path)
    head, body = split(real("read", path))
    assert head["kind"] == "docx" and "워드 본문 글" in body and "수식안말" in body
    found = json.loads(real("search", "수식안말", path).stdout)
    assert found["results"][0]["files"][0]["exact"] == 1


def test_real_pptx_reordered_read_and_search_agree(tmp_path):
    path = make_pptx(tmp_path / "발표.pptx", ["첫 파일 슬라이드", "둘째 파일 슬라이드", "셋째 파일 슬라이드"], order=[2, 0, 1])
    head, body = split(real("read", path, "--pages", "1"))
    assert head["page_basis"] == "slide" and "셋째 파일" in body
    assert json.loads(real("search", "셋째 파일", path).stdout)["results"][0]["files"][0]["pages"] == [1]


def test_real_xlsx(tmp_path):
    head, body = split(real("read", make_xlsx(tmp_path / "표.xlsx")))
    assert head["kind"] == "xlsx" and "쿠팡 변동" in body


def test_real_image_of_hwp_page(tmp_path):
    data = json.loads(real("image", FIXTURES / "equation_real.hwp", "--pages", "1", cwd=tmp_path).stdout)
    entry = data["images"][0]
    assert entry["page"] == 1 and data["source"] == "render" and data["page_basis"] == "estimated"
    with Image.open(entry["path"]) as image:
        assert image.size == (entry["width"], entry["height"]) and max(image.size) >= 500


def test_real_long_name_image(tmp_path):
    # 83자는 249바이트다. rhwp의 출력 이름(`…_001.md`)이 파일 이름 한도 255바이트를 넘는다.
    long_name = tmp_path / ("가" * 83 + ".hwp")
    shutil.copy(FIXTURES / "equation_real.hwp", long_name)
    data = json.loads(real("image", long_name, "--pages", "1", cwd=tmp_path).stdout)
    assert os.path.exists(data["images"][0]["path"])
    head, _ = split(real("read", long_name, "--pages", "1"))
    assert head["path"] == str(long_name)


def test_real_timeout_is_not_hit_by_ordinary_read():
    assert real("read", FIXTURES / "two_pages_real.pdf", "--pages", "1", timeout=60).returncode == 0


def test_real_kordoc_read_of_encrypted_is_exit_3():
    completed = real("read", FIXTURES / "encrypted.pdf")
    assert completed.returncode == 3 and json.loads(completed.stdout)["error"]["code"] == "encrypted"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm이 없다")
def test_fix_recovers_an_empty_environment(tmp_path):
    home = tmp_path / "빈환경"
    home.mkdir()
    env = {"HOME": str(home), "npm_config_cache": str(tmp_path / "npm")}
    fixed = real("doctor", "--fix", env=env, timeout=900)
    assert fixed.returncode == 0, fixed.stderr
    data = json.loads(fixed.stdout)
    assert "kordoc" in data["fixed"] and data["kordoc"]["ok"] and data["rhwp"]["ok"]
    assert str(home) in data["kordoc"]["path"]
    # 이미 PATH의 rhwp가 맞는 판이면 내려받지 않는다. PATH를 비워 다운로드 경로도 본다.
    env_no_rhwp = {**env, "PATH": ":".join(p for p in os.environ["PATH"].split(":") if not os.path.exists(os.path.join(p, "rhwp")))}
    fixed = real("doctor", "--fix", env=env_no_rhwp, timeout=300)
    assert fixed.returncode == 0, fixed.stderr
    data = json.loads(fixed.stdout)
    assert data["fixed"] == ["rhwp"] and data["rhwp"]["path"] == str(home / ".cache/documents/bin/rhwp")
    assert data["rhwp"]["ok"]
    head, body = split(real("read", FIXTURES / "equation_real.hwp", "--pages", "1", env=env_no_rhwp))
    assert body.startswith("[p.1]\n")
