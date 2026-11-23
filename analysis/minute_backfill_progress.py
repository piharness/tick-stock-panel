"""5min 回补进度看板：把「还要多久」变成可判定的读数。

为什么单独一个脚本
------------------
`backfill_bs_minute.py --status` 是**自证读数**（给运维看对不对），它不区分 cohort，
也不回答「现在能不能开始做研究」。而这个任务的核心约束是
**无生存偏差**——退市股那一侧是否封顶，和「总数涨了多少」是两件事，
混在一个百分比里就会得出「才 45%，还不能用」的错误判断（实际已经能用了）。

三条进度线分开报
----------------
1. 总体（按行数，不是按只数）：缺口股平均只 820 个交易日、存续股 1,134 天，
   按只数外推会低估剩余工作量。
2. **无生存偏差线**：退市 + 其他缺口，这一侧封顶就是「研究可以开始」。
3. 存续股：只影响覆盖面，不影响偏差。

用法
----
    python analysis/minute_backfill_progress.py            # 打一次
    python analysis/minute_backfill_progress.py --watch    # 每 15 秒刷新（Ctrl-C 退出）
    python analysis/minute_backfill_progress.py --html     # 生成自包含 HTML 看板
    python analysis/minute_backfill_progress.py --ascii    # 终端不支持方块时
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:/DSH/tick-stock-panel")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

import polars as pl  # noqa: E402

import backfill_bs_minute as bsm  # noqa: E402

DATA = bsm.DATA
OUT = bsm.OUT
LANDING = bsm.LANDING
BS_START = bsm.BS_START

#: 今日预算：**从驱动日志里读当前那一轮**，不要硬编码。
#: 硬编码会让「今日 96M / 110M 快满了」成为假信号——循环下一轮预算是 150M，
#: 数字没变但含义变了。读不到就退回默认值。
TODAY_BUDGET_DEFAULT = 110_000_000
LOOP_LOG = OUT / "_run_1120loop.log"


def auto_budget() -> int:
    try:
        txt = LOOP_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return TODAY_BUDGET_DEFAULT
    import re
    hits = re.findall(r"round budget=(\d+)", txt)
    return int(hits[-1]) if hits else TODAY_BUDGET_DEFAULT


# ---------------------------------------------------------------- 读数采集

def _cohort_map() -> dict[str, str]:
    """symbol → cohort（退市 / 其他缺口 / 存续）。优先用审计落地的明细。"""
    m: dict[str, str] = {}
    p = OUT / "_coverage.parquet"
    if p.exists():
        for r in pl.read_parquet(p, columns=["symbol", "cohort"]).iter_rows(named=True):
            m[r["symbol"]] = r["cohort"]
    return m


def _coverage_rate() -> dict[str, float]:
    """各 cohort 的 只-日 覆盖率（分母 = 两套独立日K，见 _audit_minute_coverage）。"""
    p = OUT / "_coverage.parquet"
    if not p.exists():
        return {}
    d = pl.read_parquet(p, columns=["cohort", "exp", "miss"])
    g = d.group_by("cohort").agg(pl.col("exp").sum(), pl.col("miss").sum())
    return {r["cohort"]: (1 - r["miss"] / r["exp"]) if r["exp"] else 0.0
            for r in g.iter_rows(named=True)}


def _rate_per_sec(st: dict[str, dict], window_min: int = 10) -> float:
    """近 window_min 分钟的真实吞吐（只算 ok 且落在窗口内的记录）。"""
    t1 = time.time()
    t0 = t1 - window_min * 60
    rows = 0
    for r in st.values():
        if r.get("status") == "ok" and t0 <= float(r.get("ts", 0) or 0) <= t1:
            rows += int(r.get("rows", 0) or 0)
    return rows / (window_min * 60) if rows else 0.0


def _scan_landing() -> tuple[set[str], float, int, int]:
    """一次 scandir 拿全 (文件名集合, 最新 mtime, 总字节, 个数)。

    为什么不用 `Path.glob` + 逐个 `stat()`：`./data` 是 9p 挂载，元数据按文件数
    线性收费（实测 glob 372 文件 = 1340ms，`DirEntry` 比 `Path.stat()` 快约 100×）。
    2,400+ 个文件时差别是「秒级 vs 上百毫秒」，直接决定 --watch 能不能 15 秒刷一次。
    """
    names: set[str] = set()
    last = 0.0
    size = 0
    n = 0
    with os.scandir(LANDING) as it:
        for e in it:
            if not e.name.endswith(".parquet"):
                continue
            st = e.stat()
            names.add(e.name[:-8])
            n += 1
            size += st.st_size
            if st.st_mtime > last:
                last = st.st_mtime
    return names, last, size, n


def collect(today_budget: int = TODAY_BUDGET_DEFAULT, with_tasks: bool = True,
            task_cache: list | None = None) -> dict:
    st = bsm.load_state()
    gaps = set(bsm.gap_symbols())
    files, last_mtime, total_size, n_files = _scan_landing()

    if with_tasks or task_cache is None:
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tl = bsm.tasks()          # 只读两个小 parquet，很快
    else:
        tl = task_cache

    ok = {s: r for s, r in st.items() if r.get("status") == "ok" and s in files}
    done_syms = set(ok)
    pend = [t["symbol"] for t in tl if t["symbol"] not in done_syms]

    # 行数口径：已完成取实测，待跑用每票日线交易日 × 48（与 --status 同一算法）
    done_rows = sum(int(r.get("rows", 0) or 0) for r in ok.values())
    ip_path = OUT / "_inplay.parquet"
    n_days = {}
    if ip_path.exists():
        n_days = {r["symbol"]: r["n_days"]
                  for r in pl.read_parquet(ip_path).iter_rows(named=True)}
    pend_rows = sum(n_days.get(s, 1134) * 48 for s in pend)
    total_rows = done_rows + pend_rows

    coh = _cohort_map()
    def bucket(s: str) -> str:
        if s in gaps:
            return coh.get(s, "gap_other")
        return "survivor"

    # cohort 的「分母只数」= 任务表里该 cohort 的全部（含已完成）
    cnt_total: dict[str, int] = {}
    cnt_done: dict[str, int] = {}
    for t in tl:
        b = bucket(t["symbol"])
        cnt_total[b] = cnt_total.get(b, 0) + 1
        if t["symbol"] in done_syms:
            cnt_done[b] = cnt_done.get(b, 0) + 1

    # 今日：跨日归零，与 --max-rows 的计数器同源（state.ts）
    t_mid = datetime.combine(datetime.today(), datetime.min.time()).timestamp()
    today = [r for r in st.values()
             if float(r.get("ts", 0) or 0) >= t_mid and r.get("status") == "ok"]
    today_rows = sum(int(r.get("rows", 0) or 0) for r in today)

    # 健康：f5 最后写入时刻 + 最近一条 state 记录
    last_ts = max((float(r.get("ts", 0) or 0) for r in st.values()), default=0)
    blacklist_recent = any(
        bsm._is_blacklist(str(r.get("note", ""))) and float(r.get("ts", 0) or 0) > time.time() - 900
        for r in st.values())

    rate = _rate_per_sec(st)
    eta_h = (pend_rows / rate / 3600) if rate > 0 else None

    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "t0": time.time(),
        "total_syms": len(tl),
        "done_syms": len(done_syms),
        "pend_syms": len(pend),
        "done_rows": done_rows,
        "pend_rows": pend_rows,
        "total_rows": total_rows,
        "cohort": {k: {"done": cnt_done.get(k, 0), "total": cnt_total.get(k, 0)}
                   for k in ("delisted", "gap_other", "survivor")},
        "coverage": _coverage_rate(),
        "today_rows": today_rows,
        "today_budget": today_budget,
        "rate": rate,
        "eta_h": eta_h,
        "idle_sec": (time.time() - last_mtime) if last_mtime else None,
        "last_state_age": (time.time() - last_ts) if last_ts else None,
        "blacklist_recent": blacklist_recent,
        "files": n_files,
        "size_gib": total_size / 2 ** 30,
    }


# ---------------------------------------------------------------- 终端渲染

BAR_W = 30
FULL, EMPTY = "█", "░"


def bar(frac: float, width: int = BAR_W, ascii_mode: bool = False) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    if ascii_mode:
        return "#" * n + "-" * (width - n)
    return FULL * n + EMPTY * (width - n)


def render(d: dict, ascii_mode: bool = False) -> str:
    L = []
    pct = d["done_rows"] / d["total_rows"] if d["total_rows"] else 0
    L.append(f"5min 回补 · {d['now']}")
    L.append("─" * 64)
    L.append(f"总体   {bar(pct, ascii_mode=ascii_mode)} {pct:6.1%}  "
             f"{d['done_rows']/1e6:6.1f}M / {d['total_rows']/1e6:6.1f}M 行")
    ps = d["done_syms"] / d["total_syms"] if d["total_syms"] else 0
    L.append(f"只数   {bar(ps, ascii_mode=ascii_mode)} {ps:6.1%}  "
             f"{d['done_syms']:5,} / {d['total_syms']:,} 只")
    L.append("")

    # —— 核心：无生存偏差线单独报 ——
    L.append("无生存偏差线（分钟研究能不能开始，看这一组）")
    cov = d["coverage"]
    for key, label in (("delisted", "退市股  "), ("gap_other", "其他缺口")):
        c = d["cohort"].get(key, {"done": 0, "total": 0})
        f = c["done"] / c["total"] if c["total"] else 0
        cr = cov.get(key)
        crs = f"· 只-日覆盖 {cr:.4%}" if cr is not None else ""
        L.append(f"  {label} {bar(f, 26, ascii_mode)} {f:6.1%}  "
                 f"{c['done']:4,}/{c['total']:4,} {crs}")
    c = d["cohort"].get("survivor", {"done": 0, "total": 0})
    f = c["done"] / c["total"] if c["total"] else 0
    cr = cov.get("survivor")
    crs = f"· 只-日覆盖 {cr:.4%}" if cr is not None else ""
    L.append(f"  存续股   {bar(f, 26, ascii_mode)} {f:6.1%}  "
             f"{c['done']:4,}/{c['total']:4,} {crs}")
    gap_done = (d["cohort"].get("delisted", {}).get("done", 0)
                + d["cohort"].get("gap_other", {}).get("done", 0))
    gap_tot = (d["cohort"].get("delisted", {}).get("total", 0)
               + d["cohort"].get("gap_other", {}).get("total", 0))
    if gap_tot and gap_done == gap_tot:
        L.append(f"  → 退市侧 {gap_done}/{gap_tot} 已封顶：**现在就能用无偏样本做研究**，"
                 f"剩下 {d['pend_syms']:,} 只只影响覆盖面")
    L.append("")

    tf = d["today_rows"] / d["today_budget"] if d["today_budget"] else 0
    L.append(f"今日   {bar(tf, ascii_mode=ascii_mode)} {tf:6.1%}  "
             f"{d['today_rows']/1e6:5.1f}M / {d['today_budget']/1e6:.0f}M 行（本轮预算）")
    if d["rate"]:
        L.append(f"吞吐   {d['rate']:,.0f} 行/s（近 10 分钟实测）")
    if d["eta_h"] is not None:
        eta = datetime.now() + timedelta(hours=d["eta_h"])
        d_ = d["eta_h"]
        L.append(f"ETA    {d['pend_rows']/1e6:.1f}M 行 ≈ {d_/24:.1f} 天"
                 f"（{d_:.1f} 小时）→ 约 {eta:%m-%d %H:%M}")
    else:
        L.append("ETA    无近 10 分钟吞吐读数（进程可能刚起或已停）")

    idle = d["idle_sec"]
    if idle is None:
        health = "没有落地文件"
    elif idle < 180:
        health = f"最后写入 {idle:.0f}s 前 · 正常"
    elif idle < 1800:
        health = f"最后写入 {idle/60:.1f} 分钟前 · 偏慢（单只 5 年约 50-80s，3 并发下属正常）"
    else:
        health = f"最后写入 {idle/60:.0f} 分钟前 · **可能已停**（检查进程/封禁）"
    if d["blacklist_recent"]:
        health += " · ⚠ 近 15 分钟有封禁记录"
    L.append(f"健康   {health}")
    L.append(f"落地   {d['files']:,} 个 parquet / {d['size_gib']:.2f} GiB")
    return "\n".join(L)


# ---------------------------------------------------------------- HTML 看板

HTML_TMPL = """<!DOCTYPE html>
<meta charset="utf-8">
<title>5min 回补进度</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--sub:#6b7280;--line:#e5e7eb;--bar:#2563eb;--bar2:#94a3b8;--ok:#16a34a;--warn:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e8eaed;--sub:#9aa4b2;--line:#232833;--bar:#4a9eff;--bar2:#3f4753;--ok:#34d399;--warn:#f87171}}
*{box-sizing:border-box}
body{margin:0;padding:28px 22px;background:var(--bg);color:var(--fg);
 font:14px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:17px;font-weight:600;margin:0 0 4px}
