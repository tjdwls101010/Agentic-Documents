"""기대값은 손으로 풀었다. normalize의 약속: 접기와 두 패턴."""
from documents import normalize


def counts(query, text, line_wrapped=False):
    pair = normalize.patterns(query, line_wrapped=line_wrapped)
    return len(pair.exact.findall(text)), len(pair.loose.findall(text))


def test_fold_expands_compatibility_characters():
    assert normalize.fold("㈜제분") == "(주)제분"
    assert normalize.fold("ＡＢＣ１２３") == "ABC123"
    assert normalize.fold("가 나") == "가 나"
    assert normalize.fold("가　나") == "가 나"


def test_fold_keeps_compatibility_jamo():
    # NFKC는 ㅇ(U+3147)을 첫소리 자모 U+110B로 바꾼다. 글머리표이므로 지킨다.
    assert normalize.fold("ㅇ 정책") == "ㅇ 정책"
    assert normalize.fold("ㅇ㈜") == "ㅇ(주)"
    assert normalize.fold("ㆍ") == "ㆍ"  # U+318D


def test_fold_composes_hangul():
    # 디스크 경로와 일부 추출물은 NFD일 수 있다.
    assert normalize.fold("가") == "가"


def test_exact_matches_any_run_of_inline_spaces():
    assert counts("가상자산 이용자", "가상자산   이용자") == (1, 1)
    assert counts("가상자산 이용자", "가상자산\t이용자") == (1, 1)


def test_variant_allows_spaces_between_letters():
    assert counts("담합", "부담 합리화와 담합") == (1, 2)
    assert counts("주민등록번호 처리제한", "주민등록번호처리제한") == (0, 1)
    assert counts("주민등록번호 처리제한", "주민 등록번호 처리 제한") == (0, 1)


def test_paragraph_boundary_is_not_crossed():
    assert counts("의무화 노사정", "의무화\n노사정 합의안") == (0, 0)
    assert counts("의무화", "의\n무화") == (0, 0)


def test_pdf_lines_are_crossed():
    assert counts("의무화 노사정", "의무화\n노사정", line_wrapped=True) == (1, 1)
    assert counts("의무화", "의\n무화", line_wrapped=True) == (0, 1)


def test_exact_is_contained_in_variant():
    pair = normalize.patterns("a b", line_wrapped=False)
    for text in ["a b", "ab", "a  b a b", "a\nb", "aab b", "b a"]:
        assert len(pair.exact.findall(text)) <= len(pair.loose.findall(text)), text
        if pair.exact.search(text):
            assert pair.loose.search(text), text


def test_query_is_folded_like_the_body():
    assert counts("㈜제분", "(주)제분") == (1, 1)
    assert counts("(주)제분", normalize.fold("㈜제분")) == (1, 1)


def test_query_metacharacters_are_literal():
    assert counts("a.b", "axb a.b") == (1, 1)
    assert counts("(주)", "주 (주)") == (1, 1)


def test_punctuation_between_letters_is_not_a_variant():
    assert counts("개인정보", "개인·정보") == (0, 0)


def test_blank_query_is_refused():
    import pytest

    with pytest.raises(ValueError):
        normalize.patterns("   ", line_wrapped=False)


def test_regex_pattern_uses_multiline_and_is_not_folded():
    compiled = normalize.regex("^가$")
    assert compiled.findall("가\n나\n가") == ["가", "가"]
    assert normalize.regex("㈜").findall(normalize.fold("㈜")) == []
