"""본문과 질의를 같은 모양으로 접고, 띄어쓰기 변형을 나눠 찾는 패턴을 만든다."""
import re
import unicodedata
from typing import NamedTuple

# 호환 자모는 NFKC에서 첫소리 자모로 바뀌는데, 문서에서는 글머리표(ㅇ)와 가운뎃점(ㆍ)으로 쓰인다.
_NOT_COMPAT_JAMO = re.compile(r"[^ㄱ-ㆎ]+")

# 문단 구조 형식에서 줄바꿈은 문단 경계이므로 공백 묶음이 넘지 않는다. PDF의 줄바꿈은 조판일 뿐이므로 넘는다.
_INLINE_SPACE = r"[^\S\n]"
_ANY_SPACE = r"\s"
_SPACES = re.compile(r"\s+")


class Patterns(NamedTuple):
    exact: re.Pattern
    loose: re.Pattern


def _nfkc(match: re.Match) -> str:
    return unicodedata.normalize("NFKC", match.group())


def fold(text: str) -> str:
    """NFKC로 접되 호환 자모는 지킨다. `㈜`는 `(주)`가 된다."""
    if text.isascii():
        return text
    return _NOT_COMPAT_JAMO.sub(_nfkc, text)


def patterns(query: str, *, line_wrapped: bool) -> Patterns:
    """질의의 표기 일치(exact)와 글자 사이 공백까지 허용한 일치(loose)를 만든다.

    문장부호가 낀 변형은 잡지 않는다. line_wrapped는 줄바꿈이 조판 줄인 형식(PDF)일 때 참이다.
    """
    words = fold(query).split()
    if not words:
        raise ValueError("질의가 비어 있다")
    space = _ANY_SPACE if line_wrapped else _INLINE_SPACE
    exact = (space + "+").join(re.escape(word) for word in words)
    loose = (space + "*").join(re.escape(letter) for letter in "".join(words))
    return Patterns(re.compile(exact), re.compile(loose))


def document_stem(name: str) -> str:
    """같은 이름 파일을 알아보는 줄기다. NFC, 공백 접기, 끝 점·공백 제거, 확장자 제외."""
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    return _SPACES.sub(" ", unicodedata.normalize("NFC", stem)).rstrip(". ")


def regex(query: str) -> re.Pattern:
    """`--regex` 질의. 접지 않고 접은 본문에 줄 단위로 대조한다. 잘못된 정규식은 re.error를 낸다."""
    return re.compile(query, re.M)
