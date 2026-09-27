# coding: utf-8
"""broker.minting：stdout 铸币契约与 FAKE 凭据源。"""
import json

import pytest

from session_broker.broker import (FakeCredentialSource, MockClock,
                                   TRIGGER_HEADLESS_REFRESH, TRIGGER_SIGN_IN,
                                   mint_contract_json, parse_provider_output)


def test_fake_source_refuses_non_fake_tokens():
    with pytest.raises(ValueError):
        FakeCredentialSource(token="REAL-LOOKING-TOKEN")


def test_mint_contract_json_is_single_line():
    src = FakeCredentialSource(expires_in=1234, clock=MockClock(start=10.0))
    mt = src.mint(trigger=TRIGGER_SIGN_IN)
    out = mint_contract_json(mt)
    assert out.count("\n") == 0
    obj = json.loads(out)
    assert obj["expires_in"] == 1234
    assert "FAKE" in obj["access_token"]


def test_parse_json_payload():
    mt = parse_provider_output('{"access_token": "FAKE.t", "expires_in": 600}')
    assert mt.token == "FAKE.t"
    assert mt.expires_in == 600


def test_parse_bare_token_defaults_ttl():
    mt = parse_provider_output("FAKE.bare.token\n")
    assert mt.token == "FAKE.bare.token"
    assert mt.expires_in == 3600


def test_parse_contract_violations():
    for bad in ("", "line1\nline2", '{"nope": 1}', '{"access_token": ""}',
                '{"access_token": "FAKE.x", "expires_in": -5}', "not json {"):
        with pytest.raises(ValueError):
            parse_provider_output(bad)


def test_minted_token_audit_fields_carry_no_plaintext():
    mt = parse_provider_output('{"access_token": "FAKE.supersecret.value", '
                               '"expires_in": 60}')
    fields = mt.audit_fields()
    blob = json.dumps(fields)
    assert "supersecret" not in blob
    assert fields["key_prefix"].startswith("sha256:")
    assert fields["trigger"] == TRIGGER_HEADLESS_REFRESH


def test_redacted_repr_hides_token():
    src = FakeCredentialSource()
    mt = src.mint(trigger=TRIGGER_SIGN_IN)
    assert "FAKE.session.token" not in mt.redacted()
    assert mt.redacted().startswith("MintedToken(trigger=sign_in")
