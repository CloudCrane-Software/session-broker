# -*- coding: utf-8 -*-
"""review.py — Kimi (K2.8 Preview) N-route parallel code reviewer.

Line-A (Kimi 接入) deliverable, 2026-09-29. Spawns the official Kimi Code CLI
(``kimi.exe``, 0.42.0) headless N times in parallel — one review perspective per
route — over the same diff, then aggregates a majority verdict (N=3: 2/3).

Wire shape (evidence: real runs archived at build/_a1_kimi/, 2026-09-29):

    kimi -m kimi-code/kimi-for-coding -p <prompt> --output-format stream-json --yolo
      -> NDJSON lines on stdout:
         {"role":"meta","type":"system.version","version":"0.42.0"}
         {"role":"assistant","content":"..."}            <- response text
         {"role":"meta","type":"session.resume_hint",...}

Model name conclusion (evidence: ~/.kimi-code/config.toml, 2026-09-29):
    alias "kimi-code/kimi-for-coding" -> model id "kimi-for-coding",
    display_name "K2.8 Preview", max_context 1_048_576, efforts low/high/max
    (default max). "kimi-code/k2.8-preview" is NOT a valid alias (CLI rejects
    locally, no server call). Siblings: kimi-for-coding-highspeed (K2.7 Code
    Highspeed, 256K), k3, k3-256k.

Credentials: none here. The spawned CLI uses the user's own OAuth login state
(~/.kimi-code/credentials, auto-refresh) — same structural quota argument as
providers/zcode. This module never reads credential files and scrubs any
token-looking substring out of everything it stores or prints.

Budget discipline (per overnight protocol §5): each route is one real call.
Default N=3 -> 3 real calls per review. Hard cap MAX_ROUTES_HARD=5. Diff is
truncated to max_diff_chars before prompting.

Standalone: Python 3.9+ standard library only (mirrors providers/zcode).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIMI_EXE_DEFAULT = r"D:\tools\bin\kimi.exe"
DEFAULT_MODEL = "kimi-code/kimi-for-coding"  # display_name: K2.8 Preview
DEFAULT_TIMEOUT_S = 300
DEFAULT_MAX_DIFF_CHARS = 60_000
MAX_ROUTES_HARD = 5

VERDICT_APPROVE = "approve"
VERDICT_NEEDS_DISCUSSION = "needs_discussion"
VERDICT_REJECT = "reject"
VALID_VERDICTS = (VERDICT_APPROVE, VERDICT_NEEDS_DISCUSSION, VERDICT_REJECT)
VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")

# Review perspectives: one route each. Same diff, different lens (ask §3).
PERSPECTIVES: Dict[str, str] = {
    "security": (
        "SECURITY review lens: secrets/credentials in code, injection (cmd/SQL/"
        "path), authz/authn gaps, unsafe deserialization, SSRF, resource "
        "exhaustion, dependency risk. Fail-closed mindset: flag anything that "
        "could widen trust boundaries."
    ),
    "correctness": (
        "CORRECTNESS review lens: logic errors, edge cases (empty/None/boundary), "
        "error handling, concurrency/ordering, API misuse, test gaps for the "
        "changed behavior. Trace the changed code paths concretely."
    ),
    "plan": (
        "PLAN-COMPLIANCE review lens: does the diff do what the stated plan/task "
        "says, no more no less? Flag scope creep, missing acceptance criteria, "
        "violated invariants named in the plan, and doc/comment drift."
    ),
}

_VERDICT_RULES = (
    'Write your review as EXACTLY ONE JSON object and nothing else — no markdown '
    'fences, no prose before or after. Schema:\n'
    '{\n'
    '  "verdict": "approve" | "needs_discussion" | "reject",\n'
    '  "findings": [{"title": str, "severity": "info|low|medium|high|critical", '
    '"detail": str}],\n'
    '  "summary": str\n'
    '}\n'
    '"approve" = safe to merge as-is; "needs_discussion" = mergeable after '
    'addressing findings; "reject" = must not merge.'
)

# Token-scrubbing (conservative, same policy as the verify-stage kimi_provider).
_TOKENISH = re.compile(
    r"(sk-[A-Za-z0-9]{8})[A-Za-z0-9\-_]+"             # sk- prefixed keys
    r"|(eyJ[A-Za-z0-9\-_]{10})[A-Za-z0-9\-_.]*"       # JWT-ish (incl. dotted tail)
    r"|([A-Fa-f0-9]{32,})"                             # long hex blobs
)


def scrub(text: str) -> str:
    """Mask credential-looking substrings; everything passes through here."""
    return _TOKENISH.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "***MASKED***", text or "")


# ---------------------------------------------------------------------------
# Wire layer: spawn kimi.exe, parse stream-json
# ---------------------------------------------------------------------------

@dataclass
class KimiRunResult:
    ok: bool
    text: str = ""
    exit_code: Optional[int] = None
    error_class: str = ""  # "" | quota | auth | model_not_found | timeout | crash | empty
    error_raw: str = ""
    session_id: str = ""
    duration_ms: int = 0


Runner = Callable[..., Any]  # subprocess.run-alike; tests inject fakes


def parse_stream_json(stdout: str) -> Tuple[str, str]:
    """Return (assistant_text, session_id) from kimi stream-json NDJSON.

    Takes the LAST assistant line (final turn wins); tolerates garbage lines
    and the resume_hint meta event.
    """
    text, session_id = "", ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(node, dict):
            continue
        if node.get("role") == "assistant" and isinstance(node.get("content"), str):
            text = node["content"]
        elif node.get("type") == "session.resume_hint":
            session_id = str(node.get("session_id", "")) or session_id
    return text, session_id


def classify_run_error(stderr: str, exit_code: Optional[int], timed_out: bool) -> Tuple[str, str]:
    """Map CLI failure modes to coarse classes (evidence: verify runbook 09-26/29)."""
    if timed_out:
        return "timeout", "process killed by timeout"
    blob = stderr or ""
    if "is not configured in config.toml" in blob:
        return "model_not_found", blob.strip().splitlines()[-1] if blob.strip() else blob
    if "403" in blob and ("limit" in blob or "quota" in blob.lower()):
        return "quota", blob.strip()
    if "auth_error" in blob or "401" in blob:
        return "auth", blob.strip()
    if exit_code not in (0, None):
        tail = " | ".join(blob.strip().splitlines()[-3:])
        return "crash", tail
    return "", ""


def run_kimi(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    exe: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: Optional[Runner] = None,
) -> KimiRunResult:
    """One headless kimi call; stream-json parsed to (text, session_id).

    NOTE (evidence: real run 2026-09-29, 3x local rejects in 1.8s): kimi 0.42.0
    forbids combining ``-p`` with ``--yolo``/``--auto`` — those are interactive-
    session mode flags. ``-p`` alone is already one-shot headless. Do not add
    --yolo back (providers/zcode uses it because zcode's flag grammar differs).
    """
    binary = exe or os.environ.get("KIMI_BIN") or (shutil.which("kimi") or KIMI_EXE_DEFAULT)
    argv = [binary, "-m", model, "-p", prompt, "--output-format", "stream-json"]
    started = time.monotonic()
    res = KimiRunResult(ok=False)
    run = runner or subprocess.run
    try:
        proc = run(argv, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        res.duration_ms = int((time.monotonic() - started) * 1000)
        res.error_class, res.error_raw = "timeout", "timeout after {}s".format(timeout_s)
        return res
    except FileNotFoundError:
        res.error_class, res.error_raw = "crash", "exe not found: {}".format(binary)
        return res
    res.duration_ms = int((time.monotonic() - started) * 1000)
    res.exit_code = getattr(proc, "returncode", None)
    text, res.session_id = parse_stream_json(getattr(proc, "stdout", "") or "")
    res.text = scrub(text)
    err_cls, err_msg = classify_run_error(getattr(proc, "stderr", "") or "", res.exit_code, False)
    res.error_class, res.error_raw = err_cls, scrub(err_msg)[:2000]
    if not err_cls:
        res.ok = bool(res.text.strip())
        if not res.ok:
            res.error_class, res.error_raw = "empty", "no assistant content in stream"
    return res


# ---------------------------------------------------------------------------
# Verdict layer: strict-JSON extraction + validation
# ---------------------------------------------------------------------------

def _balanced_json_objects(text: str) -> List[dict]:
    """Yield parsed dicts for every balanced top-level {...} blob in text.

    Brace scanning respects string literals and escapes so a '}' inside a
    finding title never truncates the object.
    """
    out: List[dict] = []
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        node = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        node = None
                    if isinstance(node, dict):
                        out.append(node)
                    start = -1
    return out


def extract_verdict(text: str) -> Optional[dict]:
    """Extract and validate the review JSON from (possibly chatty) model text.

    Returns the coerced verdict dict, or None when no valid object exists.
    Coercion: unknown verdict values -> None (invalid); unknown severities ->
    "info"; non-list findings dropped.
    """
    for node in _balanced_json_objects(text or ""):
        verdict = node.get("verdict")
        if verdict not in VALID_VERDICTS:
            continue
        findings = []
        raw_findings = node.get("findings")
        if isinstance(raw_findings, list):
            for f in raw_findings:
                if not isinstance(f, dict):
                    continue
                sev = f.get("severity") if f.get("severity") in VALID_SEVERITIES else "info"
                findings.append({
                    "title": str(f.get("title", ""))[:300],
                    "severity": sev,
                    "detail": str(f.get("detail", ""))[:2000],
                })
        return {
            "verdict": verdict,
            "findings": findings,
            "summary": str(node.get("summary", ""))[:2000],
        }
    return None


# ---------------------------------------------------------------------------
# Route + aggregation
# ---------------------------------------------------------------------------

@dataclass
class RouteResult:
    perspective: str
    ok: bool
    verdict: Optional[dict] = None
    raw_text: str = ""
    error_class: str = ""
    error_raw: str = ""
    session_id: str = ""
    duration_ms: int = 0

    def to_json(self) -> dict:
        return {
            "perspective": self.perspective,
            "ok": self.ok,
            "verdict": self.verdict,
            "summary": (self.verdict or {}).get("summary", ""),
            "findings": (self.verdict or {}).get("findings", []),
            "raw_text": self.raw_text[:2000],
            "error_class": self.error_class,
            "error_raw": self.error_raw,
            "session_id": self.session_id,
            "duration_ms": self.duration_ms,
        }


def build_review_prompt(perspective: str, diff_text: str,
                        plan_context: str = "",
                        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Same diff, perspective-specific lens; strict-JSON contract appended."""
    lens = PERSPECTIVES.get(perspective)
    if lens is None:
        raise ValueError("unknown perspective: {} (known: {})".format(perspective, sorted(PERSPECTIVES)))
    diff = diff_text or ""
    trunc_note = ""
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars]
        trunc_note = "\n[diff truncated at {} chars]".format(max_diff_chars)
    parts = [
        "You are a staff-level code reviewer. Review the unified diff below.",
        "Review lens for THIS route: " + lens,
    ]
    if plan_context.strip():
        parts.append("Plan / task context the diff claims to implement:\n<<<PLAN\n{}\nPLAN>>>".format(plan_context.strip()))
    parts.append("Unified diff to review:\n<<<DIFF\n{}\nDIFF>>>{}".format(diff, trunc_note))
    parts.append(_VERDICT_RULES)
    return "\n\n".join(parts)


