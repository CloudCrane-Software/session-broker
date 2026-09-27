# coding: utf-8
"""session_broker.broker——vendor 无关的凭据 cell / 刷新调度 / 铸币契约运行时.

治理壳（registry/policy/audit/api）管"能不能用"；本包管"怎么安全地续着用"：

- :mod:`.clock`   —— Clock / MockClock（压缩时间仿真）
- :mod:`.cell`    —— CredentialCell（目录隔离 + leader socket 隔离 + 仅引用元数据）
- :mod:`.scheduler` —— RefreshScheduler（提前量 < TTL 硬约束、401 防环路、有界退避）
- :mod:`.audit`   —— CredentialAudit（字段白名单 + token 指纹化，append-only）
- :mod:`.minting` —— 铸币 stdout 契约 + FakeCredentialSource（FAKE-only）
- :mod:`.health`  —— BrokerHealth（只读健康快照，挂 /broker/health）
- :mod:`.simulate` —— run_refresh_cycle（MockClock 压缩验证长周期刷新节律）

vendor 专属适配（配置注入点、home 变量名、leader 参数传递）不在本包——
各 vendor adapter 私有仓实现；本包只定义它们必须遵守的抽象与契约。
红线：不得违反订阅条款使用 OAuth 会话；本包零真实凭据、token 只以指纹进日志。
"""
from .audit import AUDIT_FIELDS, TOKEN_KEYS, CredentialAudit, key_prefix
from .cell import CredentialCell, restrict_perms
from .clock import Clock, MockClock, SystemClock
from .health import BrokerHealth
from .minting import (FakeCredentialSource, MintedToken, REDACTED,
                      TRIGGER_HEADLESS_REFRESH, TRIGGER_SIGN_IN,
                      mint_contract_json, parse_provider_output)
from .scheduler import (DEFAULT_BACKOFF, DEFAULT_LEAD_SECONDS,
                        DEFAULT_FRESH_REJECT_SECONDS, RefreshScheduler)
from .simulate import CycleInvariantError, CycleReport, run_refresh_cycle

__all__ = [
    "AUDIT_FIELDS", "TOKEN_KEYS", "CredentialAudit", "key_prefix",
    "CredentialCell", "restrict_perms",
    "Clock", "MockClock", "SystemClock",
    "BrokerHealth",
    "FakeCredentialSource", "MintedToken", "REDACTED",
    "TRIGGER_SIGN_IN", "TRIGGER_HEADLESS_REFRESH",
    "mint_contract_json", "parse_provider_output",
    "DEFAULT_BACKOFF", "DEFAULT_LEAD_SECONDS", "DEFAULT_FRESH_REJECT_SECONDS",
    "RefreshScheduler",
    "CycleInvariantError", "CycleReport", "run_refresh_cycle",
]
