# providers/zcode — ZCode CLI provider（spawn 家族）+ B-1 OpenAI 兼容壳

把官方 zcode CLI（无头模式）包装成可被任意 OpenAI-chat 客户端 / agent harness 复用的
provider。由 route-B 验证脚本升级而来（2026-09-26/27 端到端 PASS，含真实模型调用与
配额归属验证，见文末「证据」）。2026-09-29 线2 产品化：**长跑模式**（`--mode`
build/edit/plan/yolo 成为一等参数）+ **stream-json 事件流**（`run_prompt_streaming`
增量消费 + `GET /v1/events` 事件环）+ **闲时任务策略**（本 README「闲时」节与
company-ops 仓 `ops/harness-routing.yaml` 的 `zcode-longrun-offpeak` 路由草案）。

## 组成

| 文件 | 说明 |
| --- | --- |
| `zcode_provider.py` | provider 主体：`ZCODE_ADAPTER`（spawn 形态启动知识）、`build_turn_command`（含 `--mode` 参数）、`run_prompt()`（spawn CLI → 解析 NDJSON → 结构化结果）、`run_prompt_streaming()`（Popen 增量消费 stream-json，逐事件 `on_event` 回调）、`ZcodeHarness`/`ZcodeHarnessProvider`（duck-typed 工厂 SPI：card + create(config)） |
| `adapter_server.py` | B-1 本地 HTTP 适配层（stdlib `http.server`）：`POST /v1/chat/completions`（非流式 JSON；`stream=true` 走单 chunk SSE 兜底）、`GET /v1/models`、`GET /v1/events`（NDJSON 事件环）、`GET /healthz` |
| `tests/` | pytest（全 mock，不 spawn 真 CLI，28 项）：启动命令形态（含 mode 透传/校验）/ result 终止事件解析 / 流式增量消费（fake Popen）/ 超时 kill / usage 映射 / system 消息丢弃 / 会话续接与分叉 / HTTP 线路端到端 / 日志脱敏 |

## 快速开始

```bash
# 前置：官方 zcode CLI 在 PATH（zcode-app-cli >= 3.14），且已登录、
# 已设置默认模型（新机一次性：TUI /model 选定并存为共享默认，
# 否则无头新会话报 "Select a model before continuing"）。

python -m pytest providers/zcode/tests -q        # mock 全绿

# 就地起 B-1 适配层（默认 127.0.0.1:8123，默认 mode=yolo）
python providers/zcode/adapter_server.py &

curl -s http://127.0.0.1:8123/v1/models
curl -s http://127.0.0.1:8123/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"只回复一个字：好"}],"mode":"build"}'
curl -s http://127.0.0.1:8123/v1/events          # NDJSON 事件环（仅元数据）
```

jiuwenswarm 侧零代码接入：

```yaml
model_client_config:
  model_name: GLM-5.3
  api_base: http://127.0.0.1:8123/v1
  api_key: local-adapter-placeholder   # 仅非空校验
  client_provider: OpenAI
  verify_ssl: false                    # 本地 http；openjiuwen 0.1.16 校验 verify_ssl=True 必须给 ssl_cert
```

环境变量：`ZCODE_ADAPTER_HOST/PORT/MODEL/MODE/CWD/TIMEOUT`（默认 127.0.0.1 / 8123 /
GLM-5.3 / yolo / 进程 cwd / 300s）。直接用 provider 的话：

```python
from zcode_provider import run_prompt, run_prompt_streaming, ZcodeHarnessProvider
res = run_prompt("只回复一个字：好", timeout_s=180)   # -> ZcodeRunResult(ok/response/session_id/usage/...)
res2 = run_prompt_streaming("长任务", mode="build", on_event=print)  # 逐事件实时回调
agent = ZcodeHarnessProvider.create({"mode": "yolo"})  # card + create(config) 工厂 SPI
out = agent.turn("继续")                               # 自动 --resume 上次的 zcode 会话
out2 = agent.turn("重开任务", mode="plan", stream=True)  # 换模式 + 流式；事件见 agent.last_stream_events
```

## 线路契约与映射决策（证据优先）

CLI 线路（zcode-app-cli 3.14.3，官方源码 repos/ZCode @ 29628c9）：

```
zcode [--resume <sid>] --mode <build|edit|plan|yolo> -p <task> --output-format stream-json
  → stdout NDJSON；终止行 {"type":"result", sessionId, traceId, response, usage, projection}
  （run.ts:42,115；arguments.ts:15（--mode 四值）；prompt-command.ts:328,347-359）
```

- **messages → prompt**：非 system 消息拍平加角色前缀；system 消息**有意丢弃**——
  agent 系统提示词归 zcode 自身，这正是 B-1 设计（zcode 在 OpenAI 门面后充当 harness）。
- **会话复用**：适配层内存中保一个 zcode sessionId；首轮创建、后续 `--resume`，
  供应商侧 prompt 缓存跨请求命中。注意：当前版 CLI `--resume` 会分叉出新 sessionId
  （缓存仍命中）。请求可带非标准字段 `"session": "new"` 强制重开会话（长跑任务边界）。