def run_route(
    perspective: str,
    diff_text: str,
    *,
    plan_context: str = "",
    model: str = DEFAULT_MODEL,
    exe: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    runner: Optional[Runner] = None,
) -> RouteResult:
    """One review route = one real kimi call, parsed to a verdict."""
    prompt = build_review_prompt(perspective, diff_text, plan_context, max_diff_chars)
    r = run_kimi(prompt, model=model, exe=exe, timeout_s=timeout_s, runner=runner)
    verdict = extract_verdict(r.text) if r.ok else None
    if r.ok and verdict is None:
        return RouteResult(perspective, False, raw_text=r.text, error_class="parse",
                           error_raw="no valid verdict JSON in response",
                           session_id=r.session_id, duration_ms=r.duration_ms)
    return RouteResult(perspective, r.ok, verdict=verdict, raw_text=r.text,
                       error_class=r.error_class, error_raw=r.error_raw,
                       session_id=r.session_id, duration_ms=r.duration_ms)


def aggregate(routes: List[RouteResult], n_expected: Optional[int] = None) -> dict:
    """Majority verdict over parsed routes, with degraded-mode semantics.

    - majority = strict >half of *parsed* routes agreeing on the same verdict;
    - no strict majority (split) -> needs_discussion, reason "no-majority";
    - any route failed/missing (vs n_expected) -> degraded=True;
    - 0 parsed routes -> verdict needs_discussion, reason "no-routes".
    """
    parsed = [r.verdict["verdict"] for r in routes if r.ok and r.verdict]
    expected = n_expected if n_expected is not None else len(routes)
    failed = [r for r in routes if not (r.ok and r.verdict)]
    counts = Counter(parsed)
    verdict, reason = VERDICT_NEEDS_DISCUSSION, ""
    if not parsed:
        reason = "no-routes"
    else:
        top, top_n = counts.most_common(1)[0]
        if top_n * 2 > len(parsed):
            verdict, reason = top, "majority {} of {}".format(top_n, len(parsed))
        else:
            reason = "no-majority: " + ", ".join("{}x{}".format(v, n) for v, n in counts.most_common())
    findings = []
    for r in routes:
        if r.ok and r.verdict:
            for f in r.verdict.get("findings", []):
                findings.append(dict(f, route=r.perspective))
    sev_rank = {s: i for i, s in enumerate(VALID_SEVERITIES)}
    findings.sort(key=lambda f: -sev_rank.get(f.get("severity", "info"), 0))  # critical first
    return {
        "verdict": verdict,
        "reason": reason,
        "degraded": bool(failed) or len(parsed) != expected,
        "tally": dict(counts),
        "n_expected": expected,
        "n_parsed": len(parsed),
        "n_failed": len(failed),
        "failed_routes": [
            {"perspective": r.perspective, "error_class": r.error_class} for r in failed
        ],
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Orchestrator: N routes in parallel, budget-capped
# ---------------------------------------------------------------------------

def run_review(
    diff_text: str,
    *,
    perspectives: Tuple[str, ...] = ("security", "correctness", "plan"),
    plan_context: str = "",
    model: str = DEFAULT_MODEL,
    exe: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    runner: Optional[Runner] = None,
    max_workers: int = 3,
) -> dict:
    """Full N-route review. Real-call cost = len(perspectives) (hard-capped)."""
    if len(perspectives) > MAX_ROUTES_HARD:
        raise ValueError("perspectives > hard cap {} (budget control)".format(MAX_ROUTES_HARD))
    for p in perspectives:
        if p not in PERSPECTIVES:
            raise ValueError("unknown perspective: {} (known: {})".format(p, sorted(PERSPECTIVES)))
    started = time.monotonic()

    def _one(p: str) -> RouteResult:
        return run_route(p, diff_text, plan_context=plan_context, model=model,
                         exe=exe, timeout_s=timeout_s, max_diff_chars=max_diff_chars,
                         runner=runner)

    if len(perspectives) == 1:
        routes = [_one(perspectives[0])]
    else:
        with ThreadPoolExecutor(max_workers=max(max_workers, 1)) as pool:
            routes = list(pool.map(_one, perspectives))

    agg = aggregate(routes, n_expected=len(perspectives))
    result = {
        "provider": "kimi",
        "model": model,
        "reviewer": "kimi-review/1.0",
        "mock": runner is not None and getattr(runner, "is_mock", False),
        "verdict": agg["verdict"],
        "reason": agg["reason"],
        "degraded": agg["degraded"],
        "tally": agg["tally"],
        "routes": [r.to_json() for r in routes],
        "findings": agg["findings"],
        "n_failed": agg["n_failed"],
        "failed_routes": agg["failed_routes"],
        "meta": {
            "calls": len(routes),
            "n_perspectives": len(perspectives),
            "perspectives": list(perspectives),
            "diff_chars": len(diff_text or ""),
            "wall_ms": int((time.monotonic() - started) * 1000),
        },
    }
    return result


# ---------------------------------------------------------------------------
# Mock runner (quota-exhausted fallback + tests). Clearly labelled everywhere.
# ---------------------------------------------------------------------------

class MockKimiRunner:
    """subprocess.run-alike that returns canned per-perspective verdict JSON.

    Every result produced through it is labelled mock=True end to end (ask §4:
    '403 则 mock 全链+标注 [待额度]'). Assign .is_mock=True on instances.
    """

    is_mock = True

    def __init__(self, verdicts: Optional[Dict[str, dict]] = None,
                 fail_perspectives: Tuple[str, ...] = (),
                 stderr: str = "", returncode: int = 0, sleep_s: float = 0.0):
        self.verdicts = verdicts or {
            p: {"verdict": VERDICT_APPROVE, "findings": [], "summary": "mock {} ok".format(p)}
            for p in ("security", "correctness", "plan")
        }
        self.fail_perspectives = tuple(fail_perspectives)
        self.stderr = stderr
        self.returncode = returncode
        self.sleep_s = sleep_s
        self.calls: List[list] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        # perspective is recoverable from the prompt: the lens line names it
        prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
        persp = next((p for p in PERSPECTIVES if ("lens for THIS route" in prompt and p.capitalize()[0] in prompt[:200])), "")
        for p in PERSPECTIVES:
            if PERSPECTIVES[p] in prompt:
                persp = p
                break
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if persp in self.fail_perspectives or self.returncode != 0:
            class _P:
                returncode = self.returncode or 1
                stdout = ""
                stderr = self.stderr or "error: failed to run prompt: provider.auth_error: 403 weekly limit"
            return _P()
        payload = self.verdicts.get(persp, self.verdicts.get("security"))
        class _Q:
            returncode = 0
            stdout = json.dumps({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})
            stderr = ""
        return _Q()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Kimi N-route parallel code review (K2.8 Preview)")
    ap.add_argument("--diff", help="path to unified diff (or '-' for stdin)")
    ap.add_argument("--plan-context", default="", help="path to plan/task text the diff claims to implement")
    ap.add_argument("--perspectives", default="security,correctness,plan",
                    help="comma list from: " + ",".join(sorted(PERSPECTIVES)))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="per-route seconds")
    ap.add_argument("--max-diff-chars", type=int, default=DEFAULT_MAX_DIFF_CHARS)
    ap.add_argument("--out", default="", help="write verdict JSON here")
    ap.add_argument("--mock", action="store_true", help="no real calls; canned verdicts, labelled mock")
    args = ap.parse_args(argv)

    if args.diff == "-":
        diff_text = sys.stdin.read()
    elif args.diff:
        with open(args.diff, "r", encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    else:
        ap.error("--diff is required")
    plan_context = ""
    if args.plan_context:
        with open(args.plan_context, "r", encoding="utf-8", errors="replace") as fh:
            plan_context = fh.read()

    runner = MockKimiRunner() if args.mock else None
    result = run_review(
        diff_text,
        perspectives=tuple(s.strip() for s in args.perspectives.split(",") if s.strip()),
        plan_context=plan_context,
        model=args.model,
        timeout_s=args.timeout,
        max_diff_chars=args.max_diff_chars,
        runner=runner,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    print(json.dumps({k: result[k] for k in ("provider", "model", "verdict", "reason",
                                             "degraded", "mock", "n_failed")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