.meta{color:var(--sub);font-size:12px;margin-bottom:22px;font-variant-numeric:tabular-nums}
.sec{margin:22px 0 8px;font-size:12px;letter-spacing:.06em;color:var(--sub);text-transform:uppercase}
.row{display:grid;grid-template-columns:88px 1fr 150px;gap:12px;align-items:center;margin:9px 0}
.lbl{font-size:13px}
.track{height:14px;background:var(--line);border-radius:7px;overflow:hidden}
.fill{height:100%;background:var(--bar);border-radius:7px;transition:width .6s ease}
.fill.done{background:var(--ok)}
.fill.mute{background:var(--bar2)}
.val{font-size:12px;color:var(--sub);text-align:right;font-variant-numeric:tabular-nums}
.big{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;margin:2px 0 12px}
.note{background:rgba(22,163,74,.08);border-left:3px solid var(--ok);
 padding:11px 14px;border-radius:0 6px 6px 0;margin:16px 0;font-size:13px}
.warnnote{background:rgba(220,38,38,.08);border-left:3px solid var(--warn);
 padding:11px 14px;border-radius:0 6px 6px 0;margin:16px 0;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
td{padding:5px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
td:last-child{text-align:right;color:var(--sub)}
</style>
<div class="wrap">
  <h1>5min 历史回补</h1>
  <div class="meta">快照 __NOW__ ｜ 页面按快照速率外推，仅供估时；<b>要更准就重新运行 --html</b></div>

  <div class="sec">总体（按行数）</div>
  <div class="big" id="pct">—</div>
  <div class="row"><div class="lbl">行数</div><div class="track"><div class="fill" id="b_rows"></div></div><div class="val" id="v_rows"></div></div>
  <div class="row"><div class="lbl">只数</div><div class="track"><div class="fill mute" id="b_syms"></div></div><div class="val" id="v_syms"></div></div>

  <div class="sec">无生存偏差线（研究可用性）</div>
  <div class="row"><div class="lbl">退市股</div><div class="track"><div class="fill done" id="b_del"></div></div><div class="val" id="v_del"></div></div>
  <div class="row"><div class="lbl">其他缺口</div><div class="track"><div class="fill done" id="b_gap"></div></div><div class="val" id="v_gap"></div></div>
  <div class="row"><div class="lbl">存续股</div><div class="track"><div class="fill mute" id="b_sur"></div></div><div class="val" id="v_sur"></div></div>
  <div id="biasnote"></div>

  <div class="sec">今日 / 吞吐</div>
  <table>
    <tr><td>本轮预算已用</td><td id="t_today">—</td></tr>
    <tr><td>实测吞吐（近 10 分钟）</td><td id="t_rate">—</td></tr>
    <tr><td>预计剩余</td><td id="t_eta">—</td></tr>
    <tr><td>落地体积</td><td id="t_size">—</td></tr>
    <tr><td>健康</td><td id="t_health">—</td></tr>
  </table>
</div>
<script>
const S = __DATA__;
const pct = (a,b)=> b>0 ? a/b : 0;
function setBar(id, frac){ document.getElementById(id).style.width = (Math.min(1,Math.max(0,frac))*100).toFixed(2)+"%"; }
function fmt(n){ return n>=1e6 ? (n/1e6).toFixed(1)+"M" : Math.round(n).toLocaleString(); }

function paint(){
  const el = Math.max(0, Date.now()/1000 - S.t0);      // 秒
  const grown = S.rate > 0 ? Math.min(S.rate*el, S.pend_rows) : 0;
  const rows = S.done_rows + grown;
  const total = S.total_rows;
  const f = pct(rows,total);
  document.getElementById("pct").textContent = (f*100).toFixed(1)+"%  ·  "+fmt(rows)+" / "+fmt(total)+" 行";
  setBar("b_rows", f);
  document.getElementById("v_rows").textContent = fmt(rows);
  // 只数也按同一速率外推：平均每只已完成的实测行数作除数，比「只数不动」自洽
  const avgRows = S.done_syms>0 ? S.done_rows/S.done_syms : 56000;
  const estSyms = Math.min(S.total_syms, S.done_syms + grown/avgRows);
  setBar("b_syms", pct(estSyms,S.total_syms));
  document.getElementById("v_syms").textContent = Math.round(estSyms).toLocaleString()+" / "+S.total_syms.toLocaleString();

  const c = S.cohort;
  [["b_del","v_del","delisted"],["b_gap","v_gap","gap_other"],["b_sur","v_sur","survivor"]].forEach(([b,v,k])=>{
    const x = c[k]||{done:0,total:0};
    const cr = (S.coverage&&S.coverage[k]!=null) ? " · 只-日 "+(S.coverage[k]*100).toFixed(3)+"%" : "";
    setBar(b, pct(x.done,x.total));
    document.getElementById(v).textContent = x.done.toLocaleString()+" / "+x.total.toLocaleString()+cr;
  });
  const gd=(c.delisted?c.delisted.done:0)+(c.gap_other?c.gap_other.done:0);
  const gt=(c.delisted?c.delisted.total:0)+(c.gap_other?c.gap_other.total:0);
  document.getElementById("biasnote").innerHTML = (gt && gd===gt)
    ? '<div class="note"><b>退市侧 '+gd+'/'+gt+' 已封顶。</b>无生存偏差的分钟样本此刻已经可用；剩下 '+Math.round(S.total_syms-estSyms).toLocaleString()+' 只只影响覆盖面，不影响偏差。</div>'
    : '<div class="warnnote">退市侧未封顶（'+gd+'/'+gt+'），此刻做的分钟研究仍有生存偏差。</div>';

  const used = S.today_rows + grown;
  document.getElementById("t_today").textContent = fmt(used)+" / "+(S.today_budget/1e6).toFixed(0)+"M 行";
  document.getElementById("t_rate").textContent = S.rate>0 ? Math.round(S.rate).toLocaleString()+" 行/s" : "无读数";
  if(S.rate>0){
    const left = Math.max(0, (total-rows)/S.rate);
    const h = left/3600, end = new Date(Date.now()+left*1000);
    document.getElementById("t_eta").textContent = (left>36*3600? (left/86400).toFixed(1)+" 天" : h.toFixed(1)+" 小时")
      + "  →  约 "+(end.getMonth()+1)+"-"+String(end.getDate()).padStart(2,"0")+" "+String(end.getHours()).padStart(2,"0")+":"+String(end.getMinutes()).padStart(2,"0");
  }
  document.getElementById("t_size").textContent = S.size_gib.toFixed(2)+" GiB · "+S.files.toLocaleString()+" 个文件";
  const age = S.idle_sec==null ? null : S.idle_sec + el;
  let h = age==null ? "没有落地文件" : (age<180 ? "最后写入 "+Math.round(age)+"s 前 · 正常"
        : (age<1800 ? "最后写入 "+(age/60).toFixed(1)+" 分钟前 · 偏慢" : "最后写入 "+(age/60).toFixed(0)+" 分钟前 · 可能已停"));
  if(S.blacklist_recent) h += " · 近 15 分钟有封禁记录";
  document.getElementById("t_health").textContent = h;
}
paint(); setInterval(paint, 1000);
</script>
"""


def write_html(d: dict, path: Path) -> Path:
    data = {k: v for k, v in d.items() if k != "now"}
    html = (HTML_TMPL
            .replace("__NOW__", d["now"])
            .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="每 N 秒刷新一次")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--html", action="store_true", help="生成自包含 HTML 看板")
    ap.add_argument("--out", default=str(OUT / "_progress.html"))
    ap.add_argument("--json", action="store_true", help="输出结构化读数（供其他程序读）")
    ap.add_argument("--ascii", action="store_true")
    ap.add_argument("--today-budget", type=int, default=None,
                    help="本轮预算；缺省从驱动日志的最后一个 round budget= 读")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.watch:
        cache = None
        first = True
        try:
            while True:
                d = collect(args.today_budget or auto_budget(),
                            with_tasks=first, task_cache=cache)
                if first:
                    cache = None  # tasks 结果不落地，watch 里只在变化时重算
                out = render(d, args.ascii)
                sys.stdout.write("\033[H\033[2J" + out + "\n")
                sys.stdout.flush()
                first = False
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n停止。")
        return

    budget = args.today_budget if args.today_budget else auto_budget()
    d = collect(budget)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return
    print(render(d, args.ascii))
    if args.html:
        p = write_html(d, Path(args.out))
        print(f"\nHTML 看板 → {p}")


if __name__ == "__main__":
    main()
