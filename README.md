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