- **长跑 mode**：请求可带非标准字段 `"mode": "build"|"edit"|"plan"|"yolo"`（官方
  `--mode` 四值，arguments.ts:15），非法值 400；缺省取 `ZCODE_ADAPTER_MODE`（yolo，
  即官方无头默认）。yolo=无人值守自动批准（长跑主力）；build=常规监督；plan=只读规划；
  edit=聚焦编辑。**无人值守长跑必须用 yolo**——build/plan 会在权限确认处阻塞。
- **事件流**：默认走 `run_prompt_streaming()`（Popen 逐行消费 stream-json），每个事件
  的元数据（type/session_id/ts，**不含 prompt/response 内容**）进 500 条环形缓冲，
  `GET /v1/events` 以 NDJSON 返回，回合结束追加 `turn.completed`（含 ok）终结行。
- **usage 映射**：`inputTokens→prompt_tokens`、`outputTokens→completion_tokens`、
  `totalTokens→total_tokens`；cache/reasoning 计数放 `usage.zcode_usage` 留证。
- **凭据**：本目录代码不读 `~/.zcode`、不持任何密钥；被 spawn 的官方 CLI 用用户自身
  登录态（与 GUI 同一份），这是配额待遇保留的结构性论证。

## 闲时任务策略（线2 增补，2026-09-29）

**策略：带「长跑」标签的工单应排在官方非高峰窗口派给 zcode provider，窗口内长跑收益最大。**

依据（均 doc 来源，已核对）：

1. **官方计量口径**：BigModel Coding Plan 文档（docs.bigmodel.cn/cn/coding-plan/overview，
   2026-09-26 抓取）——非高峰（**周一至五 14:00–18:00 UTC+8 之外**）按基础积分消耗的
   **50% 抵扣**。即同样的 token 消耗，窗口内只扣一半积分。
2. **官方闲时（Off-Peak）子系统实锤**（开源仓代码研究，见
   `docs/zcode-startplan-notes.md`）：官方在 zcode.z.ai 有完整闲时任务 API
   （`/api/v1/off-peak/ticket*` 取号/状态/结算 + `X-Off-Peak-Ticket-ID` 请求头 +
   JWT/Coding-Plan-Key 双凭证），**闲时通道明确不支持 Start Plan**
   （offPeakRuntimeModel.ts:98-100 `start_plan_not_supported`），个人/团队 Coding Plan
   在支持矩阵内。
3. **本 provider 的边界（如实）**：本 provider 不调用官方 off-peak ticket API——
   那是 GUI/服务端调度面的功能，且官方注释明言准入判断以服务端为准。我们的闲时策略是
   **调度层**的：把长跑工单排进非高峰墙钟窗口派给官方 CLI，让套餐自身的 50% 抵扣与
   150%（2/3 消耗）计量待遇天然生效。route-B e2e 已实证配额按 Coding Plan 原生待遇扣减
   （见「证据」节）。
4. **路由规则草案**：工单含「长跑」标签 → 走 zcode provider（B-1 通道），窗口 =
   周一至五 14:00–18:00 UTC+8 **之外**；已增补进 company-ops 仓
   `ops/harness-routing.yaml`（`zcode-longrun-offpeak` 路由，mode=window_first）。
   约束：CLI 共享登录态必须串行（适配层 LOCK）；影子期纪律——周报上线前该路由只记录
   不生效（决策域 status=shadow 口径）。

## 边界（如实）

- 同步一次性 turn 已实现；`send/abort` 异步流式面是标注的生产化工作项（NotImplementedError）；
  流式能力仅到"逐事件实时回调/事件环"（`run_prompt_streaming`/`GET /v1/events`），
  OpenAI `stream=true` 仍为回合完成后的单 chunk SSE 兜底；
- `pause_resume/checkpoint/steer/abort` 能力位为 False（CLI 无长驻 stdin 协议，属
  spawn/re-invoke 家族，同 codex-exec 先例）；
- 并发：`/v1/chat/completions` 内部有锁串行化 zcode turn（会话状态有状态性决定）；
- API 服务器无 TLS，仅监听 127.0.0.1——生产前置逆代/TLS。

## 证据（真实调用 + 配额待遇）

- 2026-09-26 route-B e2e PASS：`zcode --resume` 真实调用，result 事件 + usage
  （input 22649 / output 35 / cacheRead 15616）+ 客户端 DB model_usage 计量行齐备。
- 2026-09-27 B-1 经 jiuwenswarm 驱动 e2e PASS：DeepAgent 返回"好"；配额按 Coding Plan
  原生待遇扣减——**150%（2/3 消耗）待遇保留**：jw 驱动行与原生行同池同线性计价曲线
  （双窗校准 8,245~8,310 tokens/配额单位），5h 窗实测 Δ=+4 单位恰为 2 次真实调用。
  全量证据与复现步骤见 `_tmp/harness-research/verify/zcode/jw-e2e/结论.md`（本机工作区）；
  日常前后对账用 company-ops 仓 `ops/scripts/quota-reconcile/`。
- 2026-09-29 线2 产品化：mock 测试 14→28 全绿（本仓 CI 口径裸 pytest 119 passed，
  floor ≥106）；`--mode`/事件流为纯代码升级，未发起新的真实调用（预算纪律：每平台
  ≤2 次真实调用，本轮 0 次）。
