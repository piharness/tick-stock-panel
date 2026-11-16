"""首屏加载耗时诊断：把「页面慢」拆成可复现的数字。

为什么需要它
------------
「优化加载速度」最贵的错是改完凭感觉宣布变快。本脚本对首屏实际发出的
接口做多轮计时，输出 min/p50/max 与字节数，改动前后用**同一个命令**对照。

只测已经注册的路径。404 会显式标出来 —— 路径写错时 curl 也返回得飞快，
不标出来就会把「路径不存在」读成「优化成功」。

用法
----
    python analysis/_diag_page_load_latency.py                    # 默认 127.0.0.1:3018
    python analysis/_diag_page_load_latency.py --base http://host:3018 --rounds 5
    python analysis/_diag_page_load_latency.py --json out.json    # 额外落一份 JSON

判据
----
不看单个数字，看 p50：首屏接口 p50 应 < 300ms；`/api/data/status` 任何一轮
超过 2s 都算红 —— 它在 Dashboard 上是整屏 spinner 的直接来源。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 首屏链路：Layout + Dashboard 挂载时实际发出的请求。
# 顺序 = 用户等待的顺序，不是字母序。
FIRST_PAINT = [
    "/",
    "/api/settings",
    "/api/overview/market",
    "/api/data/status",
    "/api/data/integrity",
    "/api/capabilities",
]

# 只做提示，不参与判定：这些路径名随路由演进会变，404 不代表页面慢。
OPTIONAL = [
    "/api/preferences",
    "/api/capability-matrix",
]

# 阈值（秒）。data/status 单轮超 2s 即判红。
LIMITS = {
    "/api/data/status": 2.0,
    "/api/overview/market": 1.0,
}
DEFAULT_LIMIT = 1.0


def _probe(base: str, path: str) -> tuple[int, int, float]:
    """返回 (http_code, bytes, seconds)。不抛异常：失败也是一种结果。"""
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip, br"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, len(body), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, 0, time.perf_counter() - started
    except Exception:  # noqa: BLE001 — 连接失败也要能继续测下一项
        return 0, 0, time.perf_counter() - started


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3018")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    report: dict[str, dict] = {}
    print(f"base={args.base} rounds={args.rounds}")
    print(f"{'path':28s} {'code':>4s} {'bytes':>9s} {'min':>8s} {'p50':>8s} {'max':>8s}  verdict")
    print("-" * 82)

    worst = 0.0
    for path in FIRST_PAINT + OPTIONAL:
        samples: list[float] = []
        code = 0
        size = 0
        for _ in range(args.rounds):
            code, size, dt = _probe(args.base, path)
            samples.append(dt)
        p50 = statistics.median(samples)
        entry = {
            "code": code,
            "bytes": size,
            "min": min(samples),
            "p50": p50,
            "max": max(samples),
            "samples": samples,
        }
        report[path] = entry

        if path in OPTIONAL:
            verdict = "skip(404)" if code == 404 else "ok"
        elif code != 200:
            verdict = f"RED http={code}"
        else:
            limit = LIMITS.get(path, DEFAULT_LIMIT)
            if p50 > limit:
                verdict = f"RED > {limit:.1f}s"
                worst = max(worst, p50)
            else:
                verdict = "ok"
        print(
            f"{path:28s} {code:4d} {size:9d} {min(samples):7.3f}s {p50:7.3f}s "
            f"{max(samples):7.3f}s  {verdict}"
        )

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "base": args.base,
                    "rounds": args.rounds,
                    "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "paths": report,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\njson → {out}")

    return 1 if worst > 0 else 0  # 供 shell 判红，不吞掉失败


if __name__ == "__main__":
    sys.exit(main())
