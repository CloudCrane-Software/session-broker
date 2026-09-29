# z.ai / BigModel Start Plan 端点研究纪要（纯代码研究，零调用）

> 线2 任务3，2026-09-29。方法：grep 官方开源仓 zai-org/ZCode（本机克隆
> `_tmp/harness-research/repos/ZCode`，commit `29628c9`，v3.14.3，2026-09-24），
> 只读源码，**未发起任何真实请求**（预算纪律 + 调研报告 §5 ToS 灰区结论：权益类接口
> 在官方客户端之外调用属灰区，本纪要只做代码事实登记）。
> 上游调研底料：`_tmp/harness-research/zcode-调研.md` §2a（X-Device-Mid 必带）——本纪要
> 是它的代码级展开与修正补充。
> 文件引用均相对仓库根，行号基于 `29628c9`。

## 1. Start Plan 权益查询：billing/balance（权威接口）

**端点**（`packages/shared/src/zcodeEndpoint.ts:269`）：

```
GET https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=<真实版本号>
```

- origin 可被 `ZCODE_BASE_URL` / `ZCODE_ENDPOINT_ORIGIN` 覆盖（`zcodeEndpoint.ts:148-149`）。
- `app_version` **必须带真实 app 版本**，注释明言"Start Plan balance 接口按真实
  app_version 判定能力；开发环境也不能固定 3.0.0，否则本地验证会绕过当前 App 版本的
  后端策略"（`packages/services/src/model-provider/zaiStartPlanBilling.ts:63-69`）。
- **`billing/current` 已废弃**：两处注释一致——"billing/current 已废弃，balance 会同时
  返回 plans 与 balances"（`codingPlanProviderAvailability.ts:411`、
  `usage-stats/providers/bigmodelUsageQuotaProvider.ts:375`）。

**鉴权头**：`Authorization: <zcode JWT>`（`zaiStartPlanBilling.ts:104-110`）。JWT 来源
（`packages/services/src/model-provider/bigmodelStartPlanZcodeJwt.ts:10-27`）：

- BigModel 侧：凭据键 `zcodejwttoken`（BigModel OAuth callback 阶段用授权码 body 落盘）；
  active provider 为 bigmodel 或显式信任缓存时读它；**不再**用 access_token 临时兑换
  （注释：避免 `/oauth/token` 400）；兜底 = provider 配置里的 apiKey 副本。
- Z.AI 侧：`resolveStartPlanAuthorization`（`codingPlanProviderAvailability.ts:440-461`）。

**X-Device-Mid 必带**（权益/计费类请求）：

- 头构造：`packages/shared/src/zcode-source-headers.ts:45-58`（deviceMid 存在才带）；
  值从 `getAppConfigDir()/telemetry-state.json` 的 `deviceMid` 字段读（进程内缓存），
  只复用不生成（`packages/services/src/providers/sourceHeaders.ts:41-63`）。
- **服务端硬拒实证（代码注释）**：`packages/server/src/stdioDeviceMid.ts:13-16`——
  "3.12.0 起 Start Plan 权益由远端自己查询，billing/balance 请求因缺 X-Device-Mid
  被服务端拒绝为 parameter error"。远端 stdio server 启动时会 ensureDeviceMid，与同机
  CLI/Desktop 共享同一设备身份文件。
- deviceMid 本体 = 客户端自生成 UUID（`packages/services/src/device/deviceMid.ts:1`，
  `:21-27` 存储），非硬件指纹；`deviceMid.ts:191`："跨端共享的设备身份：X-Device-Mid
  计费 header、反馈、onboarding 都读它"。
- 同请求还带全套 ZCode 来源头（`User-Agent: ZCode/<ver>`、`X-Title: Z Code@<surface>`、
  `X-ZCode-App-Version`、`X-Platform` 等，`zcode-source-headers.ts:45-58`）。

**响应信封字段**（`zaiStartPlanBilling.ts:14-56`，loose 解析）：

```
data.server_time: number（秒）
data.plans[]:     user_plan_id, plan_id, name, status("active"|…), starts_at, ends_at,
                  entitlements[]{entitlement_id, show_name, period, effective_at}
data.balances[]:  bucket_id, user_plan_id, plan_id, entitlement_id, show_name,
                  meter, unit_type, capabilities[]（"model:<modelId>" 形态）,
                  total_units, used_units, reserved_units, remaining_units,
                  available_units, period_start, period_end, expires_at
```

- **模型可用性从 balance 推导**：`capabilities[]` 里 `model:` 前缀项 → 规范化 modelId；
  无 capabilities 时回退 `show_name`（`resolveZaiStartPlanBalanceModelIds`，
  `zaiStartPlanBilling.ts:129-155`）。
- **过期归一**：响应 HTTP `Date` 头与 `data.server_time` 配对判"现在"；`status=active`
  但 `ends_at <= now` 的 plan 就地降级 `expired`，其归属 balances 被过滤（防止旧 JSON
  让过期记录继续供权益，`normalizeStartPlanExpiry`，`:157-188`）。
- 请求侧防抖：同 (authorization, url) 在飞请求复用；响应保留 ≥1s 才驱逐（429 后不立刻
  重复打，`:58-127`）。
- 静态预览配置（未领取时的展示）：`StartPlanPreviewEntitlement{grantUnits, meter,
  period, showName, unitType}`（`packages/shared/src/coding-plan-subscription.ts:103-115`）。

**配额快照消费方**：`usage-stats/providers/bigmodelUsageQuotaProvider.ts:351-452`
`getStartPlanSnapshot()` → remaining/subscription/quota 三视图，同样走 balance 单接口。

## 2. Start Plan 的模型请求路径（与 Coding Plan 同门）

