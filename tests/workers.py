"""scan 시험 전용 작업 함수. spawn 자식이 이름으로 불러올 수 있게 모듈에 둔다."""
import os
import time

INHERITED_FROM_PARENT = False


def echo(item):
    if item == "crash":
        os._exit(1)
    if item == "hang":
        time.sleep(600)
    if item == "raise":
        raise ValueError("작업 함수 예외")
    return item.upper()


def inherited(_item):
    return INHERITED_FROM_PARENT


def objc_runtime(_item):
    # fork 뒤 macOS 시스템 프레임워크를 건드리면 자식이 SIGABRT로 죽는 길이다.
    import _scproxy

    _scproxy._get_proxies()
    return "ok"
