"""대조, 순위, 스니펫, 쪽, 범위 보고."""
import json
import os
import re
import time
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache

from . import normalize, rhwp, scan

RESULTS_CHAR_LIMIT = 20000
CELL_LIMIT = 10
FOLDER_LIMIT = 20
_PAGE_TIMEOUT_MARGIN = 5.0
_SPACES = re.compile(r"\s+")
_DOCTOR_NOTE = "hwp·hwpx 쪽을 정하지 못한 파일이 있다(pages가 null). `doctor --fix`로 rhwp를 설치하거나 상태를 확인한다"


class UsageError(Exception):
    """종료 코드 2로 끝낼 인자 오류."""


@dataclass(frozen=True)
class Query:
    text: str
    ands: tuple = ()
    regex: bool = False
    snippets: int = 3
    context: int = 40


@lru_cache(maxsize=8)
def _patterns(text: str, is_regex: bool, line_wrapped: bool):
    """(exact, loose)를 준다. 정규식은 두 자리가 같다."""
    if is_regex:
        compiled = normalize.regex(text)
        return compiled, compiled
    return normalize.patterns(text, line_wrapped=line_wrapped)


def validate(query: Query) -> None:
    for text in (query.text, *query.ands):
        try:
            _patterns(text, query.regex, False)
        except re.error as error:
            raise UsageError(f"정규식 오류: {text}: {error}") from error
        except ValueError as error:
            raise UsageError(str(error)) from error


def _spans(pattern, text) -> list:
    return [match.span() for match in pattern.finditer(text) if match.end() > match.start()]


def _snippet(text: str, span: tuple, context: int) -> str:
    start, end = span
    return _SPACES.sub(" ", text[max(0, start - context) : end + context]).strip()


def _has_match(pattern, text: str) -> bool:
    """길이 0인 일치(`z*`)는 일치로 치지 않는다."""
    return any(match.end() > match.start() for match in pattern.finditer(text))


def _variant_spans(pattern, text: str, exact_spans: list) -> list:
    """표기 일치 바깥에서 변형을 찾는다. 따로 세면 겹치는 자리에서 표기 일치 하나가 변형으로도 세진다."""
    spans = []
    position = 0
    for start, end in [*exact_spans, (len(text), len(text))]:
        spans.extend(match.span() for match in pattern.finditer(text, position, start) if match.end() > match.start())
        position = end
    return spans


def _search_file(path: str, extracted, query: Query) -> dict:
    """작업자 안에서 돈다. 본문 대신 일치 수와 스니펫 후보만 돌려준다."""
    exact_pattern, loose_pattern = _patterns(query.text, query.regex, extracted.line_wrapped)
    and_patterns = [_patterns(text, query.regex, extracted.line_wrapped)[1] for text in query.ands]
    unmet = list(and_patterns)
    exact_total = variant_total = 0
    units = []
    exact_snippets = []
    variant_snippets = []
    for unit, raw in extracted.parts:
        folded = normalize.fold(raw)
        if unmet:
            unmet = [pattern for pattern in unmet if not _has_match(pattern, folded)]
        exact_spans = _spans(exact_pattern, folded)
        variant_spans = [] if query.regex else _variant_spans(loose_pattern, folded, exact_spans)
        if not exact_spans and not variant_spans:
            continue
        exact_total += len(exact_spans)
        variant_total += len(variant_spans)
        units.append(unit)
        for span in exact_spans[: query.snippets - len(exact_snippets)]:
            exact_snippets.append({"page": unit, "exact": True, "text": _snippet(folded, span, query.context)})
        for span in variant_spans[: query.snippets - len(variant_snippets)]:
            variant_snippets.append({"page": unit, "exact": False, "text": _snippet(folded, span, query.context)})
    hit = bool(units) and not unmet
    return {
        "kind": extracted.kind,
        "hit": hit,
        "exact": exact_total if hit else 0,
        "spacing_variants": variant_total if hit else 0,
        "units": units if hit else [],
        "snippets": (exact_snippets + variant_snippets) if hit else [],
    }


def _matching_pages(path: str, query: Query, binary: str) -> list:
    """작업자 안에서 rhwp 쪽 글에 주 질의를 대조한다. 줄바꿈은 조판 줄이므로 넘는다. --and의 말은 쪽 표시에 쓰지 않는다."""
    pattern = _patterns(query.text, query.regex, True)[1]
    return [number for number, text in enumerate(rhwp.page_texts(path, binary), 1) if _has_match(pattern, normalize.fold(text))]


def _page_job(path: str, query: Query, binary: str):
    try:
        return _matching_pages(path, query, binary)
    except rhwp.ToolError:
        return None


_PAGE_BASIS = {"pdf": "physical", "pptx": "slide", "xlsx": "sheet", "docx": "none", "hwp": "estimated", "hwpx": "estimated"}


def _file_entry(path: str, found: dict) -> dict:
    entry = {"path": path, "kind": found["kind"], "exact": found["exact"], "spacing_variants": found["spacing_variants"]}
    kind = found["kind"]
    if kind == "xlsx":
        entry["cells"] = found["units"][:CELL_LIMIT]
        entry["cells_more"] = max(0, len(found["units"]) - CELL_LIMIT)
    elif kind in ("pdf", "pptx"):
        entry["pages"] = found["units"]
    else:
        entry["pages"] = None
    entry["page_basis"] = _PAGE_BASIS[kind]
    return entry


