# providers/kimi-review — Kimi（K2.8 Preview）N 路并行审查器（spawn 家族）

把官方 Kimi Code CLI（`kimi.exe`，无头模式）按**同一 diff、N 个独立视角**并行拉起 N 路
审查，聚合为多数裁决（N=3：2/3 多数）。线 A（Kimi 接入）交付物，2026-09-29。

模型结论先行（证据见下「模型名」节）：**别名 `kimi-code/kimi-for-coding` 即
K2.8 Preview**（display_name 实证，1M 上下文），`kimi-code/k2.8-preview` 不是合法别名。

## 组成

| 文件 | 说明 |
| --- | --- |
| `review.py` | 主体：`build_review_prompt`（视角镜头 + 严格 JSON 契约）、`run_kimi`（spawn CLI → 解析 stream-json NDJSON）、`extract_verdict`（平衡括号 JSON 提取 + schema 校验/矫正）、`run_route`（单路）、`aggregate`（多数裁决 + 降级语义）、`run_review`（ThreadPool 并行 + 预算硬顶）、`MockKimiRunner`（额度枯竭 fallback，全链标注 mock） |
| `tests/test_kimi_review.py` | pytest 23 例，**全 mock 零真调**：NDJSON 解析/verdict 提取/错误分类（quota·timeout·model_not_found·parse）/多数与降级聚合/预算硬顶/脱敏/CLI 冒烟/线路形态守卫 |

## 快速开始

```bash
# 前置：D:\tools\bin\kimi.exe（0.42.0 已实测）已登录
#（~/.kimi-code/credentials OAuth 自动刷新，与桌面版同一账号）

python -m pytest providers/kimi-review/tests -q     # 23 mock 全绿

# 真跑 3 路审查（3 次真实调用，一次并行）：
python providers/kimi-review/review.py --diff <path/to.diff> \
  --plan-context <path/to/plan.txt> \
  --out <verdict.json>
# stdout 摘要行 + verdict.json 全量（逐路 verdict/findings/session_id/duration + 聚合）

# 额度枯竭时全链 mock（输出带 "mock": true，绝不冒充真审）：
python providers/kimi-review/review.py --diff <d.patch> --mock --out <verdict.json>
```

裁决语义：`approve` / `needs_discussion` / `reject`；无严格多数 → `needs_discussion`
（reason=no-majority）；有路失败 → `degraded: true` 并按存活路多数裁决；全灭 →
`needs_discussion`（reason=no-routes）。findings 按 critical→info 排序并附 route 归属。

## 模型名结论（2026-09-29 实测，~/.kimi-code/config.toml + CLI）

| 别名（`-m` 用） | 后端 model id | display_name | 上下文 | 备注 |
| --- | --- | --- | --- | --- |
| `kimi-code/kimi-for-coding`（默认） | `kimi-for-coding` | **K2.8 Preview** | 1,048,576 | efforts low/high/max，默认 max |
| `kimi-code/kimi-for-coding-highspeed` | 同名 | K2.7 Code Highspeed | 262,144 | 快速档 |
| `kimi-code/k3` / `kimi-code/k3-256k` | `k3` / `k3-256k` | K3 / K3-256k | 1M / 256K | |

- **`kimi-code/k2.8-preview` 等 K2.8 字面变体名不存在**：CLI 本地校验直接报
  `error: Model "kimi-code/k2.8-preview" is not configured in config.toml.`（未触服务端）。
  "K2.8 Preview" 是服务端给 `kimi-for-coding` 挂的显示名——**用默认别名即用 K2.8**。
- 线路（实测存档 `build/_a1_kimi/`）：`kimi -m <alias> -p <prompt> --output-format
  stream-json` → stdout NDJSON，应答行 `{"role":"assistant","content":...}`，
  meta 行含 `session.resume_hint`。**`-p` 不可与 `--yolo`/`--auto` 组合**
  （0.42.0 本地校验 `Cannot combine --prompt with --yolo.`，实测 3 连拒）。
- app.asar/gateway 逆向**未执行**（config 即答案，无需开壳）；如未来 CLI 配置不可读，
  备用路径仍在：`D:/AI软件们/Kimi/resources/{app,gateway}.asar` 只读研究调用协议。

## jiuwenswarm / broker 注册法（B-1 形态）

与 `providers/zcode` 同一立场：**本目录代码不持任何密钥**，被 spawn 的官方 CLI 用用户
自身 OAuth 登录态（与桌面版同账号同配额池），这是配额待遇保留的结构性论证。

- **审查工作器形态（本次交付，已实证）**：Kimi 以"审查工单执行器"身份注册——工单执行方
  按 CLI 契约调用 `review.py --diff <file> --out <verdict.json>`，消费 verdict.json
  （结构见上文；`mock` 字段区分真审/演练，`degraded` 标记降级）。无端口、无长驻进程，
  与 broker 的 spawn 家族（codex-exec / zcode）同族。
- **OpenAI 兼容门面形态（B-1 HTTP 壳，未含于此）**：若 jiuwenswarm 需要把 Kimi 注册成
  chat provider（`client_provider: OpenAI` 指向本地适配层），照抄
  `providers/zcode/adapter_server.py` 的壳、把后端换成本文件的 `run_kimi`
  （messages→单 prompt，`/v1/models` 报 `kimi-code/kimi-for-coding`）。该壳是标注的
  后续工作项，本次未建——审查器本身不依赖它。

## 预算与降级纪律

- 每路 = 1 次真实调用；默认 3 路 = 3 次/审查，硬顶 `MAX_ROUTES_HARD=5`（超出直接
  ValueError，不静默截断）；diff 超 `--max-diff-chars`（默认 60k）截断并标注。
- 单路失败（403 周限 / 401 / 超时 / 输出无合法 JSON）不拖垮整体：聚合按存活路继续，
  `degraded: true` + `failed_routes[]` 留痕；`--mock` 提供零真调的全链演练。
- 所有落盘/返回文本过 `scrub()`（sk- key / JWT / 长 hex 打码）；本目录不读不存凭据。

## ToS 注记

用途为**自有代码仓的审查**（own-use），走用户本人 Kimi 会员的官方 CLI + OAuth 登录态，
单账号、无转售/共享/多账号规避——符合 Moonshot 订阅条款的常规使用范畴（403 周限按官方
口径等窗口滚动或购加油包，勿用多账号绕过）。审查产出为内部决策参考，不对外分发模型输出
产物本身。

## 证据（真实运行，2026-09-29）

- 额度恢复：`kimi -p 只回复一个字：好` → exit 0，答「好」（09-26 的 403 周限已随 7 天
  窗口滚动解除；当日探针存档 `build/_a1_kimi/quota-probe-*.txt`）。
- 真 3 路审查 `build/w01-diff.patch`（W-01 glue fail-closed 修复，9,367 字符）：
  聚合裁决 **needs_discussion（majority 2 of 3，degraded=false，wall 91.7s）**——
  security=approve，correctness/plan=needs_discussion；三路独立抓到同一真缺口
  （工单①的 submit_check 自报路径复核未随 diff 交付、"空集四例"仅交付 2-3 例），
  另有 8 条 low/info 发现（级联遍历无 visited 兜底、非根惰性过期记 REVOKED 审计失真、
  aggregate 对未知 verdict 字符串仍 fail-open 等）。全量：
  `build/_a1_kimi/w01-review-verdict.json`。
- mock 套件：`python -m pytest providers/kimi-review/tests -q` → **23 passed**。
