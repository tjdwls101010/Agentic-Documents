---
name: documents
description: 로컬 한컴(hwp·hwpx)·PDF·워드(docx)·파워포인트(pptx)·엑셀(xlsx) 파일을 읽거나 요약하고, 이런 파일이 든 폴더에서 말·수치·사례를 찾거나 있는지 확인할 때 쓴다. 사용자가 폴더나 문서만 말해도 대상이 이 형식이면 쓴다. 트리거: hwp, hwpx, 한글 파일, pdf, 워드, docx, 파워포인트, pptx, 엑셀, xlsx, 문서 읽어줘, 요약해줘, 폴더에서 찾아줘, 자료에 있는지 확인, read this document, search these files. 웹 페이지와 URL의 PDF·Office 문서는 ultra-search, md·csv·html·코드 같은 평문은 rg와 Read를 쓴다. 문서 작성과 변환은 대상이 아니다.
allowed-tools: Bash(uv run "${CLAUDE_SKILL_DIR}/scripts/cli.py":*), Read(./.documents/**)
---

# documents

한컴·PDF·Office 문서를 같은 명령으로 읽고 찾는다. 형식을 나눠 부르지 않고, 섞인 폴더도 한 번에 찾는다. `Read`는 PDF만 열고, `rg`는 이 파일들에서 오류 없이 0건을 내므로 그 0건은 본문에 없다는 근거가 아니다.

`uv run "${CLAUDE_SKILL_DIR}/scripts/cli.py" <search|read|image|text|doctor> …`로 부른다. 인자, 출력, 실패는 `--help`와 `<명령> --help`가 말한다.

## 결과가 보여 주지 않는 것

- 검색은 글자만 본다. 그림 속 글자는 어느 문서에서든 걸리지 않고, `coverage`가 비어도 마찬가지다.
- 글자로 읽은 표는 그림으로 든 값을 빈 칸으로 낸다. 표 모양은 멀쩡하다. 2단이나 펼침 쪽은 표로 오인되어 문장이 행마다 끊긴다. 경고는 휴리스틱이라 놓칠 수 있다. 쪽 그림(`image`, pdf·hwp·hwpx)에는 그대로 보인다.
- 같은 이름 파일은 각자 검색한 것이고 내용이 다를 수 있다.
- OCR은 쪽당 수 초가 들고 도는 중에 멈추기 어렵다.
