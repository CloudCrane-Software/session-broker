# Qoder CN provider（B-1 本地 OpenAI 兼容层）

## 1. 这是什么

把已安装的 Qoder CN CLI（命令名 `qodercn`，不是 `qoder`）包成一个本机 OpenAI 兼容 HTTP 服务。jiuwenswarm 不改代码，只把 `model_client_config.api_base` 指到这里。

这是接入手册里的 **B-1 零侵入** 薄层：`POST /v1/chat/completions` 收到 OpenAI 形状的请求后，串行调用

```text
qodercn -p "<prompt>" --model Qwen3.8-Flash --output-format json --no-session-persistence
```

再把 CLI 的单行 JSON（`type=result`）映射成 `chat.completion`。实现只用 Python 标准库（`http.server`），没有第三方依赖。扩展计量放在响应的 `qoder` 对象里：`total_credits`、`total_cost_usd`、`session_id`、`duration_ms`。

## 2. 启动

在 session-broker 仓库根目录：

```bash
python providers/qoder/server.py
```

默认监听 `http://127.0.0.1:8123/v1`。启动时打印一行：

```text
qoder provider listening on http://127.0.0.1:8123/v1
```

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `QODER_PROVIDER_HOST` | `127.0.0.1` | 监听地址 |
| `QODER_PROVIDER_PORT` | `8123` | 监听端口 |
| `QODER_BIN` | 调用时 `shutil.which("qodercn")` | CLI 可执行文件；未设置则每次请求再解析 PATH |
| `QODER_MODEL` | `Qwen3.8-Flash` | 请求体没带 `model` 时使用的默认模型 |
| `QODER_TIMEOUT_S` | `180` | 单次 CLI 超时（秒）。首包实测约 40s，默认留足余量 |
| `QODER_LOG_DIR` | `providers/qoder/logs` | 请求日志目录，写入 `requests.jsonl` |

日志每行一个 JSON，字段只有 `ts`、`remote`、`model`、`prompt_chars`、`status`、`subtype`、`total_credits`、`total_cost_usd`、`duration_ms`、`error`。不记录 prompt 原文、argv 或 message 内容。

最小调用：

```bash
curl -s http://127.0.0.1:8123/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"Qwen3.8-Flash\",\"messages\":[{\"role\":\"user\",\"content\":\"只回复一个字：好\"}]}"
```

健康检查：`GET /healthz`。模型列表：`GET /v1/models`。

## 3. jiuwenswarm 接入

B-1 零侵入：jiuwenswarm 侧把本服务当成普通 OpenAI 兼容端点。出处：`_tmp/harness-research/01-接入执行手册.md` §2.7。

```yaml
model_client_config:
  api_base: http://127.0.0.1:8123/v1
  client_provider: OpenAI
  model: Qwen3.8-Flash
```

`api_base` 要带 `/v1`，客户端会自己拼 `/chat/completions`。

## 4. 免费窗口说明

`Qwen3.8-Flash` 在 **2026-09-30 23:59:59** 前免费。实测成功响应里 `total_credits=0`、`total_cost_usd=0`（这两个字段会原样出现在 HTTP 响应的 `qoder` 对象和 `requests.jsonl` 里）。证据：

- 本服务请求日志：`providers/qoder/logs/`
- 实跑样本：`_tmp/harness-research/verify/qoder/logs/run1-live.json`

窗口关闭后按正常 Credits 计费。每日 100 Credits 需要在 Qoder 桌面端领取，CLI 不会自动领。窗口结束后路由应改为按需，见 CNB company-ops 的 `ops/harness-routing.yaml`。

## 5. 已知边界

- CLI 登录必须人工做一次：`qodercn login`（设备码页面大约 5 分钟有效）。未登录时 CLI 直接失败，本服务返回 502，`error.code=qodercn_failed`。
- `QODER_OPENAPI_BASE_URL` / `QODER_AUTH_BASE_URL` / `QODER_INFER_BASE_URL` / `QODER_TELEMETRY_BASE_URL` 改道无效。CLI 1.1.64 的端点引导是硬编码的，环境变量不参与解析。见 runbook §4（`_tmp/harness-research/verify/qoder/runbook-片段.md`）。
- 服务端用 `CALL_LOCK` 把 CLI 调用串行化。qodercn 并发跑会抢同一份登录态，不要为了吞吐拆掉这把锁。
- 找不到 `qodercn` 时返回 500，`error.code=provider_binary_missing`（配置错误，不是上游失败）。
- 非 success、`is_error: true`、stdout 无法解析、超时，一律 502 `qodercn_failed`。未捕获异常返回 500，响应体不带 traceback（traceback 打到进程 stderr）。

## 6. 测试

全部 mock，不会真的调用 `qodercn`：

```bash
python -m pytest tests/test_qoder_provider.py tests/test_qoder_server.py
```
