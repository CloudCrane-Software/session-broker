# coding: utf-8
"""broker.cell：目录隔离、leader socket 按 cell 命名、元数据仅引用。"""
import json
import os

import pytest

from session_broker.broker import CredentialCell, MockClock


def _cell(tmp_path, cell_id="cell-a", **kw):
    return CredentialCell(str(tmp_path / cell_id), cell_id, **kw)


def test_create_layout_and_metadata_references_only(tmp_path):
    clk = MockClock(start=1234.0)
    cell = _cell(tmp_path, clock=clk, vendor="demo",
                 login_state_ref="openbao://secret/sessions/demo/alice")
    cell.create()
    assert os.path.isdir(cell.sessions_dir)
    assert os.path.isfile(cell.metadata_path)
    meta = json.load(open(cell.metadata_path, encoding="utf-8"))
    # 元数据只存引用与时间——任何凭据值字段不允许出现
    assert meta["cell_id"] == "cell-a"
    assert meta["login_state_ref"] == "openbao://secret/sessions/demo/alice"
    assert meta["tokens"] == "reference-only"
    raw = open(cell.metadata_path, encoding="utf-8").read()
    assert "secret_value_like_token_string_x9" not in raw


def test_leader_socket_is_unique_per_cell(tmp_path):
    a = _cell(tmp_path, "cell-a")
    b = _cell(tmp_path, "cell-b")
    assert a.leader_socket.endswith("leader-cell-a.sock")
    assert b.leader_socket.endswith("leader-cell-b.sock")
    assert a.leader_socket != b.leader_socket


def test_env_overrides_redirect_home_family(tmp_path):
    cell = _cell(tmp_path)
    env = cell.env_overrides()
    assert env["HOME"] == cell.root
    assert env["USERPROFILE"] == cell.root


def test_config_writer_output_is_persisted(tmp_path):
    cell = _cell(tmp_path, config_writer=lambda c: "key = 'value'\n",
                 config_filename="vendor.toml")
    cell.create()
    assert open(cell.config_path, encoding="utf-8").read() == "key = 'value'\n"


def test_empty_cell_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        CredentialCell(str(tmp_path / "x"), "  ")


def test_config_writer_must_return_text(tmp_path):
    cell = _cell(tmp_path, config_writer=lambda c: None)
    with pytest.raises(ValueError):
        cell.create()
