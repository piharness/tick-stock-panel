"""生成 HTML 报告：10 日涨 50% 的最佳形状。数据全部来自 data/_diag_surge50_shape_*.json。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "reports" / "surge50-shape-2026-11-19.html"

shapes = json.loads((DATA / "_diag_surge50_shape_table.json").read_text(encoding="utf-8"))
d5 = json.loads((DATA / "_diag_surge50_shape_step5.json").read_text(encoding="utf-8"))
d3 = json.loads((DATA / "_diag_surge50_shape_step3.json").read_text(encoding="utf-8"))
d6 = json.loads((DATA / "_diag_surge50_shape_step6.json").read_text(encoding="utf-8"))
thr = json.loads((DATA / "_diag_surge50_shape_step9.json").read_text(encoding="utf-8"))

# 月度基率（2010 起）
monthly = [m for m in d3["monthly"] if m["date"] >= "2010-01"]
mon_x = [m["date"] for m in monthly]
mon_y = [round(float(m["hit_pct"]), 3) for m in monthly]

# 逐年超额
yr = sorted(int(k) for k in d6["yearly"]["S1_首板"])
s1_y = [d6["yearly"]["S1_首板"][str(y)]["exc_pct"] for y in yr]
s5b_y = [d6["yearly"]["S5b_跳空启动强"].get(str(y), {}).get("exc_pct") for y in yr]

# 阈值扫描（S1）
s1_thr = [r for r in thr if r["who"] == "S1"]
thr_x = [r["thr"] for r in s1_thr]
thr_hit = [r["hit_pct"] for r in s1_thr]
thr_hitret = [r["hit_ret"] for r in s1_thr]
thr_missret = [r["miss_ret"] for r in s1_thr]

scatter = [
    {"name": s["shape"].split("_")[0], "value": [s["hit"], s["exc"]], "n": s["n"]}
    for s in shapes
]

table_rows = "".join(
    f"""<tr class="{'pos' if s['exc'] > 0 else 'neg'}">
      <td class="l">{s['shape']}</td>
      <td>{s['n']:,}</td><td>{s['perday']}</td>
      <td class="hi">{s['hit']:.2f}</td><td>{s['lift']:.2f}</td>
      <td>{s['hit_ret']:.1f}</td><td>{s['miss_ret']:+.2f}</td>
      <td>{s['fwd']:+.2f}</td><td class="strong">{s['exc']:+.2f}</td>
      <td>{s['exc_t']}</td><td>{s['untrad']:.2f}</td>
    </tr>"""
    for s in shapes
)

json_dump = lambda o: json.dumps(o, ensure_ascii=False)

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>10 日涨 50% 的最佳形状 — 全样本复盘</title>
<script>{{ECHARTS}}</script>
<style>
  :root {{
    --ink:#14181d; --ink2:#3d4650; --ink3:#6b7683; --line:#e3e6ea;
    --bg:#fbfbfa; --card:#ffffff; --up:#c0392b; --down:#1f7a4d; --accent:#1a4f8b;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.65; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:48px 28px 80px; }}
  .kicker {{ font-size:12px; letter-spacing:.14em; color:var(--ink3); text-transform:uppercase; }}
  h1 {{ font-size:32px; line-height:1.25; margin:10px 0 14px; letter-spacing:-.01em; }}
  .lede {{ font-size:16.5px; color:var(--ink2); max-width:820px; }}
  .lede b {{ color:var(--ink); }}
  .meta {{ font-size:12.5px; color:var(--ink3); margin-top:18px; padding-top:14px; border-top:1px solid var(--line); }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:32px 0 8px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 16px 14px; }}
  .card .v {{ font-size:26px; font-weight:650; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }}
  .card .k {{ font-size:12px; color:var(--ink3); margin-top:2px; }}
  .card .s {{ font-size:11.5px; color:var(--ink3); margin-top:8px; padding-top:8px; border-top:1px dashed var(--line); }}
  .v.neg {{ color:var(--down); }} .v.pos {{ color:var(--up); }}
  h2 {{ font-size:19px; margin:46px 0 6px; letter-spacing:-.01em; }}
  h2 .no {{ color:var(--ink3); font-weight:400; margin-right:8px; font-variant-numeric:tabular-nums; }}
  h3 {{ font-size:14.5px; margin:26px 0 6px; color:var(--ink2); }}
  p {{ color:var(--ink2); font-size:14.5px; }}
  .chart {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:6px; margin:16px 0 6px; }}
  .cap {{ font-size:12px; color:var(--ink3); margin:8px 2px 22px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; margin:14px 0 6px;
    background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden;
    font-variant-numeric:tabular-nums; }}
  th {{ text-align:right; font-weight:600; color:var(--ink3); font-size:11.5px;
    padding:10px 8px; border-bottom:1px solid var(--line); background:#f6f7f8; white-space:nowrap; }}
  td {{ text-align:right; padding:9px 8px; border-bottom:1px solid #eef0f2; }}
  td.l, th.l {{ text-align:left; }}
  tr:last-child td {{ border-bottom:none; }}
  tr.pos td.strong {{ color:var(--up); font-weight:650; }}
  tr.neg td.strong {{ color:var(--down); font-weight:650; }}
  td.hi {{ font-weight:600; }}
  .box {{ background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent);
    border-radius:8px; padding:16px 18px; margin:20px 0; }}
  .box .t {{ font-size:13px; font-weight:650; color:var(--accent); margin-bottom:6px; }}
  .box p {{ margin:4px 0; font-size:14px; }}
  code {{ background:#f1f3f5; padding:1px 5px; border-radius:4px; font-size:12.5px;
    font-family:"SF Mono",Consolas,monospace; }}
  .formula {{ text-align:center; font-family:"SF Mono",Consolas,monospace; font-size:14px;
    background:#f6f7f8; border:1px solid var(--line); border-radius:8px; padding:14px; margin:14px 0; }}
  ul {{ padding-left:20px; }} li {{ color:var(--ink2); font-size:14.5px; margin:6px 0; }}
  .two {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
  .disc {{ margin-top:52px; padding-top:16px; border-top:1px solid var(--line);
    font-size:11.5px; color:var(--ink3); }}
  @media (max-width:820px) {{ .cards {{ grid-template-columns:1fr 1fr; }} .two {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">

<div class="kicker">全样本复盘 · A 股日线 · 2005-01-04 → 2026-11-19</div>
<h1>10 日涨 50% 的最佳形状：先涨后抓必亏，因为「命中率」和「赔率」在这条分布上是对冲的</h1>
<p class="lede">15,316,400 个股票-日里，<b>14,761,400</b> 个可判定，<b>12,657</b> 个独立事件（4,529 只）。
我把「形状」拆成 9 种可计算的定义，逐一检验它们的命中率、失败端亏损、10 日超额。
结论不是「哪种形状最好」，而是：<b>命中率最高的形状，超额最差，且 17 年无一年例外。</b></p>
<div class="meta">数据：本地日K <code>kline_daily_enriched</code>，前复权序列，日历相邻校验；严格样本剔除上市 &lt;60 日、无 10 日历史、停牌。
基准：全市场等权同区间收益。显著性按「日频序列」计算，不按股票-日（后者 z 值虚高 10–30 倍）。</div>

<div class="cards">
  <div class="card"><div class="v">0.42%</div><div class="k">基率（严格样本）</div><div class="s">每 236 个股票-日出现 1 次</div></div>
  <div class="card"><div class="v">3.61%</div><div class="k">最高命中形状 · S5b</div><div class="s">8.5× 基率，但 10 日超额 −2.13%</div></div>
  <div class="card"><div class="v neg">−2.13%</div><div class="k">S5b 的 10 日超额</div><div class="s">t = −22.7，2012–2026 年年为负</div></div>
  <div class="card"><div class="v">3,207×</div><div class="k">月份之间的基率差</div><div class="s">2024-09 为 9.62%，2011-09 为 0.003%</div></div>
</div>

<h2><span class="no">01</span>先确认口径：50% 是累计涨幅，不是乖离率</h2>
<p>「10 日累计偏离 50%」有两种读法。若指 10 日乖离率 <code>close/MA10−1 ≥ 50%</code>，全样本只有 <b>1,566</b> 个股票-日；
若指 10 个交易日累计涨幅 <code>close_t/close_{{t−10}}−1 ≥ 50%</code>，有 <b>72,848</b> 个股票-日（含重叠），去重后 <b>12,657</b> 个独立事件。两者差 46 倍，本报告采用后者。</p>
<p>事件不是一个稀有到无法研究的样本：4,529 只票（占在册 81%）至少发生过一次，中位涨幅 56.6%，中位换手 6.5%，<b>50.4% 的事件日当天是涨停</b>。</p>

<h2><span class="no">02</span>九种形状的排名：命中和赚钱是反向的</h2>
<p>每种形状只用 t 日收盘及以前的信息。按 10 日超额排序，最短的那一列不是命中率，而是失败端的亏损。</p>
<table>
  <thead><tr><th class="l">形状</th><th>样本</th><th>只/日</th><th>命中%</th><th>lift</th><th>命中端%</th><th>失败端%</th><th>10日%</th><th>超额%</th><th>t</th><th>买不到%</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
<div class="cap">「命中端」= 10 日内最高收盘涨 ≥50% 的样本，其到第 10 日收盘的平均收益；「失败端」= 未命中的样本同口径收益。
「买不到」= T+1 开盘涨幅达到涨停幅度（一字板）的比例。「只/日」= 平均每日可选只数。</div>

<div class="chart" id="c1" style="height:400px"></div>
<div class="cap">横轴命中率，纵轴 10 日超额收益（相对全市场等权）。点大小按样本量。两组点被一条向下的斜面分开：
命中率越高的形状，超额越负；唯一落在零线以上的三个形状，命中率都低于基率。</div>

<h2><span class="no">03</span>为什么这是数学必然，不是巧合</h2>
<p>命中样本的收益被定义锁死在 50% 附近（实测 46.9%–56.1%）。所以期望收益只由两件事决定：命中率 <code>p</code>，和失败端的平均亏损 <code>m</code>。</p>
<div class="formula">E[10日收益] = p × 命中端 + (1 − p) × 失败端</div>
<p>S5b 的 p = 3.61%、命中端 = 49.95%、失败端 = −3.32%，代入得 <b>−1.40%</b>。S1 的 p = 2.17%、命中端 = 49.85%、失败端 = −0.75%，代入得 <b>+0.34%</b>。
想让 S5b 打平，失败端必须从 −3.32% 收到 −1.9%，或者命中率从 3.61% 提到 7.4%——两者都没有在任何子集里出现过。</p>
<div class="box">
  <div class="t">最反直觉的一步：阈值换了，期望一点不变</div>
  <p>把「成功」从 50% 改成 10%：S1 的命中率从 2.17% 跳到 38.48%，但命中端从 49.85% 塌到 11.40%，失败端从 −0.75% 恶化到 −6.57%。
  加权和仍然是 <b>+0.34%</b>，一分不差。阈值只决定你把分布的哪一段叫「成功」，不改变策略本身的期望。</p>
</div>
<div class="chart" id="c2" style="height:360px"></div>
<div class="cap">S1 首板：阈值从 10% 扫到 100%，命中率（左轴）与命中端/失败端收益（右轴）。三条线的乘积恒为常数。</div>

<h2><span class="no">04</span>时间比形状重要 3,207 倍</h2>
<p>按月统计「未来 10 日出现 50% 涨幅」的股票占比，最高是 2024-09 的 <b>9.62%</b>，最低是 2011-09 的 <b>0.003%</b>，中位月 0.255%。
在 2024-09，「什么形状」几乎不重要；在 2011-09，什么形状都没用。任何形状的 lift 都要乘在这个 3,207 倍的基底上。</p>
<div class="chart" id="c3" style="height:340px"></div>
<div class="cap">月度基率（对数轴）。2015 与 2024 的两个尖峰不是形状研究能解释的，它们是制度性行情。</div>

<h2><span class="no">05</span>事后追入：钱在确认日之前就已经走了</h2>
<p>以「涨完 50% 的当天收盘」为确认日 T，T+1 开盘买入：</p>
<table>
  <thead><tr><th class="l">持有期</th><th>样本</th><th>平均</th><th>中位</th><th>胜率</th><th>期间最大回撤中位</th></tr></thead>
  <tbody>
    <tr><td class="l">5 日</td><td>12,223</td><td>−1.66%</td><td>−4.90%</td><td>36.2%</td><td>−9.6%</td></tr>
    <tr><td class="l">10 日</td><td>11,994</td><td>−0.45%</td><td>−4.82%</td><td>37.4%</td><td>−14.5%</td></tr>
    <tr><td class="l">20 日</td><td>11,664</td><td>−0.89%</td><td>−6.33%</td><td>37.8%</td><td>−20.7%</td></tr>
  </tbody>
</table>
<p>唯一例外是北交所：20 日平均 <b>+12.9%</b>、胜率 55.1%，30% 涨跌幅制度让「涨 50%」只等于两个板，事件的性质完全不同。主板 20 日为 −3.0%，科创板 −2.2%，创业板 +0.1%。</p>

<h2><span class="no">06</span>路径：一半的 50% 根本没有涨停</h2>
<table>
  <thead><tr><th class="l">路径</th><th>事件数</th><th>占比</th><th>10日涨幅中位</th><th>事件日换手中位</th><th>T+1买 10日均值</th><th>10日中位</th></tr></thead>
  <tbody>
    <tr><td class="l">A 连板型（≥3 板）</td><td>2,921</td><td>23.1%</td><td>58.8%</td><td>3.69%</td><td>+2.64%</td><td>−6.83%</td></tr>
    <tr><td class="l">B 加速型（无涨停）</td><td>6,280</td><td>49.6%</td><td>55.5%</td><td>7.15%</td><td>−1.10%</td><td>−3.98%</td></tr>
    <tr><td class="l">C 阶梯型（1–2 板）</td><td>3,456</td><td>27.3%</td><td>56.9%</td><td>7.30%</td><td>−1.85%</td><td>−5.42%</td></tr>
  </tbody>
</table>
<p>连板型的均值 +2.64% 全靠右尾，中位是 −6.83%；而且它的事件日换手只有 3.69%，意味着一字板居多、根本买不到。
占一半的加速型（无涨停、换手 7.15%）是双创 20% 制度下的连续上涨，追入后 10 日平均 −1.10%。</p>

<h2><span class="no">07</span>结论：如果一定要给一个「最佳形状」</h2>
<div class="box">
  <div class="t">按命中率排</div>
  <p><b>S5b 跳空启动</b>：20 日内 ≥3 次跳空 + 当日涨幅 &gt;9%。命中率 3.61%，8.5 倍基率，每日约 17 只。
  代价：10 日超额 −2.13%（t=−22.7），2012–2026 年<strong>每一年</strong>都为负，失败端 −3.32%。这是「看得见、吃不到」的标准形态。</p>
</div>
<div class="box">
  <div class="t">按不亏钱排</div>
  <p><b>S9 高位横盘</b>：距 250 日高 &lt;5% + 20 日振幅处于当日最低 30% 分位。10 日超额 +0.53%（t=5.69），每日约 25 只，成交额分位 0.63、换手分位 0.24（容量不差）。
  但它命中率 0.34%，<b>低于基率</b>——它抓不到 50%，它只是「低波动 + 高位置」的慢牛，2025 年也失效过（−0.65%，t=−4.9）。</p>
</div>
<div class="box">
  <div class="t">如果只想要「不亏且尽量高命中」</div>
  <p><b>S1 低位首板</b>：当日涨停、前 4 日涨幅 &lt;5%、距 250 日高 −20% 以下、换手分位 &lt;0.5、高波动。
  命中率 1.90%（4.5× 基率），失败端 <b>+1.12%</b>（正的），期望 +0.9%/10 日、超额 ≈ 0（−0.006%，t=1.59）。
  但这是 54 个子集里挑出来的，15 年里只有 2015 年（+3.51%）和 2022/2024（+0.4%/0.0%）不亏，每日中位只有 2 只。它是「2015 年的记忆」，不是可复制的形状。</p>
</div>

<h3>真正可执行的三条</h3>
<ul>
  <li><b>把「跳空启动」「首板」当排除项，而不是买入项。</b>它们是最稳定的负 alpha（17 年全负，t≈−21），在别的策略里可以把它们从候选池里剔掉。</li>
  <li><b>要 50% 的命中率，就得接受负期望；要正期望，就得放弃命中率。</b>两者在同一条分布上是对冲的，不存在同时优化的点——这是 14.7M 行数据给出的恒等式，不是参数没调好。</li>
  <li><b>先做时间，再做形状。</b>月基率 0.003%→9.62% 的摆动，大于任何形状的 lift。在基率低于 0.1% 的月份，所有形状的绝对期望都是负的。</li>
</ul>

<h2><span class="no">08</span>可证伪的验收标准</h2>
<ul>
  <li>任一候选形状，必须在<b>日频序列</b>上给出 |t| &gt; 3 的超额，而不是股票-日 z 值。</li>
  <li>必须在 2012–2026 的<b>每一年</b>都不为负，或至少负年份不超过 2 个；只靠 2015/2024 两年撑起来的形状一律不采信。</li>
  <li>必须报告<b>失败端平均亏损</b>；只报命中率的形状视为未完成。</li>
  <li>必须报告<b>买不到比例</b>（T+1 涨停开盘占比）；超过 0.5% 的形状，其超额要按可成交子样本重算。</li>
  <li>任何在 30 个以上子集里挑出来的最优组合，需通过 Bonferroni 门槛（|t| &gt; 4.9），否则只当假设。</li>
</ul>

<div class="disc">
数据来源：本地 <code>kline_daily_enriched</code>（前复权，5,581 只，2005-01-04 至 2026-11-19，15,316,400 行）。
已知偏差：本地日K 只含「今天仍在册」的股票，退市股不在样本内，事后收益偏乐观；ST 与新股已做剔除但不完全。
脚本：<code>analysis/_diag_surge50_shape_step1.py</code> … <code>step9.py</code>；中间结果 <code>data/_diag_surge50_shape_*.json</code>。
<br><br>
<b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。
</div>
</div>

<script>
var mono = getComputedStyle(document.body).fontFamily;
var AXIS = {{ axisLine:{{lineStyle:{{color:'#c9ced5'}}}}, axisLabel:{{color:'#6b7683',fontSize:11}},
  splitLine:{{lineStyle:{{color:'#eef0f2'}}}} }};

var c1 = echarts.init(document.getElementById('c1'));
c1.setOption({{
  grid:{{left:64,right:36,top:34,bottom:56}},
  tooltip:{{trigger:'item', formatter:function(p){{ var d=p.data; return d.name+'<br/>命中率 '+d.value[0]+'%<br/>超额 '+d.value[1]+'%<br/>样本 '+d.n.toLocaleString(); }}}},
  xAxis:Object.assign({{}}, AXIS, {{type:'value', name:'命中率 %', nameLocation:'middle', nameGap:34,
    nameTextStyle:{{color:'#6b7683',fontSize:11}}, min:0, max:4.2}}),
  yAxis:Object.assign({{}}, AXIS, {{type:'value', name:'10 日超额 %', nameGap:16,
    nameTextStyle:{{color:'#6b7683',fontSize:11}}, min:-2.4, max:0.8}}),
  series:[{{
    type:'scatter',
    data:{json_dump(scatter)},
    symbolSize:function(v,d){{ return Math.max(14, Math.min(46, Math.sqrt(d.n)/18)); }},
    itemStyle:{{ color:function(p){{ return p.data.value[1] > 0 ? 'rgba(192,57,43,.72)' : 'rgba(31,122,77,.72)'; }},
      borderColor:'#fff', borderWidth:1.5 }},
    label:{{show:true, position:'right', formatter:function(p){{return p.data.name;}},
      color:'#3d4650', fontSize:12, fontWeight:600}},
    markLine:{{ silent:true, symbol:'none', lineStyle:{{color:'#9aa3ad', type:'dashed'}},
      data:[{{yAxis:0, label:{{formatter:'超额 0', color:'#6b7683', fontSize:10}}}}] }}
  }}]
}});

var c2 = echarts.init(document.getElementById('c2'));
c2.setOption({{
  grid:{{left:64,right:64,top:46,bottom:52}},
  legend:{{top:6, textStyle:{{color:'#6b7683',fontSize:11}}}},
  tooltip:{{trigger:'axis'}},
  xAxis:Object.assign({{}}, AXIS, {{type:'category', data:{json_dump(thr_x)},
    name:'成功阈值 %', nameLocation:'middle', nameGap:32, nameTextStyle:{{color:'#6b7683',fontSize:11}}}}),
  yAxis:[
    Object.assign({{}}, AXIS, {{type:'value', name:'命中率 %', nameTextStyle:{{color:'#6b7683',fontSize:11}}}}),
    Object.assign({{}}, AXIS, {{type:'value', name:'收益 %', nameTextStyle:{{color:'#6b7683',fontSize:11}}}})
  ],
  series:[
    {{ name:'命中率', type:'line', data:{json_dump(thr_hit)}, smooth:true,
       lineStyle:{{width:2.4,color:'#1a4f8b'}}, itemStyle:{{color:'#1a4f8b'}} }},
    {{ name:'命中端收益', type:'line', yAxisIndex:1, data:{json_dump(thr_hitret)}, smooth:true,
       lineStyle:{{width:2,color:'#c0392b'}}, itemStyle:{{color:'#c0392b'}} }},
    {{ name:'失败端收益', type:'line', yAxisIndex:1, data:{json_dump(thr_missret)}, smooth:true,
       lineStyle:{{width:2,color:'#1f7a4d'}}, itemStyle:{{color:'#1f7a4d'}} }}
  ]
}});

var c3 = echarts.init(document.getElementById('c3'));
c3.setOption({{
  grid:{{left:64,right:36,top:34,bottom:56}},
  tooltip:{{trigger:'axis', formatter:function(p){{ return p[0].axisValue+'<br/>基率 '+p[0].value+'%'; }}}},
  xAxis:Object.assign({{}}, AXIS, {{type:'category', data:{json_dump(mon_x)}, boundaryGap:false,
    axisLabel:{{color:'#6b7683',fontSize:10, interval:35}}}}),
  yAxis:Object.assign({{}}, AXIS, {{type:'log', name:'月基率 %', nameGap:14,
    nameTextStyle:{{color:'#6b7683',fontSize:11}}, min:0.002, max:12}}),
  series:[{{
    type:'line', data:{json_dump(mon_y)}, showSymbol:false, smooth:false,
    lineStyle:{{width:1.6,color:'#1a4f8b'}},
    areaStyle:{{color:'rgba(26,79,139,.08)'}}
  }}]
}});

window.addEventListener('resize', function(){{ c1.resize(); c2.resize(); c3.resize(); }});
</script>
</body>
</html>
"""

echarts_js = (ROOT / "frontend" / "node_modules" / "echarts" / "dist" / "echarts.min.js").read_text(encoding="utf-8")
HTML = HTML.replace("{ECHARTS}", echarts_js)
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(HTML, encoding="utf-8")
print("written", OUT, len(HTML))
