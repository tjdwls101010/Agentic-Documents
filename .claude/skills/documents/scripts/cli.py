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


_non_negative.__name__ = "0 이상 정수"
_positive.__name__ = "1 이상 정수"

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
    estimated: hwp·hwpx의 rhwp 조판 기준. 약 9%가 실제와 어긋난다.
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

    text = commands.add_parser("text", help="뽑은 글자를 흘려보낸다", description=_TEXT_HELP, formatter_class=_formatter)
    text.add_argument("paths", metavar="PATH", nargs="*", help="파일 또는 폴더. 안 주면 현재 폴더다")
    text.add_argument("--glob", metavar="G", help="루트 기준 상대 경로에 fnmatch로 대조한다(`*`는 `/`도 넘는다)")
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


_COMMANDS = {"search": _run_search, "text": _run_text}


def _usage_errors():
    from documents import scan, search

    return scan.MissingPath, search.UsageError


def main():
    # 디스크 경로가 UTF-8이 아니어도 그대로 내보낸다.
    sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")
    sys.stderr.reconfigure(encoding="utf-8", errors="surrogateescape")
    args = _build_parser().parse_args()
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # 받는 쪽(`rg -m1`, `head`)이 먼저 닫았다. 파이썬이 끝날 때 다시 쓰다 실패하지 않게 막는다.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except _usage_errors() as error:
        message = f"없는 경로: {error}" if type(error).__name__ == "MissingPath" else str(error)
        print(f"오류: {message}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
