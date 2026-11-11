"""把 `analysis/*.py` 的断言行按「能不能机械锚定」分档，并生成影子登记表。

**它不写 `docs/number-provenance.json`，不改 `SCOPE_GLOBS`，不进测试。**
产出只落在 `data/.scratch/`，用途是让「该不该把 analysis 纳入射程」这个决定
从判断题变成读数题。

分档判据（一个 token 一档，不用猜）：

- `unique`  同名产物里**恰好一个**数值叶子落在显示精度容差内 ⇒ 可锚定；
- `multi`   多个叶子同时命中 ⇒ 路径不可辨（同一数出现在产物多处）；
- `prose`   只在字符串值里出现 ⇒ `resolve()` 结构性读不到（叶子不是数）；
- `absent`  产物里根本没有。

容差**只由显示精度反推**：`75.7` 写了一位小数 ⇒ 0.05（半个末位）。
绝不拿残差反推（把 0.03 当容差）——那是用被测对象校准被测器。

本文件的 docstring 刻意不含那四个触发词：工具脚本一旦进射程，它的**规则说明**
会被当成读数，于是建守卫的人先给自己造了一条债（本轮 +2 行）。

跨产物引用（本脚本产物里没有、兄弟产物里有）单独记一档 `sibling`：它是
传递依赖，能锚但锚点不在同名文件上，需要人指定 `@artifact:path`。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_number_provenance as C  # noqa: E402

GLOBS = ("analysis/*.py",)
ART_DIR = "analysis"
OUT = ROOT / "data" / ".scratch" / "provenance_shadow_analysis.json"


def leaves(node, prefix="", out=None):
    """(路径, 数值) —— 只收 JSON 数值叶子，路径用 `.` 拼，列表用 `[i]`。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            leaves(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            leaves(v, f"{prefix}[{i}]", out)
    else:
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            return out
        out.append((prefix, float(node)))
    return out


def strings(node, out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        for v in node.values():
            strings(v, out)
    elif isinstance(node, list):
        for v in node:
            strings(v, out)
    elif isinstance(node, str):
        out.append(node)
    return out


def tol_of(tok: str) -> float:
    """显示精度容差 = 半个末位。`75.7`→0.05，`0.0695`→0.00005。"""
    m = re.match(r"(\d+)\.(\d+)", tok)
    if m:
        return 0.5 * 10 ** (-len(m.group(2)))
    return 0.5          # 整数读数（`19bp`/`2 倍`）没有末位语义，按整数半格


_SIGNED = re.compile(r"(?<![\d.])([+\-\u2212]?)(\d+(?:\.\d+)?)")


def signed_readings(text: str) -> list[tuple[float, str, float]]:
    """(带符号数值, 显示串, 容差)。符号规则与 `digits()` 一致：紧邻、前一位非数字。"""
    body = C.identity_text(text)
    want = []
    for tok in C.readings(text):
        want.append(re.match(r"\d+(?:\.\d+)?", tok).group(0))  # type: ignore[union-attr]
    out: list[tuple[float, str, float]] = []
    for m in _SIGNED.finditer(body):
        disp = m.group(2)
        if disp not in want:
            continue
        sign = -1.0 if m.group(1) in ("-", "\u2212") else 1.0
        out.append((sign * float(disp), m.group(1) + disp, tol_of(disp)))
    return out


def classify(value: float, tol: float, nums, strs):
    """一个 token 在产物里的可锚定状态。"""
    hit = [(p, v) for p, v in nums if abs(v - value) <= tol + 1e-12]
    if len(hit) == 1:
        return "unique", hit
    if len(hit) > 1:
        return "multi", hit
    shown = f"{abs(value):g}"
    pat = re.compile(r"(?<![\d.])" + re.escape(shown) + r"(?![\d])")
    if any(pat.search(s) for s in strs):
        return "prose", []
    return "absent", []


def _set(node, path: str, value: float) -> bool:
    """按 `a.b[0].c` 改写叶子。用于自检：把产物挪一格，看登记表会不会红。"""
    parts = re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)
    cur = node
    for i, (name, idx) in enumerate(parts):
        last = i == len(parts) - 1
        key = int(idx) if idx else name
        if last:
            cur[key] = value
            return True
        cur = cur[key]
    return False


def selftest(entries: list[dict]) -> int:
    """**绿得可证伪**：每条 entry 都要能被两件事弄红。

    1. 产物改一格（`+2·tol`）⇒ `VALUE_MISMATCH`；
    2. 这条改不了——说明锚点路径是死的，登记表在盖空章。
    """
    import copy
    bad = 0
    for e in entries:
        src = json.loads((ROOT / e["artifact"]).read_text(encoding="utf-8"))
        for c in e["checks"]:
            one = dict(e, checks=[c])      # 一次只喂一项，报错才不会张冠李戴
            base = copy.deepcopy(src)
            rep = C.Report(0, 0, 0)
            clean = C._check_entry(ROOT, e["file"], e["text"], one, rep)
            if clean:
                print(f"  ✗ 未扰动就报错：{e['file']} {c['path']} {clean[0]['detail']}")
                bad += 1
                continue
            got = C.reduce_leaves(C.resolve(src, c["path"]), c.get("agg"))
            drifted = copy.deepcopy(src)
            if not _set(drifted, c["path"], got + 2 * c["tol"] + 1e-6):
                print(f"  ✗ 路径写不回去：{e['file']} {c['path']}")
                bad += 1
                continue
            tmp = ROOT / "data" / ".scratch" / "_selftest"
            ap = tmp / e["artifact"]
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(json.dumps(drifted, ensure_ascii=False), encoding="utf-8")
            rep2 = C.Report(0, 0, 0)
            hits = C._check_entry(tmp, e["file"], e["text"], one, rep2)
            if not any(h["kind"] == "VALUE_MISMATCH" for h in hits):
                print(f"  ✗ 挪了 {2 * c['tol'] + 1e-6:g} 也不红：{e['file']} {c['path']}")
                bad += 1
            base = None
    n = sum(len(e["checks"]) for e in entries)
    print(f"自检：{n} 项，能双向可失败 {n - bad}，不能 {bad}")
    return bad


def main(argv: list[str] | None = None) -> int:
    cfg = C.Config(globs=GLOBS)
    lines = C.scan(ROOT, cfg)
    numbered = C.numbered_lines(ROOT, cfg)
    claim_all = list(C._claim_lines(ROOT, cfg))

    # 每个分析脚本的产物内容，只加载一次
    cache: dict[str, tuple] = {}
    def art_of(stem: str):
        if stem not in cache:
            p = ROOT / ART_DIR / f"{stem}_result.json"
            if not p.exists():
                cache[stem] = None
            else:
                d = json.loads(p.read_text(encoding="utf-8"))
                cache[stem] = (leaves(d), strings(d))
        return cache[stem]

    sib = {}
    for p in sorted((ROOT / ART_DIR).glob("*_result.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        sib[p.stem] = (leaves(d), strings(d))

    buckets = Counter()
    per_line: list[dict] = []
    entries: list[dict] = []
    for ln in lines:
        stem = Path(ln.file).stem
        art = art_of(stem)
        rec = {"file": ln.file, "where": f"{ln.where}:{ln.lineno}",
               "text": ln.text, "tokens": []}
        if art is None:
            rec["status"] = "no_artifact"
            per_line.append(rec)
            buckets["行·无同名产物"] += 1
            continue
        nums, strs = art
        checks = []
        states = []
        for value, disp, tol in signed_readings(ln.text):
            state, hit = classify(value, tol, nums, strs)
            if state == "absent":
                for other, (onum, ostr) in sib.items():
                    if other == stem:
                        continue
                    st2, _h2 = classify(value, tol, onum, ostr)
                    if st2 in ("unique", "multi"):
                        state = "sibling"
                        hit = [(other, p, v) for p, v in _h2]
                        break
            states.append(state)
            buckets[f"token·{state}"] += 1
            rec["tokens"].append({"disp": disp, "state": state,
                                  "paths": [f"{a}:{b}" if isinstance(a, str) and a in sib
                                            else b for a, b in
                                            ([(x[0], x[1]) if len(x) == 3 else (None, x[0])
                                              for x in hit])][:6]})
            if state == "unique":
                checks.append({"path": hit[0][0], "claimed": value, "tol": tol})
            elif state == "multi":
                rec.setdefault("why", []).append(f"{disp} 命中 {len(hit)} 个叶子")
            elif state == "sibling":
                rec.setdefault("why", []).append(f"{disp} 只在兄弟产物里")
            elif state == "prose":
                rec.setdefault("why", []).append(f"{disp} 只在散文里")
            else:
                rec.setdefault("why", []).append(f"{disp} 产物里没有")

        if checks and len(checks) == len(states):
            rec["status"] = "mechanical"
            buckets["行·可机械锚定"] += 1
            entries.append({"file": ln.file, "text": ln.text, "artifact":
                            f"{ART_DIR}/{stem}_result.json", "checks": checks})
        elif checks:
            rec["status"] = "partial"
            buckets["行·部分可锚定"] += 1
        else:
            rec["status"] = "unanchored"
            buckets["行·不可锚定"] += 1
        per_line.append(rec)

    payload = {
        "_doc": "影子基线：不做判定，只记「哪些行能机械锚定」。由 scripts/"
                "_gen_provenance_entries.py 生成，不进版本控制。",
        "scope": {"globs": list(GLOBS), "claim_lines": len(claim_all),
                  "numbered": len(numbered), "claims": len(lines)},
        "buckets": dict(buckets),
        "entries": entries,
        "lines": per_line,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"射程 analysis/*.py：claim 段落非空行 {len(claim_all)}｜带身份 {len(numbered)}"
          f"｜读数形态 {len(lines)}")
    print("行分档：" + "，".join(f"{k[2:]} {v}" for k, v in sorted(buckets.items())
                                 if k.startswith("行·")))
    print("token 分档：" + "，".join(f"{k[6:]} {v}" for k, v in sorted(buckets.items())
                                    if k.startswith("token·")))
    print(f"可机械锚定的 entry：{len(entries)} 条，共 "
          f"{sum(len(e['checks']) for e in entries)} 项核对")
    print(f"影子基线写入 {OUT.relative_to(ROOT).as_posix()}")
    if "--selftest" in (argv or sys.argv[1:]):
        return 1 if selftest(entries) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
