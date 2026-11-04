"""탐색, 묶음 키, 격리 실행, text 스트림."""
import fnmatch
import json
import multiprocessing
import os
import stat
import unicodedata
from collections import Counter
from concurrent.futures import as_completed
from contextlib import closing
from dataclasses import dataclass, field
from typing import NamedTuple

from . import extract as extract_unit
from . import normalize

# 열지 않고 not_read로 보고한다. 구형 Office(doc·ppt·xls)는 도구가 없고 나머지는 rg와 Read가 맡는다.
NOT_TARGET_EXTENSIONS = frozenset(
    "zip 7z rar tar gz bz2 xz egg alz doc dot ppt pps pot xls xlt rtf odt ods odp "
    "jpg jpeg png gif bmp tif tiff webp svg heic ico emf wmf "
    "md markdown txt csv tsv json jsonl xml html htm yaml yml toml ini log hml "
    "mp3 mp4 mov avi wmv mkv wav m4a "
    "py js ts sh c h cpp java go rs rb exe dmg".split()
)
NO_EXTENSION = "(없음)"

FILE_TIMEOUT = 60.0
MAX_WORKERS = 8
# 공백을 뺀 글자가 이보다 적으면 읽었어도 글이 없는 파일로 보고한다.
NO_TEXT_BELOW = 50


class MissingPath(Exception):
    """인자로 준 경로가 없다."""


class Target(NamedTuple):
    path: str
    root: str
    relative: str


class Outcome(NamedTuple):
    item: object
    value: object
    failure: str | None  # None, timeout, crashed


class FileFacts(NamedTuple):
    path: str
    no_text: bool
    image_only_pages: list
    embedded_images: int


class DocumentResult(NamedTuple):
    path: str
    value: object
    error: str | None
    facts: FileFacts | None


@dataclass
class Discovery:
    targets: list = field(default_factory=list)
    not_read: Counter = field(default_factory=Counter)
    not_read_paths: list = field(default_factory=list)  # (경로, 확장자)
    broken: list = field(default_factory=list)  # 깨진 링크와 열 수 없는 항목


def _extension(name: str) -> str:
    return os.path.splitext(name)[1][1:].lower()


def _matches_glob(relative: str, glob: str | None) -> bool:
    if glob is None:
        return True
    # 디스크 경로가 NFD일 수 있어 양쪽을 NFC로 맞춘다.
    return fnmatch.fnmatchcase(unicodedata.normalize("NFC", relative), unicodedata.normalize("NFC", glob))


def _is_target(path: str, extension: str) -> bool:
    if extension in extract_unit.DOCUMENT_EXTENSIONS:
        return True
    if extension in NOT_TARGET_EXTENSIONS:
        return False
    with open(path, "rb") as stream:
        return extract_unit.looks_like_document(stream.read(1024))


class _Walker:
    def __init__(self, glob):
        self.glob = glob
        self.found = Discovery()
        self.seen_folders = set()
        self.seen_files = set()

    def file(self, path, root, relative, status):
        if not stat.S_ISREG(status.st_mode):
            return
        if not _matches_glob(relative, self.glob):
            return
        identity = (status.st_dev, status.st_ino)
        if identity in self.seen_files:
            return
        self.seen_files.add(identity)
        name = os.path.basename(path)
        extension = _extension(name)
        try:
            is_target = _is_target(path, extension)
        except OSError:
            self.found.broken.append(path)
            return
        if is_target:
            self.found.targets.append(Target(path, root, relative))
        else:
            key = extension or NO_EXTENSION
            self.found.not_read[key] += 1
            self.found.not_read_paths.append((path, key))

    def folder(self, path, root, relative, status):
        identity = (status.st_dev, status.st_ino)
        if identity in self.seen_folders:
            return
        self.seen_folders.add(identity)
        try:
            names = sorted(os.listdir(path))
        except OSError:
            self.found.broken.append(path)
            return
        for name in names:
            if name.startswith("."):
                continue
            child = name if path == os.curdir else os.path.join(path, name)
            child_relative = os.path.join(relative, name) if relative else name
            try:
                child_status = os.stat(child)  # 심링크를 따라간다. 깨진 링크는 여기서 걸린다
            except OSError:
                self.found.broken.append(child)
                continue
            if stat.S_ISDIR(child_status.st_mode):
                self.folder(child, root, child_relative, child_status)
            else:
                self.file(child, root, child_relative, child_status)


