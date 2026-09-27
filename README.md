# session-broker

原子会话能力（atomic-session）。一句话：**会话与工作区生命周期的原子经纪：创建 / 挂起 / 恢复 / 交接 / 复用。**

## 定位

- 《建设方案-多Agent系统与GitOps》（PROP-0001）第 8 节团队分工中 `atomic-session` 角色的承载仓库；随 M3"原子能力开源"（WO-0008）交付。
- 与 openJiuwen 原生的边界：会话内上下文压缩、todo 等运行时能力归原生（第 4.9 节 #5 / #10）；本仓库只做**跨任务会话资产的原子化经纪**，不复制原生已有决策点。
- v1.7 §12.1 C 路线（OAuth 中继，**合规 ⚠️ 逐家 ToS 评审后才能激活**）：本仓承担其**治理壳**——登记 / 红线 / 审计；出站中继设计见 `docs/relay-design.md`（**只设计，未实现**）。

## 红线（C 路线治理核心）

> **不得违反订阅条款使用 OAuth 会话**
> （`session_broker.policy.REDLINE_TEXT`，代码/文档/拒绝消息三处一致）

- `activate()` 无 `review_ref`、或 ToS 评审缺失 / 未批准 / 过期（90 天）→ **拒绝**，会话状态停在 `REGISTERED`；
- vendor 无 ToS 评审记录 → 永远无法激活；
- `login_state_ref` 只收**密钥库路径引用**（URI 形式，如 `openbao://secret/sessions/<vendor>/<subject>`），裸凭据值在登记口即被拒收；
- 审计事件只含引用与字段名，任何密钥值不进审计。

## 现状（MVP 治理壳：无任何真实 OAuth 会话接入）

| 组件 | 说明 |
| --- | --- |
| `src/session_broker/registry.py` | `VendorSession`（vendor / subject_ref / login_state_ref / review_ref / state ∈ REGISTERED→ACTIVE→REVOKED/EXPIRED / activated_at / expires_at）+ register/activate/revoke/list/sweep_expired；activate/revoke 幂等 |
| `src/session_broker/policy.py` | 红线硬门 `check_activation`（无评审拒 / 无记录拒 / vendor 不符拒 / 未批准拒 / **>90 天过期拒**）+ `ReviewStore`（ToS 评审记录册，90 天有效） |
| `src/session_broker/audit.py` | append-only 审计事件（register/activate/revoke/deny/expire），每事件含 actor/subject/reason/ts，可选 JSONL sink |
| `src/session_broker/api.py` | stdlib `http.server` 最小 REST：`GET /health`、`GET/POST /sessions`、`POST /sessions/{id}/activate`、`POST /sessions/{id}/revoke`、`POST /reviews`；JSON 错误体；**请求日志只记字段名不记值** |
| `providers/zcode/` | ZCode CLI provider（spawn 家族）+ B-1 本地 OpenAI 兼容 HTTP 壳 + pytest（全 mock）；真实调用与配额待遇证据见其 README 文末 |
| `docs/relay-design.md` | Higress 出站中继设计草案：transform 插件槽（归一化/计量）、伪装能力默认禁用、逐家 ToS 评审清单模板——**只设计不实现** |
| `tests/` | pytest：红线门 ≥6 类拒绝路径 + 幂等 + 到期 + API dispatch 分支 + 真实端口端到端 + 日志不泄值 |

## 用法

```bash
python -m pytest            # 全绿

# 就地起一个治理 API（默认 127.0.0.1，无 TLS——生产前置逆代/TLS，见 relay-design.md）
PYTHONPATH=src python -c "
from session_broker.api import BrokerApp, make_server
srv = make_server(BrokerApp(), '127.0.0.1', 8765)
srv.serve_forever()
"
```

零第三方依赖：Python 3.9+ 标准库即可运行。

## broker/ 运行时框架（vendor 无关，首版）

治理壳管"**能不能用**"（红线门/登记册/审计），`src/session_broker/broker/` 管"**怎么安全地续着用**"：

| 组件 | 说明 |
| --- | --- |
| `broker/clock.py` | `Clock`/`SystemClock`/`MockClock`——调度全部经时钟注入，可压缩时间仿真 |
| `broker/cell.py` | `CredentialCell`：目录即隔离边界 + 按 cell 命名的 leader socket + **仅引用**的元数据（凭据值零落盘）+ 0600 等价 ACL |
| `broker/scheduler.py` | `RefreshScheduler`：**刷新提前量 < TTL 构造级硬约束**（防"一出生就在刷新窗口"死循环）、401 新铸即拒 30s 环路保护、1→60s 有界退避 |
| `broker/audit.py` | `CredentialAudit`：字段白名单 + `key_prefix` 指纹化（sha256 前 12 位 + 长度）；键名含 token/secret 直接拒收；append-only |
| `broker/minting.py` | 外部认证提供者 stdout 铸币契约（单行 token 或单行 JSON）+ `FakeCredentialSource`（FAKE-only）+ 契约违规解析拒绝 |
| `broker/health.py` | `BrokerHealth`：cell/调度器只读快照 → `GET /broker/health`（未配置返回 501） |
| `broker/simulate.py` | `run_refresh_cycle`：MockClock 驱动的长周期刷新仿真，窗口/间隔/提前量三条不变量逐条断言 |

**vendor 边界**：各厂商的专属适配（配置注入点、home 变量、leader 参数、登录流程）不在本仓——
vendor adapter 在私有运维仓实现（例：grok vendor adapter 见私有仓；
红线=**不得违反订阅条款使用 OAuth 会话**）。本仓只定义 adapter 必须遵守的抽象与契约。

7 天 TTL 压缩验证（无真实登录，毫秒级）::

    from session_broker.broker import (MockClock, RefreshScheduler,
                                       FakeCredentialSource, run_refresh_cycle)
    clk = MockClock(start=1_700_000_000.0)
    sched = RefreshScheduler(ttl_seconds=7*86400, lead_seconds=300, clock=clk)
    src = FakeCredentialSource(expires_in=7*86400, clock=clk)
    report = run_refresh_cycle(sched, clk, src, total_seconds=70*86400)
    assert len(report.refresh_epochs) == 10   # 70 天 / 7 天，每次都落在窗口内


## 会话状态机

```
REGISTERED ──activate(过红线门)──▶ ACTIVE ──revoke──▶ REVOKED
    │ revoke                        │ sweep_expired（TTL 到期）
    ▼                               ▼
 REVOKED                         EXPIRED        （终态：重复撤销 = 幂等无操作）
```

## 边界（如实）

- 本仓**不接入任何真实 OAuth 会话**：没有厂商登录流程、没有登录态读写、没有 Higress 配置；
- 登记册/评审册为内存实现（MVP）；持久化与 OpenBao 租约下发未做；
- 激活任何厂商前必须走 `docs/relay-design.md` §5 的逐家 ToS 评审清单，`decision=approved` 且 90 天内；
- Code 模式始终是默认 harness；本中继属可插拔执行主体路径（12.1）。

## 状态

- ~~M0 骨架（README / LICENSE / .gitignore)~~
- **M3/WO-0008（本次）：治理壳 MVP（registry/policy/audit/api）+ 中继设计草案 + pytest 全绿；C 路线保持 ⚠️ 未激活。**

## License

Apache-2.0
