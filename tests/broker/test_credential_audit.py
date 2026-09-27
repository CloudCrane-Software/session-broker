# coding: utf-8
"""broker.audit：白名单、token 拒收、指纹化、append-only JSONL。"""
import json

import pytest

from session_broker.broker import CredentialAudit, key_prefix


def test_key_prefix_never_contains_plaintext():
    token = "SUPER-SECRET-VALUE-abcdefghijklmnop"
    fp = key_prefix(token)
    assert token not in fp
    assert fp.startswith("sha256:")
    assert fp.endswith(":len=%d" % len(token))
    assert key_prefix("") == "none"


def test_emit_whitelists_fields_and_fills_host():
    a = CredentialAudit()
    rec = a.emit(event="refresh", cell_id="c1", issuer="https://example.invalid",
                 bogus_debug_field="DROP ME")
    assert rec["type"] == "refresh"
    assert rec["subject"] == "c1"
    assert "issuer" in rec
    assert "bogus_debug_field" not in rec
    assert rec["host"]  # 自动补 host


def test_emit_refuses_raw_token_keys():
    a = CredentialAudit()
    with pytest.raises(ValueError):
        a.emit(event="refresh", cell_id="c1", access_token="FAKE.x.y")
    with pytest.raises(ValueError):
        a.emit(event="refresh", cell_id="c1", token="whatever")
    assert len(a) == 0  # 拒收事件不落审计


def test_no_sink_no_file_written(tmp_path):
    a = CredentialAudit()
    a.emit(event="login", cell_id="c1")
    assert len(a) == 1
    assert not (tmp_path / "audit.jsonl").exists()  # 未接 sink 不落盘


def test_jsonl_sink_grows_append_only(tmp_path):
    sink = tmp_path / "audit.jsonl"
    a = CredentialAudit(sink=str(sink))
    a.emit(event="login", cell_id="c1", key_prefix=key_prefix("FAKE.a.b"))
    a.emit(event="refresh", cell_id="c1", key_prefix=key_prefix("FAKE.a.c"))
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert second["seq"] == first["seq"] + 1
    assert second["type"] == "refresh"
    # 全文件无明文 token 形态
    assert "FAKE.a.b" not in sink.read_text(encoding="utf-8")
