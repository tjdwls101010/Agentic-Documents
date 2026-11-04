"""doctor의 계약. 가짜 설치 경로로 상태 분기를 보고, 실제 설치는 test_real_tools.py가 본다."""
import json
import os
import stat
import sys

import pytest


def test_empty_environment_reports_everything_missing(cli):
    result = cli("doctor")
    assert result.code == 0
    data = result.json
    assert data["rhwp"]["ok"] is False and data["kordoc"]["ok"] is False and data["fixed"] == []
    assert set(data) >= {"rhwp", "node", "kordoc", "ocr_models", "fixed"}


def test_found_tools_report_path_and_version(cli, fake_rhwp, node_path, fake_kordoc):
    data = cli("doctor", path_prefix=f"{node_path}:{fake_rhwp}").json
    assert data["rhwp"]["ok"] is True and data["rhwp"]["version"] == "0.8.7" and data["rhwp"]["path"].endswith("rhwp")
    assert data["node"]["ok"] is True and data["node"]["version"]
    assert data["kordoc"]["ok"] is True and data["kordoc"]["version"] == "4.24.43"


def test_other_rhwp_version_is_reported_as_not_ok(cli, fake_rhwp):
    data = cli("doctor", path_prefix=fake_rhwp, env={"FAKE_RHWP_VERSION": "0.7.0"}).json
    assert data["rhwp"]["ok"] is False


def test_installed_rhwp_takes_precedence(cli, home, fake_rhwp):
    target = home / ".cache/documents/bin"
    target.mkdir(parents=True)
    (target / "rhwp").write_text("#!/bin/sh\necho rhwp v0.8.7\n")
    (target / "rhwp").chmod(0o755)
    assert cli("doctor", path_prefix=fake_rhwp).json["rhwp"]["path"] == str(target / "rhwp")


def test_fix_failure_is_exit_4(cli, tmp_path, fake_rhwp):
    # npm이 없으면 kordoc을 설치할 수 없다. 네트워크를 쓰지 않게 rhwp는 이미 맞는 판을 둔다.
    result = cli("doctor", "--fix", path_prefix=fake_rhwp)
    assert result.code == 4 and result.err.strip()


def test_fix_leaves_what_is_already_right(cli, fake_rhwp, node_path, fake_kordoc):
    result = cli("doctor", "--fix", path_prefix=f"{node_path}:{fake_rhwp}")
    assert result.code == 0 and result.json["fixed"] == []


def test_doctor_help_states_the_contract(cli):
    text = cli("doctor", "--help").out
    for word in ["--fix", "rhwp", "kordoc", "OCR", "종료 코드", "~/.cache/documents"]:
        assert word in text, word
