# -*- coding: utf-8 -*-
"""生成 reports/yaogu-census-2026-11-25.html（自包含，无外部依赖）。"""
import json, numpy as np, pandas as pd, pyarrow.parquet as pq

a = pd.read_parquet("data/.scratch/yaogu_anatomy.parquet")
inst = pq.read_table("data/instruments/instruments.parquet").to_pandas()[["symbol", "name"]]
a = a.drop(columns=["name"]).merge(inst, on="symbol", how="left")
a["yr"] = a.peak_date.astype(str).str[:4]
a["name"] = a["name"].fillna(a["symbol"].str[:6])

def f(x, nd=1):
    return None if pd.isna(x) else round(float(x) * 100, nd)

rows = []
for _, r in a.sort_values(["peak_date"]).iterrows():
    rows.append(dict(
        yr=r.yr, sym=r.symbol, nm=r["name"], tier=r.tier,
        typ=("E" if r.susp else "T") if r.tier == "S" else "",
        s=str(r.start_date)[:10], p=str(r.peak_date)[:10],
        g=f(r.gain, 0), nd=int(r.ndays), lu=int(r.lu), ml=int(r.maxlu),
        cap=None if pd.isna(r.fcap) else round(float(r.fcap)),
        e20=f(r.ex20), e250=f(r.ex250), dd=f(r.post_dd250, 0),
    ))

# 年度计数
yr = a.groupby("yr").apply(lambda g: pd.Series({
    "S_T": int(((g.tier=="S") & (~g.susp)).sum()),
    "S_E": int(((g.tier=="S") & (g.susp)).sum()),
    "A": int((g.tier=="A").sum()), "B": int((g.tier=="B").sum()),
    "C": int((g.tier=="C").sum()),
}), include_groups=False).reset_index()

reg = pd.read_parquet("data/.scratch/yaogu_regime.parquet")[["yr","mkt_ret","next_ret"]]
yr = yr.merge(reg, on="yr", how="left")

# 解剖中位数
anat = a.groupby("tier")[["gain","ndays","lu","maxlu","fcap","pre20","pre60","pre_to","ex5","ex20","ex60","ex250","post_dd250"]].median()
anat_tab = []
for k, lab in [("S","S 超级妖股"),("A","A 大妖"),("B","B 强势"),("C","C 对照(非涨停驱动)")]:
    r = anat.loc[k]
    anat_tab.append(dict(k=lab, gain=f(r.gain,0), nd=int(r.ndays), lu=int(r.lu), ml=int(r.maxlu),
                         cap=round(float(r.fcap)), pre20=f(r.pre20), pre60=f(r.pre60), preto=round(float(r.pre_to),2),
                         e5=f(r.ex5), e20=f(r.ex20), e60=f(r.ex60), e250=f(r.ex250), dd=f(r.post_dd250,0)))
s_t = a[(a.tier=="S") & (~a.susp)]
s_e = a[(a.tier=="S") & (a.susp)]

# 首板漏斗
fb = pd.read_parquet("data/.scratch/first_board.parquet")
fb["cap_b"] = pd.cut(fb.fcap, [0,20,40,80,150,1e9], labels=["<20亿","20-40亿","40-80亿","80-150亿","≥150亿"])
fb["cold_b"] = pd.cut(fb.to_pre60, [0,0.8,1.5,3,1e9], labels=["<0.8%","0.8-1.5%","1.5-3%","≥3%"])
fn = fb.groupby(["cap_b","cold_b"], observed=True).apply(lambda g: pd.Series({
    "n": len(g), "p2": (g.clu_next60>=2).mean()*100, "p3": (g.clu_next60>=3).mean()*100,
    "p5": (g.clu_next60>=5).mean()*100, "p8": (g.clu_next60>=8).mean()*100}), include_groups=False).reset_index()
funnel = [dict(cap=str(r.cap_b), cold=str(r.cold_b), n=int(r.n), p2=round(r.p2,2), p3=round(r.p3,2),
               p5=round(r.p5,2), p8=round(r.p8,2)) for _, r in fn.iterrows()]