- 网关基础 URL（`zcodeEndpoint.ts:267-268`）：`{origin}/api/v1/zcode-plan`（OpenAI 形）/
  `{origin}/api/v1/zcode-plan/anthropic`（Anthropic 形）。
- Coding Plan 流量强制改道平台网关（`apps/zcode-cli/packages/adapters/src/model/
  official-coding-plan-gateway.ts:22-31`，调研 §2c 已核）：`open.bigmodel.cn/api/anthropic`
  → `zcode.z.ai/api/v1/ultra/anthropic/v1/messages`（Z.AI 同理 ultra-zai）。
- Start Plan 特有：`accountAccess.mode == "start-plan"` 且 providerKind 为
  openai-compatible 时**才包含流式响应体**（`apps/zcode-cli/packages/adapters/src/model/
  runner-options.ts:20-24`）——Start Plan 走 OpenAI 兼容形而非 Anthropic 形的旁证。
- planKind 三值：`"start-plan" | "individual-coding-plan" | "team-coding-plan"`
  （`codingPlanProviderAvailability.ts:98`）。Start Plan 是**独立权益**：即使当前连接是
  个人/团队 Coding Plan 也要单独查询 balance（`:190-193` 注释）。

## 3. 闲时（Off-Peak）子系统——与 Start Plan 的关系（重要边界）

代码位置：`packages/services/src/session/offPeak*.ts` + `packages/shared/src/off-peak-types.ts`。

**服务端五接口**（`offPeakServerClient.ts:1-4,165,212-274`；base = `{zcode origin}/api/v1/off-peak`）：

| 接口 | 方法+路径 | 要点 |
| --- | --- | --- |
| 取号资格 | `GET /ticket/availability` | `can_take_number` + `next_take_at`；false 必带 next_take_at |
| 取号 | `POST /ticket` `{task_id}` | 返回 `ticket_id/state/position/next_poll_after(秒)/queued_at/ready_deadline` |
| 批量状态 | `POST /ticket/status` `{ticket_ids[]}` | ≤100 条；state ∈ queued/ready/active/expired/settled/not_found |
| 结算 | `POST /ticket/{id}/settle` | 幂等，重复/未知票一律 2xx |
| 模型调用 | **不走这里** | "messages 调模型不走这里（由 idle plan per-turn provider 在 agent 进程内直连）"（`:1-4`） |

**逐请求双凭证**（`offPeakRuntimeModel.ts:225-260`）：

```
Authorization: Bearer <zcodejwttoken JWT>          # apiKey 字段也是 JWT（:250-252）
X-Coding-Plan-Api-Key: <套餐 API key>
X-Off-Peak-Ticket-ID: <ticketId>                    # run 作用域（validation.ts:404）
# Team 追加：bigmodel-organization / bigmodel-project（半套身份头禁发，:225-240）
```

- **闲时通道明确不支持 Start Plan**：`offPeakRuntimeModel.ts:98-100`——
  `planKind === "start-plan"` → 抛 `OffPeakCodingPlanUnavailableError("start_plan_not_supported")`。
  支持矩阵 = 个人/团队 × zai/bigmodel 四种 kind（`:101-118`）。
- 双凭证一致性校验：zcode JWT 必须与选中 provider family 一致，否则
  `provider_identity_mismatch`（`:157-167`，防"ZAI JWT + BigModel key"拼接）。
- 错误码：3101 无资格 / 3103 取号超限（`offPeakServerClient.ts:114`）+ 3006 兜底
  （"准入/低峰判断仍以服务端为准（3006 兜底）"，`coding-plan-subscription.ts:126`）。
- 入口开关：`enable_offpeak_task === true` 且 Built-in 模型成员非空（
  `coding-plan-subscription.ts:127` 注释）；开发 mock：`ZCODE_OFFPEAK_MOCK=1`
  （+`ZCODE_OFFPEAK_MOCK_NO_PLAN=1`，`offPeakRuntimeModel.ts:139-151`）。
- 票据过期标记：`off-peak-ticket-expired`（`off-peak-types.ts:38`，适配层据此重试/重取号）。

## 4. 对本仓（线 B：官方 CLI 做执行主体）的含义

1. **无需直调这些接口**：route-B 下官方 CLI 自己完成权益查询/网关改道/计量——上述
   端点仅作对账与排障参考（如需自建配额看板，balance 单接口 + X-Device-Mid + zcode JWT
   就是全部契约；但**在官方客户端之外调用属 ToS 灰区**，调研 §5 结论不变，本仓不做）。
2. **闲时策略是调度层的**：官方闲时通道（ticket 制）是 GUI/服务端调度面功能且明确
   排斥 Start Plan；我们账号是 BigModel 个人 Coding Plan（在支持矩阵内），但 provider
   侧不碰 ticket API，只在**非高峰墙钟窗口**（周一至五 14:00–18:00 UTC+8 之外，官方
   文档 50% 抵扣口径）派长跑任务——README「闲时任务策略」节与
   company-ops `ops/harness-routing.yaml` 的 `zcode-longrun-offpeak` 路由。
3. **X-Device-Mid 依赖是 CLI 内部的**：同机共享 `~/.zcode/v2/telemetry-state.json`，
   官方 CLI spawn 即自带；任何"复制凭据到别的机器"的思路会同时缺 deviceMid + 触发
   服务端风控（调研 §6.3），进一步坐实线 B 是唯一稳妥形态。

## 5. 未核验项（如实）

- `ultra` 网关服务端判定逻辑：客户端源码不可见（NOTICE.md:39 明言），仅知请求形态。
- Start Plan 与个人 Coding Plan 能否并存扣减顺序：代码只证"独立权益、单独查询"，
  扣减优先级无客户端证据。
- `X-Off-Peak-Ticket-ID` 的服务端配额核销算法：纯服务端，无客户端证据。
