#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "olefile==0.47",
#     "openpyxl==3.1.5",
#     "pebble==5.2.3",
#     "pillow==13.0.0",
#     "pypdfium2==5.14.0",
# ]
# ///
"""documents 스킬의 유일한 진입점. 파서, 도움말, 분기, 종료 코드만 둔다."""
import argparse
import json
import os
import signal
import sys

if __name__ == "__mp_main__":
    # 작업자는 부모가 멈춘다. Ctrl-C가 프로세스 그룹 전체에 가도 작업자마다 예외를 뿌리지 않는다.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREADABLE = 3
EXIT_TOOL = 4
EXIT_INTERRUPTED = 130

_ARGPARSE_KOREAN = {
    "usage: ": "사용법: ",
    "positional arguments": "위치 인자",
    "options": "옵션",
    "show this help message and exit": "이 도움말을 보여 주고 끝낸다",
    "the following arguments are required: %s": "필수 인자가 없다: %s",
    "unrecognized arguments: %s": "알 수 없는 인자: %s",
    "expected one argument": "값이 하나 필요하다",
    "invalid %(type)s value: %(value)r": "값이 잘못되었다(%(type)s): %(value)r",
    "invalid choice: %(value)r (choose from %(choices)s)": "없는 선택지: %(value)r (고를 수 있는 값: %(choices)s)",
    "%(prog)s: error: %(message)s\n": "%(prog)s: 오류: %(message)s\n",
}
argparse._ = lambda text: _ARGPARSE_KOREAN.get(text, text)


def _formatter(prog):
    # 도움말은 줄 단위로 읽힌다. 폭에 맞춰 접으면 조각이 되므로 줄바꿈을 쓴 그대로 둔다.
    return argparse.RawDescriptionHelpFormatter(prog, width=100000, max_help_position=32)