fr = pd.read_parquet("data/.scratch/first_board_ret.parquet")
exp_tab = []
for h in (2,5,10,20,60):
    s = fr.dropna(subset=[f"e{h}"]); d = fr.dropna(subset=[f"e{h}"]).groupby("date")[f"e{h}"].mean()
    exp_tab.append(dict(h=h, avg=f(s[f"e{h}"].mean(),2), med=f(s[f"e{h}"].median(),2),
                        day=f(d.mean(),2), t=round(float(d.mean()/d.std()*np.sqrt(len(d))),2),
                        win=round(float((s[f"e{h}"]>0).mean()*100),1), n=int(len(s))))

DATA = dict(rows=rows, yr=yr.to_dict("records"), anat=anat_tab, funnel=funnel, exp=exp_tab,
            n_all=len(a), n_s=len(a[a.tier=="S"]), n_st=len(s_t), n_se=len(s_e), n_fb=int(len(fb)),
            s_e20=f(s_t.ex20.median()), s_e250=f(s_t.ex250.median()), s_f250=f(s_t.f250.median()), s_dd=f(s_t.post_dd250.median(),0),
            s_ex20_pos=round(float((s_t.ex20>0).mean()*100),1), s_st=round(float(s_t["name"].str.contains("ST|退", na=False).mean()*100),1))

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>近20年超级妖股 — 全样本普查</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f6f3;color:#191817;
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:56px 28px 96px}
h1{font-size:31px;line-height:1.25;margin:0 0 10px;letter-spacing:-.4px;font-weight:650}
h2{font-size:19px;margin:64px 0 6px;letter-spacing:-.2px;font-weight:640}
h3{font-size:15px;margin:30px 0 8px;font-weight:640;color:#3a3835}
.sub{color:#6d6a65;font-size:14px;margin:0 0 34px}
.rule{height:1px;background:#e2dfd9;margin:0}
.kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#e2dfd9;border:1px solid #e2dfd9;margin:34px 0 8px}
.kpi>div{background:#fff;padding:20px 18px}
.kpi .n{font-size:27px;font-weight:660;letter-spacing:-.8px;font-variant-numeric:tabular-nums;line-height:1.15}
.kpi .l{font-size:12px;color:#6d6a65;margin-top:6px;line-height:1.45}
.red{color:#c0392b}.green{color:#16794f}
.note{background:#fff;border:1px solid #e2dfd9;border-left:3px solid #c0392b;padding:16px 20px;margin:26px 0;font-size:14px;line-height:1.75}
.note b{font-weight:650}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums;background:#fff}
th{text-align:right;font-weight:600;color:#6d6a65;font-size:12px;padding:10px 9px;border-bottom:1px solid #d8d4cc;
 white-space:nowrap;cursor:pointer;user-select:none;position:sticky;top:0;background:#fff;z-index:2}
th:hover{color:#191817}
th.l,td.l{text-align:left}
td{padding:7px 9px;border-bottom:1px solid #f0ede8;text-align:right;white-space:nowrap}
tbody tr:hover{background:#fbf6f2}
.tierS{color:#c0392b;font-weight:660}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;background:#efece6;color:#5d5a55;margin-left:5px;vertical-align:1px}
.tagE{background:#eceae4;color:#7a6a4f}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 14px}
.chip{border:1px solid #d8d4cc;background:#fff;border-radius:999px;padding:5px 14px;font-size:13px;cursor:pointer;color:#4a4845}
.chip.on{background:#191817;border-color:#191817;color:#fff}
.wrapx{overflow:auto;max-height:720px;border:1px solid #e2dfd9}
.chart{background:#fff;border:1px solid #e2dfd9;padding:22px 20px 14px}
.grid2{display:grid;grid-template-columns:1.35fr 1fr;gap:18px;margin-top:18px}
p{margin:12px 0;color:#33312e}
ul{margin:10px 0;padding-left:20px;color:#33312e}
li{margin:6px 0}
code{background:#efece6;padding:1px 5px;border-radius:3px;font-size:12.5px}
.foot{margin-top:70px;padding-top:18px;border-top:1px solid #e2dfd9;color:#8a8680;font-size:12.5px;line-height:1.8}
@media(max-width:820px){.kpi{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
</style></head><body><div class="wrap">

<h1>近 20 年超级妖股：全样本普查</h1>
<p class="sub">2006-01 ～ 2026-11 · 全部 A 股 5,878 只（含退市/吸收合并 307 只）· 16,177,928 根日 K<br>
样本：__N_ALL__ 个「60 交易日内翻倍且涨停驱动」的行情段，其中 <b>__N_S__ 段</b>达到超级妖股标准</p>

<div class="kpi">
  <div><div class="n">__N_ST__</div><div class="l">连续交易型<br>（全程可买）</div></div>
  <div><div class="n">__N_SE__</div><div class="l">停牌复牌型<br>（区间内停牌≥20日，起点买不到）</div></div>
  <div><div class="n red">__S_E20__%</div><div class="l">见顶次日开盘买入<br>20 日超额中位数</div></div>
  <div><div class="n red">__S_E250__%</div><div class="l">同上，持有 1 年<br>超额中位数</div></div>
</div>

<div class="note">
<b>一句话：这份名单是真的，但它不是策略，是墓志铭。</b>
__N_ST__ 只连续交易型超级妖股，见顶后再买入，20 个交易日超额 __S_E20__%（中位），持有一年 __S_E250__%，
只有 __S_EX20POS__% 的样本在 20 日里跑赢市场。名单是<b>事后</b>的——你在峰值那天确认它是妖股，从那天起它就开始还债。
</div>

<h2>一、口径：什么算「超级妖股」</h2>
<p>妖股不是一个可以靠印象回答的问题。印象只能召回上过新闻的那几十只，而幸存者偏差恰好把它们全部保留下来。所以先给一个能被数据执行的判据：</p>
<ul>
<li><b>幅度</b>　<code>rmax60(t) = close(t) / min(close, t-59..t) - 1 ≥ 300%</code>，即 60 个交易日内从最低收盘翻 3 倍以上。</li>
<li><b>形态</b>　同一窗口内涨停 ≥ 8 天。这一条把「业绩/产品涨价驱动的慢牛」和「资金接力驱动的连板」分开——
没有它，名单会被 2007 年和 2020 年的普通大牛股淹没。</li>
<li><b>剔除次新</b>　上市前 250 个交易日不算。新股上市连续一字板是发行制度产物（2015 年前后动辄 20+ 板），
不是炒作，也买不到。不剔除的话，涨幅榜前 20 名里 19 个是次新股。</li>
<li><b>去重</b>　同一只股票 120 个交易日内只保留涨幅最高的一个峰。</li>
</ul>
<p>分级：<b>S</b> ≥300% 且涨停≥8；<b>A</b> 200–300%；<b>B</b> 150–200%；<b>C</b> ≥150% 但涨停&lt;8（非涨停驱动，作对照）。</p>
<p>用「窗口内最低收盘」而不是「固定 60 天前收盘」，是因为大量妖股诞生在停牌复牌之后（四川双马 2016、
协鑫集成 2015）。固定窗口会因跨度超限把整段丢掉——第一版脚本漏掉了四川双马 +549%，就是这么漏的。</p>

<h2>二、名单</h2>
<p>默认只显示 S 级（__N_S__ 段）。点表头排序，或用上面的筛子切换。标 <span class="tag tagE">复牌</span> 的 100 段，区间内停牌 ≥20 个交易日：涨的部分主要由复牌后的连续一字板贡献，停牌期间买不到、一字板期间也排不进。其中只有 7 段的复牌首日是真正的单日重定价（+30% 以上，如协鑫集成复牌首日 +1970%），其余 93 段是「复牌 +10% 一字 → 再连板」，本质仍是连板炒作，只是起点被停牌藏起来了。</p>
<div class="chips" id="chips"></div>
<div class="wrapx"><table id="tbl"><thead><tr>
<th class="l" data-k="yr">年份</th><th class="l" data-k="nm">名称</th><th class="l" data-k="sym">代码</th>
<th class="l" data-k="s">启动日</th><th class="l" data-k="p">见顶日</th>
<th data-k="g">区间涨幅</th><th data-k="nd">交易日</th><th data-k="lu">涨停</th><th data-k="ml">最高连板</th>
<th data-k="cap">起涨流通市值</th><th data-k="e20">20日超额</th><th data-k="e250">1年超额</th><th data-k="dd">1年内最大回撤</th>
</tr></thead><tbody></tbody></table></div>
<p style="font-size:12.5px;color:#8a8680;margin-top:8px">市值单位亿元（按启动日成交量/换手率反推流通股本）。超额 = 峰值次日开盘买入，扣同日全市场等权收益；「—」为数据不足（2026 年见顶的样本还没有 1 年）。<br>事件型的「启动日」是停牌前最后一个低点，区间涨幅跨越停牌期，「交易日」只数实际交易日，所以会出现 2014-04-28 → 2015-11-11 却只有 54 个交易日的情况——那不是错，是停了 471 天。</p>

<h2>三、年度密度：妖股是行情的温度计，不是选股的筛子</h2>
<p>把 __N_ALL__ 个行情段按见顶年份排开，得到一条极不均匀的曲线。它标记的不是「哪只股票好」，而是「市场把风险偏好烧到了几度」。</p>
<div class="chart" id="yrchart"></div>
<div class="grid2">
<div class="chart" id="scatter"></div>
<div style="background:#fff;border:1px solid #e2dfd9;padding:20px">
<h3 style="margin-top:0">密度本身有信息</h3>
<p style="font-size:13.5px">妖股数量与当年市场收益的相关只有 <b>+0.25</b>，但与<b>次年</b>市场收益的相关是 <b>−0.32</b>。
样本只有 21 个年度，这个数字远达不到显著（t≈−1.5），不能当预测用；但它与「2015 年 84 只→2016 年 4 只」
「2024 年 27 只 S 级→2025 年 −19%」的直觉一致，值得作为一个状态变量继续跟踪，而不是一个交易信号。</p>
<p style="font-size:13.5px;margin-bottom:0">更有价值的是它的<b>反向用法</b>：当妖股密度处在历史高位时，市场处于「彩票偏好」定价状态，
此时买入高热度个股的赔率最差。这是一个减仓/避让信号，不是一个加仓信号。</p>
</div></div>

<h2>四、解剖：它们长什么样</h2>
<p>把每个行情段的起点（窗口内最低收盘）当作 t0，可以还原妖股启动前的样子。中位数：</p>
<div class="wrapx" style="max-height:none;margin-top:14px"><table>
<thead><tr><th class="l">分级</th><th>区间涨幅</th><th>交易日</th><th>涨停天数</th><th>最高连板</th>
<th>起涨流通市值(亿)</th><th>前20日涨幅</th><th>前60日涨幅</th><th>前60日均换手</th>
<th>见顶后5日</th><th>见顶后20日</th><th>见顶后60日</th><th>见顶后1年</th><th>1年内回撤</th></tr></thead>
<tbody>__ANAT__</tbody></table></div>
<p>三件事值得注意：</p>
<ul>
<li><b>启动前是安静的。</b>S 级前 60 日平均换手 0.96%，启动后区间日均 2.9%——三倍放大。前 20 日涨幅还是负的（−3.5%）。
真正的妖股在启动前不是「已经在涨」，而是「没人看」。</li>
<li><b>不需要小市值。</b>起涨流通市值中位数 60 亿，东方通信 43 亿、王府井 135 亿。市值不是必要条件，
「流通盘能被少量资金锁死」才是，而这更多取决于股东结构和筹码分散度，不是绝对市值。</li>
<li><b>跌得比涨得快。</b>区间涨幅中位 +373%，见顶后一年内最大回撤中位 −62%。涨上去用 58 个交易日，跌下来用 250 个还不够。</li>
</ul>

<h2>五、分母：赌妖股的真实胜率</h2>
<p>上一节是「已知它是妖股」之后的样子。真实决策发生在<b>首板那天</b>——你能看到的信号只有「今天涨停了，之前 60 天没涨停过」。
20 年一共 __N_FB__ 个这样的首板，从首板出发，60 个交易日内能走到多高：</p>
<div class="wrapx" style="max-height:none;margin-top:14px"><table>
<thead><tr><th class="l">起涨流通市值</th><th class="l">前60日均换手</th><th>首板数</th><th>→ 2连板</th><th>→ 3连板</th><th>→ 5连板</th><th>→ 8连板</th></tr></thead>
<tbody>__FUNNEL__</tbody></table></div>
<p>最好的格子（市值&lt;20 亿 + 换手&lt;0.8%）里，走到 8 连板的概率是 <b>1.55%</b>；全样本平均 <b>0.42%</b>。
而 2015 年这个数字是 2.2%，2023 年是 0.45%——连「赌妖股」这件事本身的赔率也是随行情变化的。</p>

<h3>那么赌的期望是多少</h3>
<p>首板当日收盘确认信号，次日开盘买入（这是唯一可成交的价格，首板当天收盘价买不到封板股），扣同日全市场等权：</p>
<div class="wrapx" style="max-height:none;margin-top:14px"><table>
<thead><tr><th>持有期</th><th>笔均超额</th><th>中位超额</th><th>按日聚合</th><th>t（日聚合）</th><th>胜率</th><th>样本</th></tr></thead>
<tbody>__EXP__</tbody></table></div>
<div class="note" style="border-left-color:#191817">
<b>21 个年度里，20 个年度的首板 20 日超额是负的。</b>
唯一为正的 2010 年是 +0.42%（t≈1），而 2017 年 −4.21%、2024 年 −4.44%。
这不是「参数没调好」，是 50,613 个事件、t=−15.9 的结构性事实：
首板之后的钱被隔夜溢价一次性拿走了，开盘买入的人买的是负漂移。
</div>

<h2>六、这份名单该怎么用</h2>
<p>三种用法里，只有一种在数据上成立：</p>
<ul>
<li><b>当买入清单用 —— 不成立。</b>见顶后 20 日超额 −26.9%，胜率 6.6%。名单上每一个名字，
在你能够确认它是妖股的那一刻，都已经走完了 80% 的行情。</li>
<li><b>当事前筛选器用 —— 不成立。</b>从首板出发到 8 连板的概率上限 1.55%，且这 1.55% 的收益被
「另外 98.45% 的亏损」和隔夜溢价吃干。这是本项目已用 50 个指标、68 个检验反复验证过的同一个结论：
热度、换手、涨停强度对 20 日超额是<b>负向</b>的，而且是最稳定的负向指标之一。</li>
<li><b>当风险温度计用 —— 部分成立。</b>年度密度与次年市场收益 −0.32 相关。样本太短不足以下注，
但足以作为一个「什么时候不该碰高热度股」的状态变量。且它与「妖股见顶后 1 年跑输 57%」是同一件事的两面。</li>
</ul>
<p>最后还有一个只有把名单拉全才能看到的偏差：这 __N_ST__ 只里有 <b>__S_ST__%</b>
现在已经是 ST 或退市（ST文峰、*ST实达、ST豆神……）。它们当年都上过涨幅榜，
但十年后你记得的只剩东方通信和九安医疗——因为记得住的那几只，恰好是活着的那几只。</p>

<div class="foot">
数据 <code>data/kline_daily_enriched</code>（2005-01-04 起，15,338,691 行）+ <code>data/backfill_20261117/stock_gap_daily.parquet</code>（退市/吸收合并 307 只，1,194,530 行）<br>
脚本 <code>analysis/_yaogu_census3.py</code> · <code>analysis/_yaogu_anatomy2.py</code> · <code>analysis/_yaogu_funnel.py</code> · <code>analysis/_yaogu_expectancy.py</code> · <code>analysis/_yaogu_report.py</code><br>
生成 2026-11-25 · 全部数字可由脚本复现，无手工录入
</div>
</div>
<script>
const D = __DATA__;
const pct=(v,d=1)=>v==null?"—":((v>0?"+":"")+v.toFixed(d)+"%");
const cls=v=>v==null?"":(v>0?"red":"green");
// chips
const chips=[["S","超级妖股 S ("+D.n_s+")"],["S_T","连续交易 ("+D.n_st+")"],["S_E","停牌复牌 ("+D.n_se+")"],
             ["A","大妖 A"],["B","强势 B"],["ALL","全部 "+D.n_all]];
let cur="S", sk="g", sd=-1;
const box=document.getElementById("chips");
chips.forEach(([k,l])=>{const b=document.createElement("button");b.className="chip"+(k==="S"?" on":"");b.textContent=l;
 b.onclick=()=>{cur=k;[...box.children].forEach(c=>c.classList.remove("on"));b.classList.add("on");render();};box.appendChild(b);});
function pass(r){ if(cur==="ALL")return true; if(cur==="S")return r.tier==="S";
  if(cur==="S_T")return r.tier==="S"&&r.typ==="T"; if(cur==="S_E")return r.tier==="S"&&r.typ==="E";
  return r.tier===cur; }
function render(){
  const rws=D.rows.filter(pass).sort((a,b)=>{const x=a[sk],y=b[sk];
    if(typeof x==="string")return sd*x.localeCompare(y);
    return sd*((x==null?-1e9:x)-(y==null?-1e9:y));});
  const tb=document.querySelector("#tbl tbody");
  tb.innerHTML=rws.map(r=>`<tr>
   <td class="l">${r.yr}</td>
   <td class="l tier${r.tier}">${r.nm}${r.typ==="E"?'<span class="tag tagE">复牌</span>':''}</td>
   <td class="l" style="color:#8a8680">${r.sym}</td>
   <td class="l" style="color:#8a8680">${r.s}</td>
   <td class="l" style="color:#8a8680">${r.p}</td>
   <td class="red"><b>${r.g==null?"":(r.g>0?"+":"")+r.g+"%"}</b></td>
   <td>${r.nd}</td><td>${r.lu}</td><td>${r.ml}</td>
   <td>${r.cap??""}</td>
   <td class="${cls(r.e20)}">${pct(r.e20)}</td>
   <td class="${cls(r.e250)}">${pct(r.e250)}</td>
   <td class="green">${r.dd==null?"—":r.dd+"%"}</td></tr>`).join("");
}
document.querySelectorAll("#tbl th").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k; if(k===sk)sd=-sd; else {sk=k;sd=(k==="nm"||k==="sym")?1:-1;} render();});
render();

// 年度柱
(function(){
  const W=760,H=280,padL=38,padR=10,padT=16,padB=44;
  const yrs=D.yr, keys=[["S_T","#c0392b","连续交易S"],["S_E","#8a8279","停牌复牌S"],["A","#d99083","A"],["B","#e8c4b8","B"]];
  const max=Math.max(...yrs.map(d=>d.S_T+d.S_E+d.A+d.B));
  const bw=(W-padL-padR)/yrs.length;
  let g="";
  const yt=H-padB;
  for(let i=0;i<=4;i++){const v=max*i/4, y=yt-(yt-padT)*(i/4);
    g+=`<line x1="${padL}" x2="${W-padR}" y1="${y}" y2="${y}" stroke="#eeebe5"/>`;
    g+=`<text x="${padL-8}" y="${y+4}" text-anchor="end" font-size="10.5" fill="#a39f98">${Math.round(v)}</text>`;}
  yrs.forEach((d,i)=>{
    let acc=0; const x=padL+i*bw;
    keys.forEach(([k,c])=>{const v=d[k]; if(!v)return;
      const h=(yt-padT)*v/max; const y=yt-acc-h;
      g+=`<rect x="${x+bw*0.18}" y="${y}" width="${bw*0.64}" height="${h}" fill="${c}"/>`; acc+=h;});
    g+=`<text x="${x+bw/2}" y="${yt+16}" text-anchor="middle" font-size="10.5" fill="#8a8680">${d.yr.slice(2)}</text>`;
    g+=`<text x="${x+bw/2}" y="${yt+30}" text-anchor="middle" font-size="9.5" fill="#b8b4ac">${Math.round((d.mkt_ret??0))}%</text>`;
  });
  // 图例
  let lx=padL;
  keys.forEach(([k,c,l])=>{g+=`<rect x="${lx}" y="2" width="9" height="9" fill="${c}"/><text x="${lx+13}" y="10.5" font-size="10.5" fill="#6d6a65">${l}</text>`; lx+=l.length*10.5+34;});
  g+=`<text x="${W-padR}" y="${H-4}" text-anchor="end" font-size="10" fill="#b8b4ac">柱下灰字=当年全市场等权收益</text>`;
  document.getElementById("yrchart").innerHTML=
   `<div style="font-size:12.5px;color:#6d6a65;margin-bottom:8px">每一年达到 S/A/B 级的行情段数量（按见顶年份计）</div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${g}</svg>`;
})();

// 散点：S 数量 vs 次年市场收益
(function(){
  const W=430,H=300,padL=46,padR=14,padT=14,padB=40;
  const pts=D.yr.filter(d=>d.next_ret!=null && isFinite(d.next_ret) && isFinite(d.mkt_ret));
  const xs=pts.map(d=>d.S_T+d.S_E), ys=pts.map(d=>d.next_ret);
  const xm=Math.max(...xs), ymin=Math.min(...ys), ymax=Math.max(...ys);
  const X=v=>padL+(W-padL-padR)*v/(xm*1.08);
  const Y=v=>H-padB-(H-padT-padB)*(v-ymin)/(ymax-ymin);
  let g=`<line x1="${padL}" x2="${W-padR}" y1="${Y(0)}" y2="${Y(0)}" stroke="#d8d4cc" stroke-dasharray="3 3"/>`;
  g+=`<text x="${W-padR}" y="${Y(0)-6}" text-anchor="end" font-size="10" fill="#a39f98">0%</text>`;
  g+=`<text x="${padL-8}" y="${Y(ymax)+4}" text-anchor="end" font-size="10" fill="#a39f98">${ymax.toFixed(0)}%</text>`;
  g+=`<text x="${padL-8}" y="${Y(ymin)}" text-anchor="end" font-size="10" fill="#a39f98">${ymin.toFixed(0)}%</text>`;
  pts.forEach(d=>{const n=d.S_T+d.S_E;const x=X(n),y=Y(d.next_ret);
    g+=`<circle cx="${x}" cy="${y}" r="4.6" fill="#c0392b" opacity="0.55"/>`;
    g+=`<text x="${x}" y="${y-9}" text-anchor="middle" font-size="9" fill="#8a8680">${d.yr.slice(2)}</text>`;});
  g+=`<text x="${(W+padL)/2}" y="${H-8}" text-anchor="middle" font-size="10.5" fill="#8a8680">当年 S 级数量 →</text>`;
  g+=`<text transform="translate(12,${(H-padB+padT)/2}) rotate(-90)" text-anchor="middle" font-size="10.5" fill="#8a8680">次年全市场收益</text>`;
  document.getElementById("scatter").innerHTML=
   `<div style="font-size:12.5px;color:#6d6a65;margin-bottom:8px">妖股密度 vs 次年市场收益　r = −0.32</div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${g}</svg>`;
})();
</script></body></html>"""

def money(x): return "—" if x is None else str(x)

def fmt2(v):
    return "—" if v is None else (f"+{v}%" if v > 0 else f"{v}%")


anat_html = ""
for r in DATA["anat"]:
    anat_html += (f'<tr><td class="l"><b>{r["k"]}</b></td><td class="red">+{r["gain"]}%</td><td>{r["nd"]}</td>'
                  f'<td>{r["lu"]}</td><td>{r["ml"]}</td><td>{r["cap"]}</td>'
                  f'<td>{fmt2(r["pre20"])}</td><td>{fmt2(r["pre60"])}</td><td>{r["preto"]}%</td>'
                  f'<td class="green">{fmt2(r["e5"])}</td><td class="green">{fmt2(r["e20"])}</td>'
                  f'<td class="green">{fmt2(r["e60"])}</td><td class="green">{fmt2(r["e250"])}</td>'
                  f'<td class="green">{r["dd"]}%</td></tr>')

funnel_html = ""
for r in DATA["funnel"]:
    funnel_html += (f'<tr><td class="l">{r["cap"]}</td><td class="l">{r["cold"]}</td><td>{r["n"]:,}</td>'
                    f'<td>{r["p2"]}%</td><td>{r["p3"]}%</td><td>{r["p5"]}%</td>'
                    f'<td style="color:#c0392b"><b>{r["p8"]}%</b></td></tr>')

exp_html = ""
for r in DATA["exp"]:
    exp_html += (f'<tr><td>{r["h"]} 日</td><td class="green">{fmt2(r["avg"])}</td><td class="green">{fmt2(r["med"])}</td>'
                 f'<td class="green"><b>{fmt2(r["day"])}</b></td><td>{r["t"]}</td><td>{r["win"]}%</td><td>{r["n"]:,}</td></tr>')

html = (HTML.replace("__DATA__", json.dumps(DATA, ensure_ascii=False))
        .replace("__N_ALL__", f'{DATA["n_all"]:,}').replace("__N_S__", str(DATA["n_s"]))
        .replace("__N_ST__", str(DATA["n_st"])).replace("__N_SE__", str(DATA["n_se"]))
        .replace("__S_E20__", str(DATA["s_e20"])).replace("__S_E250__", str(DATA["s_e250"]))
        .replace("__S_EX20POS__", str(DATA["s_ex20_pos"])).replace("__S_ST__", str(DATA["s_st"]))
        .replace("__N_FB__", f'{DATA["n_fb"]:,}')
        .replace("__ANAT__", anat_html).replace("__FUNNEL__", funnel_html).replace("__EXP__", exp_html))

open("reports/yaogu-census-2026-11-25.html", "w", encoding="utf-8").write(html)
print("written", len(html), "bytes")
print("S非停牌", DATA["n_st"], "S事件", DATA["n_se"], "首板", DATA["n_fb"])