def _merge_snippets(files, found_by_path, limit: int) -> list:
    """exact를 먼저 두고 묶음 안의 같은 글은 한 번만 낸다."""
    # 경로는 files에 있다. 스니펫마다 되풀이하면 출력의 절반이 경로가 되어 문맥 비용으로 돌아온다.
    candidates = [{"file": index, **snippet} for index, entry in enumerate(files) for snippet in found_by_path[entry["path"]]["snippets"]]
    candidates.sort(key=lambda snippet: not snippet["exact"])
    picked = []
    seen = set()
    for snippet in candidates:
        key = _SPACES.sub("", snippet["text"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(snippet)
        if len(picked) == limit:
            break
    return picked


def _rank(document: dict) -> tuple:
    files = document["files"]
    return (
        -max((entry["exact"] for entry in files), default=0),
        -max((entry["spacing_variants"] for entry in files), default=0),
        not document["filename_match"],
        document["_path"],
    )


def _filename_pattern(query: Query):
    if query.ands:
        return None
    return _patterns(query.text, query.regex, True)[1]


def _build_documents(targets, found_by_path, query: Query) -> list:
    groups = {}
    for target in targets:
        groups.setdefault(scan.document_key(target.path), []).append(target)
    name_pattern = _filename_pattern(query)
    documents = []
    for (_, stem), members in groups.items():
        files = [_file_entry(t.path, found_by_path[t.path]) for t in members if found_by_path.get(t.path, {}).get("hit")]
        name_hit = bool(name_pattern and _has_match(name_pattern, normalize.fold(stem)))
        if not files and not name_hit:
            continue
        hit_paths = {entry["path"] for entry in files}
        documents.append(
            {
                "document": stem,
                "filename_match": name_hit,
                "files": files,
                "other_files": [t.path for t in members if t.path not in hit_paths],
                "snippets": _merge_snippets(files, found_by_path, query.snippets),
                "_folder": members[0].relative.split(os.sep)[0] if os.sep in members[0].relative else ".",
                "_path": os.path.join(os.path.dirname(members[0].path), stem),
            }
        )
    documents.sort(key=_rank)
    return documents


def _resolve_hancom_pages(documents, query: Query, notes: list) -> None:
    """반환하는 결과의 hwp·hwpx에 한해 rhwp로 쪽을 정한다. 각 파일은 자기 쪽만 말한다."""
    entries = {entry["path"]: entry for document in documents for entry in document["files"] if entry["kind"] in ("hwp", "hwpx")}
    if not entries:
        return
    binary = rhwp.find()
    failed = False
    if binary is None:
        failed = True
    else:
        with closing(
            scan.run_isolated(_page_job, list(entries), timeout=rhwp.PAGE_TEXT_TIMEOUT + _PAGE_TIMEOUT_MARGIN, args=(query, binary))
        ) as outcomes:
            for outcome in outcomes:
                if outcome.failure is None and outcome.value is not None:
                    entries[outcome.item]["pages"] = outcome.value
                else:
                    failed = True
    for entry in entries.values():
        if entry["pages"] is None:
            entry["page_basis"] = "none"
    if failed:
        notes.append(_DOCTOR_NOTE)


def _size(document: dict) -> int:
    return len(json.dumps(document, ensure_ascii=False))


def _fit(documents: list, max_chars: int) -> list:
    """결과가 상한을 넘으면 자른다. 최소 한 건은 내보내 이어 받기가 항상 진행한다."""
    picked = []
    used = 0
    for document in documents:
        used += _size(document)
        if picked and used > max_chars:
            break
        picked.append(document)
    return picked


def _by_folder(documents: list) -> dict:
    counts = {}
    for document in documents:
        counts[document["_folder"]] = counts.get(document["_folder"], 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    summary = dict(ordered[:FOLDER_LIMIT])
    rest = ordered[FOLDER_LIMIT:]
    if rest:
        summary[f"(그 밖 {len(rest)}개 폴더)"] = sum(count for _, count in rest)
    return summary


def _scan_all(paths, glob, query: Query):
    discovery = scan.discover(paths, glob)
    coverage = scan.Coverage()
    coverage.add_discovery(discovery)
    found_by_path = {}
    with closing(scan.map_documents(_search_file, [t.path for t in discovery.targets], args=(query,))) as results:
        for result in results:
            if coverage.add_result(result):
                found_by_path[result.path] = result.value
    return discovery, coverage, found_by_path


def coverage_records(paths, glob, query: Query):
    """`--coverage-only`: 결과 없이 범위 항목을 상한 없이 낸다."""
    validate(query)
    _, coverage, _ = _scan_all(paths, glob, query)
    return coverage.records()


def run(paths, glob, query: Query, *, limit: int = 20, offset: int = 0, max_chars: int = RESULTS_CHAR_LIMIT) -> dict:
    validate(query)
    started = time.monotonic()
    discovery, coverage, found_by_path = _scan_all(paths, glob, query)
    documents = _build_documents(discovery.targets, found_by_path, query)
    notes = []
    window = documents[offset : offset + limit]
    picked = _fit(window, max_chars)
    _resolve_hancom_pages(picked, query, notes)
    picked = _fit(picked, max_chars)
    next_position = offset + len(picked)
    results = [{key: value for key, value in document.items() if not key.startswith("_")} for document in picked]
    return {
        "summary": {
            "query": query.text,
            "files_scanned": len(discovery.targets),
            "documents_hit": len(documents),
            "exact_documents": sum(1 for d in documents if any(e["exact"] for e in d["files"])),
            "spacing_only_documents": sum(1 for d in documents if d["files"] and not any(e["exact"] for e in d["files"])),
            "filename_only_documents": sum(1 for d in documents if not d["files"]),
            "seconds": round(time.monotonic() - started, 1),
            "by_folder": _by_folder(documents),
            "returned": len(results),
            "next_offset": next_position if next_position < len(documents) else None,
            "notes": notes,
        },
        "results": results,
        "coverage": coverage.summary(list_items=offset == 0),
    }
