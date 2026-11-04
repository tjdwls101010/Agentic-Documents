"""`uv run python -m corpus_check docs [--vault 폴더] [--sample N]`

스킬은 cli.py로만 부른다(실제 계약). 기대값은 스킬이 닿지 못하는 곳에서 온다: 같은 이름 PDF, rhwp, kordoc md.
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / ".claude/skills/documents/scripts/cli.py"
SEED = 20261104
RECALL_GATE = 99.98
_WHITESPACE = re.compile(r"\s+")
_NOT_COMPAT_JAMO = re.compile(r"[^ㄱ-ㆎ]+")


def fold(text):
    return _NOT_COMPAT_JAMO.sub(lambda m: unicodedata.normalize("NFKC", m.group()), text)


def squeeze(text):
    return _WHITESPACE.sub("", text)


def unescape(text):
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}.get(m.group(1), m.group(0)), text)


def cli(*args, **options):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", **options)


def search(query, root, *extra):
    completed = cli("search", query, root, *extra)
    if completed.returncode != 0:
        raise SystemExit(f"search 실패({query}): {completed.stderr}")
    return json.loads(completed.stdout)


def stream_all(root):
    """text 명령으로 전체를 읽어 경로별 (단위, 글)로 모은다."""
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, str(CLI), "text", root], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="surrogateescape")
    documents = defaultdict(list)
    for line in process.stdout:
        path, unit, text = line.rstrip("\n").split("\t")
        documents[unescape(path)].append((unit, unescape(text)))
    stderr = process.stderr.read()
    process.wait()
    coverage = json.loads(stderr.strip().splitlines()[-1])["coverage"]
    return documents, coverage, round(time.monotonic() - started, 1)


def twins(documents):
    """같은 폴더, 같은 줄기의 (한컴 경로들, PDF 경로)."""
    groups = defaultdict(dict)
    for path in documents:
        folder, name = os.path.split(path)
        stem, extension = os.path.splitext(name)
        key = (unicodedata.normalize("NFC", folder), _WHITESPACE.sub(" ", unicodedata.normalize("NFC", stem)).rstrip(". "))
        groups[key][extension[1:].lower()] = path
    return [(group, group["pdf"]) for group in groups.values() if "pdf" in group and ({"hwp", "hwpx"} & set(group))]


def words_of(text):
    return {word for word in text.split() if len(word) >= 4}


_HANGUL_RUN = re.compile(r"[가-힣]{4,}")


def hangul_runs(text):
    """게이트의 어절: 한글 네 글자 이상의 연속. PDF 글자층의 문장부호·글머리 찌꺼기(`L“`, `\\uf000`, `ᆞ`)는 원본 글자가 아니다."""
    return set(_HANGUL_RUN.findall(text))


def pdf_recall(documents):
    """같은 이름 PDF의 어절이 한컴 본문에 있는 비율. 게이트는 한글 연속으로 재고, 공백 어절 수치는 참고로 낸다."""
    gate_total = gate_missing = raw_total = raw_missing = files = 0
    samples = []
    pairs = twins(documents)
    for group, pdf_path in pairs:
        pdf_text = " ".join(text for _, text in documents[pdf_path])
        runs = hangul_runs(pdf_text)
        raw = words_of(pdf_text)
        for kind in ("hwp", "hwpx"):
            if kind not in group:
                continue
            files += 1
            body = squeeze("".join(text for _, text in documents[group[kind]]))
            lost = [word for word in runs if word not in body]
            gate_total += len(runs)
            gate_missing += len(lost)
            raw_total += len(raw)
            raw_missing += sum(1 for word in raw if word not in body)
            if lost and len(samples) < 8:
                samples.append({"file": os.path.basename(group[kind]), "words": lost[:3]})
    recall = 100 * (1 - gate_missing / gate_total) if gate_total else 0
    return {
        "hancom_files": files,
        "hangul_runs": gate_total,
        "missing": gate_missing,
        "recall_percent": round(recall, 4),
        "gate": RECALL_GATE,
        "passed": recall >= RECALL_GATE,
        "samples": samples,
        "whitespace_words": {"words": raw_total, "missing": raw_missing, "recall_percent": round(100 * (1 - raw_missing / raw_total), 3) if raw_total else 0},
    }


def rhwp_binary():
    return os.path.expanduser("~/.cache/documents/bin/rhwp") if os.path.exists(os.path.expanduser("~/.cache/documents/bin/rhwp")) else shutil.which("rhwp")


def rhwp_pages(path):
    binary = rhwp_binary()
    completed = subprocess.run([binary, "export-text", path, "--json"], capture_output=True, text=True, errors="replace", timeout=60)
    if completed.returncode != 0:
        return None
    return [fold(page["text"]) for page in json.loads(completed.stdout)["pages"]]


def parallel(function, items):
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(function, items))


# rhwp가 조판하며 앞에 붙이는 글머리표는 원본 글자가 아니다.
_GENERATED_BULLET = re.compile(r"^[ㅇ□○●■◦▸▪※\-·ㆍ◎◇◆▶►]+|^\d+\.")


def rhwp_recall(documents, sample):
    """rhwp 글자의 4자 이상 어절이 직접 파서 본문에 있는 비율."""
    hancom = sorted(path for path in documents if path.lower().endswith((".hwp", ".hwpx")))
    random.Random(SEED).shuffle(hancom)
    chosen = hancom[:sample]

    def check(path):
        try:
            pages = rhwp_pages(path)
        except (subprocess.TimeoutExpired, ValueError):
            return None
        if pages is None:
            return None
        body = squeeze("".join(text for _, text in documents[path]))
        words = set()
        for page in pages:
            words |= {_GENERATED_BULLET.sub("", word) for word in words_of(page)}
        words = {word for word in words if len(word) >= 4}
        return len(words), [word for word in words if word not in body]

    results = [result for result in parallel(check, chosen) if result]
    total = sum(count for count, _ in results)
    lost = [word for _, words in results for word in words]
    return {"files": len(results), "words": total, "missing": len(lost), "recall_percent": round(100 * (1 - len(lost) / total), 3) if total else 0, "samples": lost[:10]}


def hit_agreement(documents, root, word):
    """한 낱말의 파일별 적중을 rhwp 글자와 대조한다."""
    root = unicodedata.normalize("NFC", root)
    hancom = sorted(path for path in documents if path.lower().endswith((".hwp", ".hwpx")) and unicodedata.normalize("NFC", path).startswith(root))

    def check(path):
        try:
            pages = rhwp_pages(path)
        except (subprocess.TimeoutExpired, ValueError):
            return path, None
        return path, None if pages is None else any(word in squeeze(page) for page in pages)

    only_ours, only_rhwp, both, skipped = [], [], 0, 0
    for path, in_rhwp in parallel(check, hancom):
        if in_rhwp is None:
            skipped += 1
            continue
        in_ours = word in squeeze("".join(text for _, text in documents[path]))
        if in_ours and in_rhwp:
            both += 1
        elif in_ours:
            only_ours.append(os.path.basename(path))
        elif in_rhwp:
            only_rhwp.append(os.path.basename(path))
    return {"word": word, "files": len(hancom), "both": both, "only_ours": len(only_ours), "only_rhwp": len(only_rhwp), "rhwp_failed": skipped, "samples_only_rhwp": only_rhwp[:5], "samples_only_ours": only_ours[:5]}


def page_accuracy(documents, sample):
    """같은 이름 PDF의 물리 쪽을 기준으로 rhwp 쪽을 표본 대조한다. PDF 쪽도 진실은 아니고 기준선이다."""
    pairs = []
    for group, pdf_path in twins(documents):
        for kind in ("hwp", "hwpx"):
            if kind in group:
                pairs.append((group[kind], pdf_path))
    random.Random(SEED).shuffle(pairs)
    picker = random.Random(SEED)

    def check(pair):
        path, pdf_path = pair
        try:
            pages = rhwp_pages(path)
        except (subprocess.TimeoutExpired, ValueError):
            return []
        if not pages:
            return []
        squeezed = [squeeze(page) for page in pages]
        checked = []
        pdf_pages = documents[pdf_path]
        random_pages = picker.sample(pdf_pages, min(3, len(pdf_pages)))
        for unit, text in random_pages:
            words = text.split()
            if len(words) < 12:
                continue
            start = len(words) // 2
            phrase = "".join(words[start : start + 3])
            found = [number for number, page in enumerate(squeezed, 1) if phrase in page]
            if len(found) == 1:
                checked.append(found[0] - int(unit))
        return checked

    differences = [value for values in parallel(check, pairs[:sample]) for value in values]
    count = len(differences)
    return {
        "phrases": count,
        "exact_percent": round(100 * sum(d == 0 for d in differences) / count, 1) if count else 0,
        "within_one_percent": round(100 * sum(abs(d) <= 1 for d in differences) / count, 1) if count else 0,
    }


def vault_recall(documents, vault):
    """kordoc md의 4자 이상 어절이 직접 파서 본문에 있는 비율. md는 같은 줄기의 파일과 맞춘다."""
    by_stem = defaultdict(list)
    for path in documents:
        if path.lower().endswith((".hwp", ".hwpx")):
            by_stem[squeeze(unicodedata.normalize("NFC", os.path.splitext(os.path.basename(path))[0]))].append(path)
    by_kind = Counter()
    lost_by_kind = Counter()
    matched = 0
    for folder, _, names in os.walk(vault):
        for name in names:
            if not name.endswith(".md"):
                continue
            stem = squeeze(unicodedata.normalize("NFC", name[:-3]))
            for path in by_stem.get(stem, []):
                markdown = fold(open(os.path.join(folder, name), encoding="utf-8", errors="replace").read())
                markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)|[*|#`]", " ", markdown)
                body = squeeze("".join(text for _, text in documents[path]))
                words = words_of(markdown)
                kind = path.rsplit(".", 1)[1].lower()
                by_kind[kind] += len(words)
                lost_by_kind[kind] += sum(1 for word in words if word not in body)
                matched += 1
    return {"matched_files": matched, **{kind: round(100 * (1 - lost_by_kind[kind] / by_kind[kind]), 2) for kind in by_kind}}


CASES = [
    ("일등중사", "남운수 PDF에만 있다", lambda d: any(f["kind"] == "pdf" and "남운수" in r["document"] for r in d["results"] for f in r["files"]) and any(p.endswith(".hwpx") for r in d["results"] for p in r["other_files"])),
    ("남운수 상병", "남운수 HWPX에만 있다", lambda d: any(f["kind"] == "hwpx" for r in d["results"] for f in r["files"])),
    ("의료쇼핑", "줄바꿈 섞인 글이 HWPX·PDF 모두 걸린다", lambda d: any({"hwpx", "pdf"} <= {f["kind"] for f in r["files"]} and "마약류대책협의회" in r["document"] for r in d["results"])),
]


def read_header(path, *extra):
    completed = cli("read", path, "--max-chars", "1", *extra)
    if completed.returncode != 0:
        return None
    return json.loads(completed.stdout.partition("\n")[0])


def warning_rates(root, sample):
    """표본 PDF 전체를 읽어 경고가 얼마나 자주 울리는지 잰다."""
    paths = sorted(os.path.join(folder, name) for folder, _, names in os.walk(root) for name in names if name.lower().endswith(".pdf"))
    random.Random(SEED).shuffle(paths)
    chosen = paths[:sample]
    headers = parallel(read_header, chosen)
    counts = Counter()
    pages = 0
    examples = defaultdict(list)
    for path, header in zip(chosen, headers):
        if header is None:
            counts["read_failed"] += 1
            continue
        pages += header["page_count"] or 0
        for warning in header["warnings"]:
            counts[warning["code"]] += len(warning["pages"]) or 1
            examples[warning["code"]].extend((path, page) for page in warning["pages"])
    picker = random.Random(SEED)
    drawn = {code: [{"path": path, "page": page} for path, page in picker.sample(items, min(6, len(items)))] for code, items in examples.items() if code in ("empty_cells_with_images", "layout_suspect")}
    return {"pdfs": len(chosen), "pages": pages, "warning_pages": dict(counts), "examples": drawn}


def find_file(root, *parts):
    for folder, _, names in os.walk(root):
        for name in names:
            normal = unicodedata.normalize("NFC", os.path.join(folder, name))
            if all(part in normal for part in parts):
                return os.path.join(folder, name)
    return None


def _body(path, *extra):
    completed = cli("read", path, *extra)
    return completed.stdout.partition("\n")[2] if completed.returncode == 0 else ""


# 대체 품질 문항의 도구 수준 길. 정답은 내장 Read로 확인했다(모델 수준은 평가에서 묻는다).
QUALITY = [
    ("바디워시 17쪽 헹굼 정도", ("251014_바디워시", ".pdf"), 17, "empty_cells_with_images", None),
    ("자사우대 35쪽 그룹 3", ("260628_플랫폼", "붙임_", ".pdf"), 35, "empty_cells_with_images", None),
    ("국민성장펀드 7쪽 인피니언의 나라", ("260211_본격", "별첨2", ".pdf"), 7, "empty_cells_with_images", None),
    ("사례집 27쪽 연락처 오기재", ("260526_2025년", "2025 개인정보 분쟁조정 사례집.pdf"), 27, "layout_suspect", "보기 어렵다"),
    ("가습기 20쪽 세척 표의 절", ("251030_가습기", ".pdf"), 20, "layout_suspect", "제품 세척 시 주의사항"),
]


def quality_questions(root):
    report = []
    for label, parts, page, code, answer in QUALITY:
        path = find_file(root, *parts)
        header = read_header(path, "--pages", str(page)) if path else None
        warned = bool(header and any(w["code"] == code and page in w["pages"] for w in header["warnings"]))
        answered = None
        if answer is not None:
            answered = answer in _body(path, "--pages", str(page), "--no-tables")
        pictured = cli("image", path, "--pages", str(page)).returncode == 0 if path else False
        report.append({"question": label, "warned": warned, "text_answer_with_no_tables": answered, "image_ok": pictured, "passed": warned and pictured and answered is not False})
    return report


def spacing_cases(root):
    report = {}
    for query, expected in (("가상자산 이용자 보호", (10, 24)), ("주민등록번호 처리제한", (4, 10))):
        summary = search(query, root)["summary"]
        report[query] = {"exact": summary["exact_documents"], "all": summary["documents_hit"], "planned": expected}
    return report


def run_cases(root):
    report = []
    for query, meaning, check in CASES:
        data = search(query, root)
        report.append({"query": query, "meaning": meaning, "documents": data["summary"]["documents_hit"], "passed": bool(check(data))})
    return report


def main():
    parser = argparse.ArgumentParser(prog="corpus_check")
    parser.add_argument("root", help="코퍼스 폴더")
    parser.add_argument("--vault", help="kordoc md가 있는 폴더")
    parser.add_argument("--sample", type=int, default=600, help="rhwp를 부르는 표본 수")
    parser.add_argument("--warning-sample", type=int, default=400, help="경고 빈도를 잴 PDF 표본 수")
    args = parser.parse_args()
    report = {}
    started = time.monotonic()
    data = search("과징금", args.root, "--limit", "1")
    report["search_seconds_warm"] = round(time.monotonic() - started, 1)
    report["files_scanned"] = data["summary"]["files_scanned"]
    report["cases"] = run_cases(args.root)
    report["spacing"] = spacing_cases(args.root)
    documents, coverage, seconds = stream_all(args.root)
    report["text_seconds"] = seconds
    report["coverage"] = {
        "unreadable": coverage["unreadable"],
        "no_text": coverage["no_text"],
        "image_only_pages": {key: coverage["image_only_pages"][key] for key in ("files", "pages")},
        "embedded_images": coverage["embedded_images"],
        "not_read": coverage["not_read"],
    }
    report["pdf_word_recall"] = pdf_recall(documents)
    if rhwp_binary():
        report["rhwp_word_recall"] = rhwp_recall(documents, args.sample)
        report["gwajing_vs_rhwp"] = hit_agreement(documents, os.path.join(args.root, "공정거래위원회"), "과징금")
        report["page_accuracy"] = page_accuracy(documents, args.sample)
    if args.vault:
        report["vault_recall"] = vault_recall(documents, args.vault)
    report["quality_questions"] = quality_questions(args.root)
    report["warning_rates"] = warning_rates(args.root, args.warning_sample)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    failed = not report["pdf_word_recall"]["passed"] or not all(case["passed"] for case in report["cases"]) or not all(item["passed"] for item in report["quality_questions"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
