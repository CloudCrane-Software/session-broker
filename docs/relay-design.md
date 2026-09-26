# Higress 出站中继设计草案（C 路线：OAuth 中继）

> session-broker 仓 · 依据 PROP-0001 v1.7 §12.1（C 路线，**合规 ⚠️ 逐家 ToS 评审后才能激活**）。
> **本文档只做设计，不实现。** 本仓当前（MVP 治理壳）没有任何真实 OAuth 会话接入。
>
> 红线（进代码与文档）：**不得违反订阅条款使用 OAuth 会话**。
> （`session_broker.policy.REDLINE_TEXT`）

## 1. 目标与不做什么

**目标**：对"仅浏览器 OAuth 登录的 CLI"类厂商（如 grok），由 session-broker 持有登录态
（引用，密钥库落地）+ Higress 出站中继统一计量，使 jiuwenswarm 侧无需每任务人工登录。

**不做**（关键架构决策，来自 §12.1）：

- **不做"请求伪装"**。伪装（masquerade）能力技术上保留但**默认禁用**、逐家评审
  通过前不得开启（见 §4 配置约束）；
- 不在执行面落地任何长期登录态（执行面零长期密钥）；
- 不复制原生已有决策点：模型流量入口唯一（Higress，4.9 #7）；本中继只是
  Higress 的一个出站 listener + 插件组合，不是第二个网关。

## 2. 架构

```
jiuwenswarm (Code 模式)
   │  单一 OpenAI 兼容端点
   ▼
Higress ── listener: oauth-relay(<vendor>) ──▶ vendor 官方端点
   │      ├─ transform slot A: normalizer（响应/计费归一）
   │      └─ transform slot B: metering（按套餐维度打标、出报表）
   │
   ▲ 会话资格判定（控制面）
session-broker (本仓, srv-1)
   ├─ registry：VendorSession 登记（vendor / subject_ref / login_state_ref→密钥库路径）
   ├─ policy：红线硬门（ToS 评审 90 天内 approved 才可激活）
   ├─ audit：register/activate/revoke/deny/expire 事件流
   └─ OpenBao：登录态值唯一落地处（session-broker 只持引用，经短时租约下发）
```

数据流约定：

1. agent 请求仍指向 Higress 唯一入口；oauth-relay listener 按 vendor 路由；
2. 每次出站前，listener 向 session-broker 查询会话资格
   （`GET /sessions?vendor=<v>&state=ACTIVE`，只读；无 ACTIVE 会话 → 请求 403 短路，
   不触发任何登录动作）；
3. 登录态值由 OpenBao 短时租约（≤1h）直接注入 listener 进程内存，
   session-broker 与日志全程只见引用；
4. 响应经 normalizer 归一为统一 usage 记录（§3），metering 打标后落计量存储。

## 3. transform 插件槽：归一化与计量

两个插件槽，各自独立开关（默认全关，逐家评审后开启）：

### 3.1 normalizer（归一化）

- 输入：vendor 响应（各 CLI 私有格式）
- 输出：统一 usage 记录 schema：

```yaml
usage_record:
  vendor: <vendor>            # 会话登记的 vendor 名
  plan: <套餐标识>             # 计量维度之一
  subject_ref: <subject_ref>  # 会话登记的主体引用（非明文身份）
  session_id: <session_id>    # 回链 session-broker 登记册
  requests: <n>
  tokens_in: <n>
  tokens_out: <n>
  ts: <iso8601>
```

- 只做格式归一与字段提取，不改写请求语义；请求体不落盘。

### 3.2 metering（计量）

- 消费 usage_record，按 vendor × plan × subject_ref × 日 聚合；
- 输出报表对接运行手册"每周：套餐计量对账（12.1）"；
- 告警阈值：单会话用量异常（对账偏差 / 突刺）→ 治理面可见 + 建议 revoke。

## 4. 伪装能力：默认禁用（硬约束）

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `masquerade.enabled` | **false** | 任何 UA/指纹/请求改写伪装；逐家 ToS 评审通过前不得置 true |
| `masquerade.approval_ref` | （空） | 置 true 时必须携带 GuardrailRun 准入引用（guardrail://…），空值拒绝启动 |
| `relay.enabled` | false | 整个 oauth-relay listener 的总开关 |
| `relay.vendor` | （空） | 单厂商一个 listener；不允许通配 |
| `relay.ttl_hours` | 1 | 登录态内存驻留上限 |

**评审未通过的厂商：listener 不创建；会话停在 REGISTERED（registry 行为）；
两道门互为冗余。**

## 5. 逐家 ToS 评审清单模板

每个 vendor 一行，评审记录入 `ReviewStore`（`review_ref` 全局唯一），90 天有效：

| 字段 | 说明 |
| --- | --- |
| `review_ref` | 全局唯一，如 `tos-review/<vendor>/2026-Q4-01` |
| `vendor` | 厂商名（与会话登记一致） |
| `tos_version` | 评审所依据的条款版本号/存档链接 |
| `reviewed_at` | 评审日期（ISO 日期；90 天后过期须复审） |
| `decision` | approved / rejected |
| 评审要点 1 | 条款是否允许"程序化使用浏览器 OAuth 会话"（原文引用入证据） |
| 评审要点 2 | 是否允许会话共享/多机使用；并发与频率限制 |
| 评审要点 3 | 数据出域：请求内容经中继是否违反数据处理条款 |
| 评审要点 4 | 套餐计量口径：官方计费边界与本仓归一 schema 的映射 |
| 评审要点 5 | 伪装能力：本 vendor 是否允许（默认禁用；允许也不开启，除非书面确认） |
| `evidence_ref` | 评审证据归档 URI |

## 6. 与本仓实现的对应关系

| 设计件 | 本仓 MVP 现状 |
| --- | --- |
| VendorSession 登记 | `registry.py`（已实现，内存） |
| 红线硬门（90 天评审 / 无评审拒激活） | `policy.py`（已实现） |
| 审计事件流 | `audit.py`（已实现，append-only） |
| REST 治理接口 | `api.py`（已实现，stdlib http.server） |
| oauth-relay listener / transform 插槽 | **未实现**（本文档 §3/§4，M3 后逐家评审再动） |
| OpenBao 登录态租约下发 | **未实现**（依赖 WO-0005 治理） |
| 计量存储与对账报表 | **未实现**（对接 12.1 运行手册周检） |

## 7. 风险与回退

- ToS 变更：90 天复审 + 厂商条款变更监控（人工周检起步）→ 立即 revoke 全部该 vendor
  ACTIVE 会话（`POST /sessions/{id}/revoke`，幂等）；
- 中继故障：`relay.enabled=false` 一键回退；Code 模式始终可用（12.1：Code 模式是
  默认 harness，vendor provider 只是可插拔执行主体）；
- 登录态泄露：值只经 OpenBao→listener 内存短租约，不经 session-broker；审计只见引用。
