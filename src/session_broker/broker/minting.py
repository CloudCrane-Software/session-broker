# coding: utf-8
"""铸币契约（minting contract）——外部认证提供者的 stdout 协议，vendor 无关.

外部提供者命令（由 vendor 配置挂进 CLI；本框架只定义协议本身）：

- **stdout**：只能是被 CLI 原样解析的 token 载荷——单行裸 token，或单行 JSON
  ``{"access_token": "...", "expires_in": <秒>}``；
- **stderr**：给人看的状态（CLI 会展示，不进协议）；
- **触发分支**：环境变量告知"token 已失效，请静默快速换发"（``headless_refresh``，
  短预算、静默、失败即非零退出）与"冷启动交互登录"（``sign_in``，长预算、
  可能需要用户在场）两类；具体环境变量名由 vendor adapter 定义。

红线：本模块只处理 FAKE / broker 凭据库引用；任何真实凭据值不进本仓代码与测试。
"""
from __future__ import annotations

import json
import time

from .audit import key_prefix
from .clock import SystemClock

__all__ = ["TRIGGER_SIGN_IN", "TRIGGER_HEADLESS_REFRESH", "MintedToken",
           "FakeCredentialSource", "mint_contract_json", "parse_provider_output",
           "REDACTED"]

TRIGGER_SIGN_IN = "sign_in"
TRIGGER_HEADLESS_REFRESH = "headless_refresh"

REDACTED = "<REDACTED>"


class MintedToken(object):
    """一次铸币结果（token 本身只在内存；对外只暴露指纹）。"""

    __slots__ = ("token", "expires_in", "fetched_at_epoch", "trigger",
                 "latency_ms", "outcome")

    def __init__(self, token, expires_in, fetched_at_epoch, trigger,
                 latency_ms=0, outcome="success"):
        self.token = token
        self.expires_in = int(expires_in)
        self.fetched_at_epoch = float(fetched_at_epoch)
        self.trigger = str(trigger)
        self.latency_ms = int(latency_ms)
        self.outcome = str(outcome)

    @property
    def expires_at_epoch(self):
        return self.fetched_at_epoch + self.expires_in

    @property
    def fingerprint(self):
        return key_prefix(self.token)

    def audit_fields(self):
        """可安全进审计的字段（无明文）。"""
        return {
            "trigger": self.trigger,
            "outcome": self.outcome,
            "expires_at": self.expires_at_epoch,
            "mint_age_seconds": 0,
            "key_prefix": self.fingerprint,
            "exit_code": 0,
            "provider_latency_ms": self.latency_ms,
        }

    def redacted(self):
        return ("MintedToken(trigger=%s, outcome=%s, token=%s, expires_in=%d)"
                % (self.trigger, self.outcome, self.fingerprint, self.expires_in))


class FakeCredentialSource(object):
    """FAKE 凭据源（测试 / mock 模式）：token 永带 FAKE 标记，零真实凭据。"""

    def __init__(self, token="FAKE.session.token.NOT-A-REAL-CREDENTIAL",
                 expires_in=3600, clock=None):
        if "FAKE" not in token:
            raise ValueError("FakeCredentialSource refuses non-FAKE tokens")
        self.token = token
        self.expires_in = int(expires_in)
        self.clock = clock or SystemClock()
        self.mint_count = 0

    def mint(self, trigger=TRIGGER_HEADLESS_REFRESH, agent_session_id=None):
        t0 = time.time()
        self.mint_count += 1
        return MintedToken(self.token, self.expires_in, self.clock.now(),
                           trigger, latency_ms=int((time.time() - t0) * 1000))


def mint_contract_json(minted):
    """铸币 stdout 契约：单行 JSON（access_token + expires_in），无多余输出。"""
    return json.dumps({"access_token": minted.token,
                       "expires_in": minted.expires_in})


def parse_provider_output(text, trigger=TRIGGER_HEADLESS_REFRESH):
    """解析外部提供者 stdout → MintedToken（裸 token 或单行 JSON，二选一）。

    契约违规（空输出 / 多行 / 无法解析）抛 ``ValueError``。
    ``trigger`` 由调用方按本次契约（sign_in / headless_refresh）传入。
    """
    if text is None:
        raise ValueError("provider produced no output")
    lines = [ln for ln in str(text).strip().splitlines() if ln.strip()]
    if not lines:
        raise ValueError("provider produced empty output")
    if len(lines) > 1:
        raise ValueError("provider output must be a single line (got %d lines)"
                         % len(lines))
    line = lines[0].strip()
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ValueError("provider JSON payload is not valid JSON: %s" % exc)
        if not isinstance(obj, dict) or "access_token" not in obj:
            raise ValueError("provider JSON payload must contain access_token")
        token = obj["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("provider access_token must be a non-empty string")
        expires_in = obj.get("expires_in", 3600)
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            raise ValueError("provider expires_in must be an integer")
        if expires_in <= 0:
            raise ValueError("provider expires_in must be positive")
        return MintedToken(token, expires_in, time.time(), trigger)
    if any(ch.isspace() for ch in line):
        raise ValueError("bare token must be a single whitespace-free line")
    return MintedToken(line, 3600, time.time(), trigger)
