"""스킬 코드 트리의 약속을 지킨다. 구조를 일부러 바꾸면 이 시험도 함께 바꾼다."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude/skills/documents/scripts"
PACKAGE = SCRIPTS / "documents"

UNITS = {"hancom", "pdf", "ooxml", "rhwp", "kordoc", "extract", "scan", "search", "reading", "images", "health", "normalize"}

# 계획의 허용 간선. 시스템 유닛(hancom, pdf, ooxml, rhwp, kordoc)은 다른 유닛을 부르지 않는다.
ALLOWED = {
    "search": {"scan", "rhwp", "normalize"},
    "reading": {"extract", "kordoc", "rhwp", "pdf", "ooxml", "normalize"},
    "images": {"extract", "pdf", "rhwp", "ooxml", "kordoc"},
    "health": {"rhwp", "kordoc"},
    "scan": {"extract", "normalize"},
    "extract": {"hancom", "pdf", "ooxml"},
    "normalize": set(),
    "hancom": set(),
    "pdf": set(),
    "ooxml": set(),
    "rhwp": set(),
    "kordoc": set(),
}
CLI_ALLOWED = {"search", "reading", "images", "health", "scan"}


def unit_of(path: Path) -> str:
    return path.relative_to(PACKAGE).parts[0].removesuffix(".py")


def _module_exists(package: Path, parts) -> bool:
    target = package.joinpath(*parts)
    return target.is_dir() or target.with_suffix(".py").exists()


def imports_in(source: str, package_parts=(), package: Path = PACKAGE):
    """소스의 모든 import를 패키지 안 경로로 돌려준다. 함수 안 import도, `from x import 하위모듈`의 이름도 본다."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "documents":
                    yield tuple(parts[1:])
        elif isinstance(node, ast.ImportFrom):
            base = list(package_parts)[: len(package_parts) - node.level + 1] if node.level else []
            parts = base + (node.module.split(".") if node.module else [])
            if not parts or parts[0] != "documents":
                continue
            inner = tuple(parts[1:])
            for alias in node.names:
                if inner and not _module_exists(package, (*inner, alias.name)):
                    yield inner  # 유닛에서 이름을 가져온다
                else:
                    yield (*inner, alias.name)


def imports_of(path: Path):
    return imports_in(path.read_text(encoding="utf-8"), path.relative_to(SCRIPTS).parts[:-1])


def python_files():
    return sorted(SCRIPTS.rglob("*.py"))


def test_top_level_names_are_fixed():
    names = {p.name for p in SCRIPTS.iterdir() if p.name != "__pycache__"}
    assert names == {"cli.py", "documents"}


def test_units_exist():
    found = {unit_of(p) for p in PACKAGE.rglob("*.py") if p.name != "__init__.py" or p.parent != PACKAGE}
    assert found == UNITS


def test_import_edges_are_the_agreed_ones():
    for path in PACKAGE.rglob("*.py"):
        if path == PACKAGE / "__init__.py":
            continue
        unit = unit_of(path)
        for target in imports_of(path):
            if target and target[0] != unit:
                assert target[0] in ALLOWED[unit], f"{path}: {unit} -> {target[0]}"


def test_cli_only_calls_feature_units():
    for target in imports_of(SCRIPTS / "cli.py"):
        assert target and target[0] in CLI_ALLOWED, f"cli.py -> {target}"


def test_nothing_reaches_past_a_unit_interface():
    """하위 패키지의 안쪽 모듈은 그 패키지 바깥에서 부르지 못한다."""
    for path in [SCRIPTS / "cli.py", *PACKAGE.rglob("*.py")]:
        unit = unit_of(path) if path.is_relative_to(PACKAGE) and path != PACKAGE / "__init__.py" else None
        for target in imports_of(path):
            if target and target[0] != unit:
                assert len(target) == 1, f"{path}: {target}"
    for path in Path(__file__).parent.rglob("*.py"):
        for target in imports_in(path.read_text(encoding="utf-8")):
            assert len(target) <= 1, f"{path}: documents.{'.'.join(target)}"


def test_nothing_edits_sys_path():
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "path":
                value = node.value
                if isinstance(value, ast.Name) and value.id == "sys":
                    raise AssertionError(f"{path}: sys.path를 건드린다")


def test_cli_declares_its_own_environment():
    head = (SCRIPTS / "cli.py").read_text(encoding="utf-8").split("\n", 30)
    text = "\n".join(head)
    assert "# /// script" in text and "requires-python" in text
    for dep in ("olefile", "pypdfium2", "pebble", "pillow", "openpyxl"):
        assert dep in text


def test_cli_has_no_heavy_import_at_module_level():
    """spawn이 main을 다시 import하므로 cli.py 맨 위에서 무거운 유닛을 부르면 작업자마다 비용이 든다."""
    tree = ast.parse((SCRIPTS / "cli.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any(n.split(".")[0] == "documents" for n in names)


def test_the_checker_sees_every_import_form(tmp_path):
    # 하위 모듈이 생겼을 때를 대비한 검사기의 시험이다. 가짜 패키지에 비추어 모양을 판별한다.
    package = tmp_path / "documents"
    (package / "hancom").mkdir(parents=True)
    (package / "hancom" / "probe.py").write_text("", encoding="utf-8")
    seen = lambda source: list(imports_in(source, package=package))  # noqa: E731
    assert seen("from documents.hancom import probe") == [("hancom", "probe")]
    assert seen("from documents.hancom.probe import x") == [("hancom", "probe")]
    assert seen("import documents.hancom.probe") == [("hancom", "probe")]
    assert seen("from documents import hancom") == [("hancom",)]
    assert seen("from documents.hancom import hwp_text") == [("hancom",)]