def _non_negative(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("0 이상이어야 한다")
    return value


def _positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 한다")
    return value


def _page_range(text):
    """`3`, `1-5`, `1,3,5-7`을 쪽 번호 목록으로 푼다. 쪽은 1부터다."""
    pages = []
    for piece in text.split(","):
        first, dash, last = piece.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise argparse.ArgumentTypeError(f"쪽 문법은 3, 1-5, 1,3,5-7이다: {text}")
        start, stop = int(first), int(last) if dash else int(first)
        if start < 1 or stop < start:
            raise argparse.ArgumentTypeError(f"쪽은 1부터이고 범위는 오름차순이다: {text}")
        pages.extend(number for number in range(start, stop + 1) if number not in pages)
    return sorted(pages)


_non_negative.__name__ = "0 이상 정수"
_positive.__name__ = "1 이상 정수"
_page_range.__name__ = "쪽 범위"

_TOP_HELP = """Read와 rg가 못 여는 문서(hwp·hwpx·pdf·docx·pptx·xlsx)를 변환 없이 찾고 읽는다. 형식이 달라도 같은 명령이다.

명령마다 인자, 출력, 실패가 `<명령> --help`에 있다.
"""

_SEARCH_HELP = """파일 또는 폴더에서 형식을 나누지 않고 말을 찾는다. 색인이 없어 검색마다 원본을 읽고, 원본은 건드리지 않는다.

찾는 법:
본문과 질의를 NFKC로 접어 대조한다(`㈜`는 `(주)`로, 전각은 반각으로). 호환 자모(글머리표 `ㅇ` 등)는 그대로다.
대소문자는 구분한다. 구분 없이 찾으려면 `--regex '(?i)말'`을 쓴다.
일반 질의는 표기 일치와 띄어쓰기 변형을 나눠 센다. 문장부호가 낀 변형은 잡지 않으니 `--regex`를 쓴다.
대조 단위는 hwp·hwpx·docx는 파일 전체, pdf는 쪽, pptx는 슬라이드, xlsx는 칸이다. 단위를 넘는 말은 걸리지 않는다.
줄바꿈은 문단 경계라 넘지 않는다. pdf의 줄바꿈만 조판 줄이라 넘는다.
글자만 본다. 그림 속 글자는 걸리지 않는다.
숨김 파일과 폴더는 인자로 직접 줄 때만 연다. 심링크를 따라가고 같은 실제 파일은 한 번만 읽는다.
파일 내용으로 형식을 가른다(`.hwpx`인 HWP도 읽는다). 알려진 비대상(zip, doc, 그림, 평문 등)은 열지 않는다.

출력(JSON 하나):
summary: query, files_scanned, documents_hit, exact_documents, spacing_only_documents, filename_only_documents, seconds, by_folder(루트 바로 아래 폴더별 문서 수), returned, next_offset, notes.
results[]: 순위순. document(같은 폴더에서 이름이 같은 파일 묶음), filename_match(이름에 걸림, 본문 0이어도 나온다), files, other_files(묶음 안에서 안 걸린 파일), snippets.
files[]: path, kind, exact, spacing_variants, 위치, page_basis.
  exact: 질의의 공백이 공백 묶음과 맞는 표기 일치 수.
  spacing_variants: exact 바깥에서 글자 사이 공백까지 허용해 찾은 일치 수. `담합`이 `부담 합리화`에 걸리는 오탐도 여기 든다.
  pages: 각 파일 자기 쪽이다. 파일 사이 대응을 뜻하지 않는다. []는 본문에 있으나 쪽을 정하지 못한 것, null은 쪽을 모르는 것이다.
  cells(xlsx): `시트번호:시트이름!셀` 10개까지, 나머지 수는 cells_more.
  page_basis:
    physical: pdf 물리 쪽 순서. 인쇄된 쪽 번호와 다를 수 있다.
    estimated: hwp·hwpx의 rhwp 조판 기준. 같은 이름 PDF와 어긋나는 쪽이 있다(표본 4~9%).
    slide: pptx 표시 순서. sheet: xlsx 시트 순서. none: docx이거나 쪽을 정할 도구가 없다.
snippets[]: path, page(hwp·hwpx는 null, xlsx는 칸 주소), exact, text. text는 접은 글이고 exact가 먼저 온다. 같은 글은 묶음 안에서 한 번만 나온다.
coverage: 이번 실행이 못 본 범위. 목록은 20개까지이고 나머지 수는 more다. 비어 있어도 그림 속 글자는 본 적이 없다.
  unreadable: 못 읽은 파일. reason은 encrypted, distribution(배포용), corrupt, not_a_document, unsupported, too_large, timeout, crashed, io.
  no_text: 읽었지만 공백 뺀 글자가 50 미만인 파일.
  image_only_pages: pdf의 공백 뺀 글자 20 미만인 쪽.
  embedded_images: pdf 밖 형식의 그림 개수. 쪽은 모른다.
  not_read: 대상이 아니라 열지 않은 파일의 확장자별 수.
결과가 2만 자를 넘으면 잘리고 next_offset을 준다. 그 값을 --offset에 주면 이어진다. 결과 한 건은 상한을 넘어도 나온다.

종료 코드:
0: 정상. 0건과 못 읽은 파일이 있어도 0이다(coverage로 보고).
2: 인자 오류(없는 경로, 정규식 오류, 빈 질의).
130: 중단.

`search`에 없는 조합(다른 정규식 도구, 같은 줄의 두 말 등)은 `text`를 `rg`·`awk`로 받는다.
"""

_TEXT_HELP = """뽑은 글자를 흘려보낸다. `search`에 없는 조합을 `rg`·`awk`로 쓸 때 쓴다. 탐색 규칙은 `search`와 같다.

출력(stdout, 한 줄이 한 레코드):
경로<TAB>단위<TAB>글
레코드는 문단(hwp·hwpx·docx), 쪽(pdf, 줄바꿈은 공백으로 접는다), 슬라이드의 문단(pptx), 칸(xlsx)이다.
단위는 쪽 번호, 슬라이드 번호, `2:Sheet1!B3`이거나 비어 있다.
글과 경로의 역슬래시, 탭, 줄바꿈, CR은 `\\\\`, `\\t`, `\\n`, `\\r`로 적는다. 글은 접은 글이다(`search --help` 참고).
파일이 끝난 순서로 나온다. 범위 보고는 끝날 때 stderr에 JSON 한 줄(`{"coverage": …}`)로 나오며 모양은 `search`의 coverage와 같다.

예: text 폴더 | rg '과징금.*담합'

종료 코드:
0: 정상. 못 읽은 파일이 있어도, 받는 쪽이 먼저 닫아도 0이다.
2: 인자 오류(없는 경로).
130: 중단.
"""


_READ_HELP = """파일 하나를 읽는다. 받은 경로를 그대로 연다. 같은 이름의 다른 파일로 바꾸지 않는다.

출력(첫 줄은 JSON 한 줄, 그 뒤는 원문):
{"path", "kind", "page_count", "page_basis", "pages_returned", "chars_total", "offset", "next_offset", "image_pages", "warnings"}
(본문)
pdf: 같은 이름 파일이 아니라 이 파일의 내용이다. 같은 폴더에 같은 이름 PDF가 있으면 pdf 필드가 그 경로를 알려 준다(내용이 다를 수 있다).
sheets(xlsx): [{n, name, dimension}].
pages_returned: 이번 조각에 든 쪽. image_pages: [{page, images, image_area}]로 이번 조각의 그림 있는 쪽. image_area는 쪽 면적 대비 비율이고 pdf 밖에서는 null이다. hwp·hwpx 전체 읽기에서는 문서 전체의 쪽을 말한다. 모르면 null이다.
쪽은 머리 `[p.N]`으로 표시한다. 본문에 없는 그림은 `[그림]`으로 나온다. 스니펫과 달리 본문은 접지 않은 글이다.

page_basis:
  physical: pdf 물리 쪽 순서. 인쇄된 쪽 번호와 다를 수 있다.
  estimated: hwp·hwpx의 rhwp 조판 기준. 같은 이름 PDF와 어긋나는 쪽이 있다.
  slide: pptx 표시 순서. sheet: xlsx 시트 순서. none: docx이거나 쪽을 정할 도구가 없다.

형식별:
  hwp·hwpx: --pages 없으면 문서 전체(본문에 쪽 표시 없음, page_count와 image_pages는 rhwp). --pages 있으면 그 쪽의 rhwp 글(쪽 머리 있음).
  pdf: 쪽 머리 `[p.N]`이 붙는다. pptx: 슬라이드 머리 `[p.N]`이 붙는다. docx: 쪽이 없어 --pages는 오류이고 --offset으로 읽는다.
  xlsx: 시트마다 `## N: 이름 (범위)`와 열 문자·행 번호가 붙은 표. 빈 행과 빈 열은 빼고 번호는 원래대로다. 칸은 표시값, 다르면 `[값 …]`, 수식이면 `[=…]`이다. 수식 칸의 값은 파일에 저장된 캐시이고 계산하지 않는다.
  HWP 3.x, .hml, .xls는 시도만 한다(page_basis none).

warnings: [{code, pages, message}]. 휴리스틱이라 울리지 않아도 안전하다는 뜻이 아니다.
  empty_cells_with_images: 쪽에 그림이 있고 표에 빈 칸이 있다. 값이 그림일 수 있다. `image`로 본다.
  layout_suspect: 다단이나 2쪽 펼침을 표로 오인했거나 읽기 순서가 섞였을 수 있다. `--no-tables`나 `image`로 본다.
  table_may_span_pages: 쪽에 걸친 표가 쪽마다 통째로 나올 수 있다.
  stale_formula_cache: 수식 칸의 값이 낡았을 수 있다.
  page_count_unavailable: rhwp를 쓸 수 없어 쪽을 모른다.
  그 밖에 kordoc 코드(SKIPPED_IMAGE, NEEDS_OCR, OCR_LOW_CONF 등)를 그대로 옮긴다.

이어 읽기: 문서가 --max-chars를 넘으면 next_offset을 준다. 같은 인자에 --offset으로 넘기면 이어지고, 조각을 이어 붙이면 원문과 같다. 한 쪽이 넘어도 마찬가지다.

종료 코드:
0: 정상.
2: 인자 오류(없는 경로, 범위 밖 --pages면 쪽 수를 말한다, 쪽이 없는 형식에 --pages, 대상 밖 파일, pdf 밖의 --no-tables·--ocr, --pages 없는 --ocr).
3: 대상 파일을 못 읽음. stdout은 {"error": {"code", "message"}}이고 code는 encrypted, distribution, corrupt, not_a_document, unsupported, too_large, io.
4: 필요한 도구가 없거나 실패함. 메시지가 `doctor --fix`를 말한다.
130: 중단.
"""

_IMAGE_HELP = """쪽 그림이나 내장 그림을 PNG 등으로 만들고 경로를 준다. 그 경로를 Read로 연다.

형식별:
  pdf: 쪽을 그린다. 긴 변 1568px. --pages 필수. source는 render, page_basis는 physical.
  hwp·hwpx: rhwp로 쪽을 그린다. --pages 필수. source는 render, page_basis는 estimated. 시스템 글꼴로 그려 원본과 조금 다를 수 있다.
  pptx: 슬라이드의 내장 그림을 꺼낸다. --pages로 슬라이드를 고른다. source는 embedded, page_basis는 slide.
  docx·xlsx: 내장 그림을 모두 꺼낸다. page는 null이고 --pages는 오류다. source는 embedded, page_basis는 none.

출력(JSON 하나): {"kind", "source", "page_basis", "images": [{"page", "path", "width", "height", "source_name"}], "skipped": [{"page", "source_name", "reason"}]}
path는 절대 경로다. BMP는 PNG로 바꾼다. EMF·WMF처럼 못 여는 형식은 skipped다(reason: unsupported_format, too_large).

쓰는 곳: --out-dir, 없으면 작업 폴더의 `.documents/pages/`다. 쓸 때만 폴더를 만들고 `.documents/.gitignore`(`*`)를 함께 둔다. 기본 폴더에서만 하루 지난 그림을 지운다. --out-dir에서는 아무것도 지우지 않는다.
기본 폴더는 작업 폴더 안이라 Read가 권한 없이 연다. --out-dir이 작업 폴더 밖이면 Read가 권한을 물을 수 있다.

종료 코드:
0: 정상.
2: 인자 오류(없는 경로, pdf·hwp·hwpx에 --pages 없음, 범위 밖 --pages면 쪽 수를 말한다, 쪽이 없는 형식에 --pages, 대상 밖 파일).
3: 대상 파일을 못 읽거나 쓸 곳에 쓸 수 없음. stdout은 {"error": {"code", "message"}}. 작업 폴더에 쓸 수 없으면 메시지가 --out-dir을 말한다.
4: rhwp가 없거나 실패함. 메시지가 `doctor --fix`를 말한다.
130: 중단.
"""

_DOCTOR_HELP = """rhwp, Node, kordoc, OCR 모델 상태를 말한다. --fix는 고정 판을 설치하고 이미 맞으면 건드리지 않는다.

필요한 곳: hwp·hwpx의 쪽, 쪽 지정 읽기, 쪽 그림은 rhwp 0.8.7. pdf·docx·pptx와 hwp·hwpx 전체 읽기는 Node와 kordoc 4.24.43. search와 text, xlsx 읽기는 없어도 돈다(hwp·hwpx의 쪽만 비고 notes가 말한다).
설치 위치: rhwp는 ~/.cache/documents/bin/rhwp(해시 검사, 약 27MB), kordoc은 ~/.cache/documents/kordoc(npm, 약 461MB). 찾는 순서는 설치 위치, PATH이고 다른 판은 쓰지 않는다.
OCR 모델은 설치하지 않는다. 처음 `read --ocr`을 쓸 때 kordoc이 받는다.

출력(JSON 하나): {"rhwp": {"path", "version", "ok"}, "node": {...}, "kordoc": {...}, "ocr_models": {"ready"}, "fixed": [이번에 설치한 것]}

종료 코드:
0: 정상(도구가 없어도 --fix가 아니면 0이고 ok로 말한다).
4: --fix의 설치 실패.
"""


def _build_parser():
    parser = argparse.ArgumentParser(prog="documents", description=_TOP_HELP, formatter_class=_formatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")

    search = commands.add_parser("search", help="파일 또는 폴더를 형식 구분 없이 찾는다", description=_SEARCH_HELP, formatter_class=_formatter)
    search.add_argument("query", metavar="QUERY", help="찾을 말. 공백은 공백 묶음과 맞는다")
    search.add_argument("paths", metavar="PATH", nargs="*", help="파일 또는 폴더. 안 주면 현재 폴더다. 없는 경로는 종료 코드 2다")
    search.add_argument("--and", dest="ands", metavar="Q", action="append", default=[], help="같은 파일 본문에 함께 있어야 할 말. 여러 번 줄 수 있다. 주 질의와 같은 방식으로 대조하고, 일치 수와 쪽에는 주 질의만 쓴다. 주면 파일 이름 일치는 보지 않는다")
    search.add_argument("--regex", action="store_true", help="QUERY와 --and를 파이썬 정규식으로 본다. 접은 본문에 re.M으로 대조하고 질의는 접지 않는다. 모두 exact로 센다")
    search.add_argument("--glob", metavar="G", help="루트 기준 상대 경로에 fnmatch로 대조한다(`*`는 `/`도 넘는다)")
    search.add_argument("--limit", type=_positive, default=20, help="한 번에 낼 문서 수(기본 20)")
    search.add_argument("--offset", type=_non_negative, default=0, help="건너뛸 문서 수(기본 0). next_offset을 그대로 준다")
    search.add_argument("--snippets", type=_non_negative, default=3, help="문서마다 낼 스니펫 수(기본 3)")
    search.add_argument("--context", type=_non_negative, default=40, help="스니펫의 앞뒤 글자 수(기본 40)")
    search.add_argument("--coverage-only", action="store_true", help="결과 없이 범위 항목을 한 줄에 하나씩 NDJSON으로 전부 낸다. 상한이 없다. 줄마다 type(unreadable, no_text, image_only_pages, embedded_images, not_read)과 path가 있다. QUERY는 범위에 영향을 주지 않는다")

    read = commands.add_parser("read", help="한 파일을 읽는다", description=_READ_HELP, formatter_class=_formatter)
    read.add_argument("file", metavar="FILE", help="읽을 파일")
    read.add_argument("--pages", metavar="R", type=_page_range, help="읽을 쪽. Read의 pages와 같은 문법이다(3, 1-5, 1,3,5-7). 쪽은 1부터다. pptx는 슬라이드, xlsx는 시트 번호다")
    read.add_argument("--no-tables", action="store_true", help="pdf 전용. 표 감지를 끄고 읽기 순서대로 낸다. 다단이나 펼침이 표로 오인된 쪽에 쓴다. 진짜 표는 망가질 수 있어 기본값이 아니다")
    read.add_argument("--ocr", action="store_true", help="pdf 전용. 글자 없는 쪽을 OCR한다. --pages가 필수다. 쪽당 1~3초가 들고 돌기 시작하면 멈추기 어렵다. 표 안 그림값은 OCR로도 안 나올 수 있다. 처음 쓸 때 모델(약 18MB)을 내려받는다. 다른 형식은 `image`로 본다")
    read.add_argument("--offset", type=_non_negative, default=0, help="조립한 글의 문자 위치(기본 0). next_offset을 그대로 준다")
    read.add_argument("--max-chars", type=_positive, default=20000, help="이번에 낼 문자 수(기본 20000)")

    image = commands.add_parser("image", help="쪽 그림 또는 내장 그림을 만든다", description=_IMAGE_HELP, formatter_class=_formatter)
    image.add_argument("file", metavar="FILE", help="대상 파일")
    image.add_argument("--pages", metavar="R", type=_page_range, help="쪽 또는 슬라이드(3, 1-5, 1,3,5-7). 쪽은 1부터다. 범위는 쓰는 쪽이 정한다")
    image.add_argument("--out-dir", metavar="D", help="그림을 쓸 폴더. 없으면 만든다")

    text = commands.add_parser("text", help="뽑은 글자를 흘려보낸다", description=_TEXT_HELP, formatter_class=_formatter)
    text.add_argument("paths", metavar="PATH", nargs="*", help="파일 또는 폴더. 안 주면 현재 폴더다")
    text.add_argument("--glob", metavar="G", help="루트 기준 상대 경로에 fnmatch로 대조한다(`*`는 `/`도 넘는다)")

    doctor = commands.add_parser("doctor", help="필요한 도구 상태를 말하고 설치한다", description=_DOCTOR_HELP, formatter_class=_formatter)
    doctor.add_argument("--fix", action="store_true", help="없거나 판이 다른 도구를 고정 판으로 설치한다. 네트워크가 필요하다")
    return parser


def _print_json(document):
    print(json.dumps(document, ensure_ascii=False))


def _run_search(args):
    from documents import search

    query = search.Query(args.query, tuple(args.ands), args.regex, args.snippets, args.context)
    if args.coverage_only:
        for record in search.coverage_records(args.paths, args.glob, query):
            _print_json(record)
    else:
        _print_json(search.run(args.paths, args.glob, query, limit=args.limit, offset=args.offset))
    return EXIT_OK


def _run_text(args):
    from documents import scan

    scan.stream_text(args.paths, args.glob, sys.stdout, sys.stderr)
    return EXIT_OK


def _run_read(args):
    from documents import reading

    result = reading.read(args.file, pages=args.pages, no_tables=args.no_tables, ocr=args.ocr, offset=args.offset, max_chars=args.max_chars)
    sys.stdout.write(json.dumps(result.header, ensure_ascii=False) + "\n" + result.text)
    sys.stdout.flush()
    return EXIT_OK


def _run_image(args):
    from documents import images

    _print_json(images.make_images(args.file, pages=args.pages, out_dir=args.out_dir))
    return EXIT_OK


def _run_doctor(args):
    from documents import health

    try:
        _print_json(health.run(fix=args.fix))
    except health.FixFailed as error:
        print(f"오류: 설치 실패: {error}", file=sys.stderr)
        return EXIT_TOOL
    return EXIT_OK


_COMMANDS = {"search": _run_search, "text": _run_text, "read": _run_read, "image": _run_image, "doctor": _run_doctor}


def _failure_classes():
    from documents import reading, scan, search

    return {
        "usage": (scan.MissingPath, search.UsageError, reading.UsageProblem),
        "unreadable": (reading.Unreadable,),
        "tool": (reading.ToolUnavailable,),
    }


def main():
    # 디스크 경로가 UTF-8이 아니어도 그대로 내보낸다.
    sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")
    sys.stderr.reconfigure(encoding="utf-8", errors="surrogateescape")
    args = _build_parser().parse_args()
    failures = _failure_classes()
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # 받는 쪽(`rg -m1`, `head`)이 먼저 닫았다. 파이썬이 끝날 때 다시 쓰다 실패하지 않게 막는다.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except failures["usage"] as error:
        message = f"없는 경로: {error}" if type(error).__name__ == "MissingPath" else str(error)
        print(f"오류: {message}", file=sys.stderr)
        return EXIT_USAGE
    except failures["unreadable"] as error:
        _print_json({"error": {"code": error.code, "message": error.message}})
        return EXIT_UNREADABLE
    except failures["tool"] as error:
        print(f"오류: {error}", file=sys.stderr)
        return EXIT_TOOL


if __name__ == "__main__":
    sys.exit(main())
