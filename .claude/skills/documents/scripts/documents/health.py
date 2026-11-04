"""doctor: 필요한 도구의 상태를 말하고 고정 판을 설치한다."""
from . import kordoc, rhwp


class FixFailed(Exception):
    """설치에 실패했다."""


def run(*, fix: bool = False) -> dict:
    fixed = []
    failures = []
    if fix:
        if not rhwp.status()["ok"]:
            try:
                rhwp.install()
                fixed.append("rhwp")
            except rhwp.ToolError as error:
                failures.append(str(error))
        if not kordoc.status()["ok"]:
            try:
                kordoc.install()
                fixed.append("kordoc")
            except kordoc.ToolError as error:
                failures.append(str(error))
    if failures:
        raise FixFailed("; ".join(failures))
    return {
        "rhwp": rhwp.status(),
        "node": kordoc.node_status(),
        "kordoc": kordoc.status(),
        "ocr_models": kordoc.ocr_status(),
        "fixed": fixed,
    }
