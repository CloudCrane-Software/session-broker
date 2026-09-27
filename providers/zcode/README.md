# providers/zcode — ZCode CLI provider（spawn 家族）+ B-1 OpenAI 兼容壳

把官方 zcode CLI（无头模式）包装成可被任意 OpenAI-chat 客户端 / agent harness 复用的
provider。由 route-B 验证脚本升级而来（2026-09-26/27 端到端 PASS，含真实模型调用与
配额归属验证，见文末「证据」）。

## 组成

| 文件 | 说明 |
| --- | --- |
| `zcode_provider.py` | provider 主体：`ZCODE_ADAPTER`（spawn 形态启动知识）、`build_turn_command`、`run_prompt()`（spawn CLI → 解析 NDJSON → 结构化结果）、`ZcodeHarness`/`ZcodeHarnessProvider`（duck-typed 工厂 SPI：card + create(config)） |
| `adapter_server.py` | B-1 本地 HTTP 适配层（stdlib `http.server`）：`POST /v1/chat/completions`（非流式 JSON；`stream=true` 走单 chunk SSE 兜底）、`GET /v1/models`、`GET /healthz` |
| `tests/` | pytest（全 mock，不 spawn 真 CLI）：启动命令形态 / result 终止事件解析 / usage 映射 / system 消息丢弃 / 会话续接 / HTTP 线路端到端 / 日志脱敏 |

## 快速开始

```bash
# 前置：官方 zcode CLI 在 PATH（zcode-app-cli >= 3.14），且已登录、
# 已设置默认模型（新机一次性：TUI /model 选定并存为共享默认，
# 否则无头新会话报 "Select a model before continuing"）。

python -m pytest providers/zcode/tests -q        # mock 全绿

# 就地起 B-1 适配层（默认 127.0.0.1:8123）
python providers/zcode/adapter_server.py &

curl -s http://127.0.0.1:8123/v1/models
curl -s http://127.0.0.1:8123/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"只回复一个字：好"}]}'
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

环境变量：`ZCODE_ADAPTER_HOST/PORT/MODEL/CWD/TIMEOUT`（默认 127.0.0.1 / 8123 / GLM-5.3 /
进程 cwd / 300s）。直接用 provider 的话：

```python
from zcode_provider import run_prompt, ZcodeHarnessProvider
res = run_prompt("只回复一个字：好", timeout_s=180)   # -> ZcodeRunResult(ok/response/session_id/usage/...)
agent = ZcodeHarnessProvider.create({})             # card + create(config) 工厂 SPI
out = agent.turn("继续")                              # 自动 --resume 上次的 zcode 会话
```

## 线路契约与映射决策（证据优先）

CLI 线路（zcode-app-cli 3.14.3，官方源码 repos/ZCode @ 29628c9）：

```
zcode --resume <sid> -p <task> --mode yolo --output-format stream-json
  → stdout NDJSON；终止行 {"type":"result", sessionId, traceId, response, usage, projection}
  （run.ts:42,115；prompt-command.ts:328,347-359）
```

- **messages → prompt**：非 system 消息拍平加角色前缀；system 消息**有意丢弃**——
  agent 系统提示词归 zcode 自身，这正是 B-1 设计（zcode 在 OpenAI 门面后充当 harness）。
- **会话复用**：适配层内存中保一个 zcode sessionId；首轮创建、后续 `--resume`，
  供应商侧 prompt 缓存跨请求命中。注意：当前版 CLI `--resume` 会分叉出新 sessionId
  （缓存仍命中）。
- **usage 映射**：`inputTokens→prompt_tokens`、`outputTokens→completion_tokens`、
  `totalTokens→total_tokens`；cache/reasoning 计数放 `usage.zcode_usage` 留证。
- **凭据**：本目录代码不读 `~/.zcode`、不持任何密钥；被 spawn 的官方 CLI 用用户自身
  登录态（与 GUI 同一份），这是配额待遇保留的结构性论证。

## 边界（如实）

- 同步一次性 turn 已实现；`send/abort` 异步流式面是标注的生产化工作项（NotImplementedError）；
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