def discover(paths, glob: str | None = None) -> Discovery:
    """파일과 폴더를 정렬 순서로 훑는다. 인자로 준 것은 숨김이어도 연다. 없는 인자는 MissingPath다."""
    walker = _Walker(glob)
    roots = [os.path.normpath(path) for path in paths] or ["."]
    statuses = []
    for root in roots:
        try:
            statuses.append(os.stat(root))
        except OSError as error:
            raise MissingPath(root) from error
    for root, status in zip(roots, statuses):
        if stat.S_ISDIR(status.st_mode):
            walker.folder(root, root, "", status)
        else:
            walker.file(root, root, os.path.basename(root), status)
    return walker.found


def document_key(path: str) -> tuple:
    """같은 폴더에서 줄기가 같은 파일을 한 문서로 묶는 키다. 표시만 묶고 검색 범위는 바꾸지 않는다."""
    folder, name = os.path.split(path)
    return unicodedata.normalize("NFC", folder), normalize.document_stem(name)


def run_isolated(fn, items, *, timeout: float = FILE_TIMEOUT, args=()):
    """파일마다 작업자에게 맡기고 끝난 순서로 돌려준다. 파일 하나가 전체를 멈추지 못한다.

    fn은 spawn 자식이 이름으로 불러올 수 있는 모듈 함수여야 한다. timeout은 파일 하나의 실행 시간이다.
    """
    # 성진: fork를 쓰면 macOS에서 자식이 objc 런타임에서 SIGABRT로 죽고 multiprocessing.Pool은 영원히 기다린다(실제 5분 멈춤). 그래서 spawn만 쓴다.
    # 성진: 동시 수는 8에서 포화한다(4: 5.4초, 6: 3.9초, 8: 3.8초, 10: 3.6초). 코어가 많은 기계에서 달라지면 다시 잰다.
    # 성진: 색인 없이 검색마다 원본을 읽는다(9,572개 약 15초). 폴더가 10배면 약 2~3분이므로 그때 저장 색인을 다시 본다.
    items = list(items)
    if not items:
        return
    from pebble import ProcessExpired, ProcessPool

    workers = min(MAX_WORKERS, os.cpu_count() or 1, len(items))
    pool = ProcessPool(max_workers=workers, context=multiprocessing.get_context("spawn"))
    finished = False
    try:
        futures = {pool.schedule(fn, args=(item, *args), timeout=timeout): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                outcome = Outcome(item, future.result(), None)
            except TimeoutError:
                outcome = Outcome(item, None, "timeout")
            except ProcessExpired:
                outcome = Outcome(item, None, "crashed")
            except Exception:
                outcome = Outcome(item, None, "crashed")
            yield outcome
        finished = True
    finally:
        # 중단(Ctrl-C, 받는 쪽이 닫힘)이면 남은 작업을 기다리지 않는다.
        if finished:
            pool.close()
        else:
            pool.stop()
        pool.join()


def _document_worker(path: str, per_document, args):
    extracted = extract_unit.extract(path)
    if extracted.error:
        return None, extracted.error, None
    return per_document(path, extracted, *args), None, facts_of(path, extracted)


def map_documents(per_document, paths, *, args=()):
    """파일마다 작업자 안에서 추출한 뒤 per_document(경로, 추출 결과, *args)를 부른다.

    못 읽은 파일은 error에 닫힌 사유를 담아 돌려주고 나머지는 계속한다.
    """
    for outcome in run_isolated(_document_worker, paths, args=(per_document, args)):
        if outcome.failure:
            yield DocumentResult(outcome.item, None, outcome.failure, None)
        else:
            value, error, facts = outcome.value
            yield DocumentResult(outcome.item, value, error, facts)


def facts_of(path: str, extracted) -> FileFacts:
    letters = sum(1 for _, text in extracted.parts for char in text if not char.isspace())
    return FileFacts(path, letters < NO_TEXT_BELOW, list(extracted.image_only_pages), extracted.embedded_images)


class Coverage:
    """이번 실행이 못 본 범위를 모은다."""

    def __init__(self, item_limit: int = 5):
        self.item_limit = item_limit
        self.unreadable = []
        self.no_text = []
        self.image_only = []
        self.embedded_files = []
        self.not_read = Counter()
        self.not_read_paths = []

    def add_discovery(self, discovery: Discovery) -> None:
        self.not_read.update(discovery.not_read)
        self.not_read_paths.extend(discovery.not_read_paths)
        for path in discovery.broken:
            self.add_failure(path, "io")

    def add_result(self, result: DocumentResult) -> bool:
        """읽은 파일이면 참이다."""
        if result.error:
            self.add_failure(result.path, result.error)
            return False
        self.add_facts(result.facts)
        return True

    def add_failure(self, path: str, reason: str) -> None:
        self.unreadable.append({"path": path, "reason": reason})

    def add_facts(self, facts: FileFacts) -> None:
        if facts.no_text:
            self.no_text.append(facts.path)
        if facts.image_only_pages:
            self.image_only.append({"path": facts.path, "pages": facts.image_only_pages})
        if facts.embedded_images:
            self.embedded_files.append((facts.path, facts.embedded_images))

    def summary(self, *, list_items: bool = True) -> dict:
        """list_items가 거짓이면 수만 준다. 같은 폴더를 이어 받을 때 같은 목록을 되풀이하지 않는다."""
        limit = self.item_limit if list_items else 0

        def capped(items) -> tuple:
            return items[:limit], max(0, len(items) - limit)

        unreadable, unreadable_more = capped(sorted(self.unreadable, key=lambda item: item["path"]))
        no_text, no_text_more = capped(sorted(self.no_text))
        pages, pages_more = capped(sorted(self.image_only, key=lambda item: item["path"]))
        return {
            "unreadable": {"count": len(self.unreadable), "items": unreadable, "more": unreadable_more},
            "no_text": {"count": len(self.no_text), "items": no_text, "more": no_text_more},
            "image_only_pages": {
                "files": len(self.image_only),
                "pages": sum(len(item["pages"]) for item in self.image_only),
                "items": pages,
                "more": pages_more,
            },
            "embedded_images": {"files": len(self.embedded_files), "images": sum(count for _, count in self.embedded_files)},
            "not_read": dict(self.not_read.most_common()),
        }

    def records(self):
        """범위 항목을 상한 없이 하나씩 낸다(`--coverage-only`)."""
        for item in sorted(self.unreadable, key=lambda item: item["path"]):
            yield {"type": "unreadable", **item}
        for path in sorted(self.no_text):
            yield {"type": "no_text", "path": path}
        for item in sorted(self.image_only, key=lambda item: item["path"]):
            yield {"type": "image_only_pages", **item}
        for path, count in sorted(self.embedded_files):
            yield {"type": "embedded_images", "path": path, "images": count}
        for path, extension in sorted(self.not_read_paths):
            yield {"type": "not_read", "path": path, "extension": extension}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _text_of(path: str, extracted) -> str:
    escaped_path = _escape(path)
    lines = []
    for unit, text in extracted.parts:
        unit_text = "" if unit is None else _escape(str(unit))
        folded = normalize.fold(text)
        records = [folded.replace("\n", " ")] if extracted.line_wrapped else folded.split("\n")
        for record in records:
            if record.strip():
                lines.append(f"{escaped_path}\t{unit_text}\t{_escape(record)}\n")
    return "".join(lines)


def stream_text(paths, glob, out, err) -> None:
    """뽑은 글을 파일마다 끝난 순서로 흘려보내고, 범위 보고를 끝에 err로 한 줄 낸다."""
    discovery = discover(paths, glob)
    coverage = Coverage()
    coverage.add_discovery(discovery)
    with closing(map_documents(_text_of, [target.path for target in discovery.targets])) as results:
        for result in results:
            if not coverage.add_result(result):
                continue
            out.write(result.value)
            out.flush()
    err.write(json.dumps({"coverage": coverage.summary()}, ensure_ascii=False) + "\n")
    err.flush()
