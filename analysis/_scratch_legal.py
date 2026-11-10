"""RETRACTED EXPLORATION -- retained only as the disclosure record for round 4.

This is the throwaway probe that was run BEFORE analysis/_diag_legal.py had its
pre-registration written, so it is the reason round 4 cannot claim a blind
pre-registration. Two of its numbers are wrong and must not be cited:

  * turnover deciles were computed on the ENTRY day (to20[i0+1]) instead of the
    signal day, which is a mild look-ahead, and
  * the identity residual printed as "+2.0bp" was just the multiplicative
    interaction term, not a discrepancy.

Its correct contribution was directional only: close[d]->close[d+1] came out
positive (+26bp on the matched benchmark) when round 3 had priced the legal
alternative as -22 to -28bp net. The authoritative numbers are in
analysis/_diag_legal_result.json.

Not part of the pipeline. Not imported by anything.

"""
import sys, numpy as np
sys.path.insert(0, "analysis")
import asymmetry_control as ac, asymmetry_probe as ap

df = ap.add_features(ap.load_panel())
dates, symbols, mats = ap.calendar_mats(df)
T, S = mats["close"].shape
s_map = {s:i for i,s in enumerate(symbols)}; d_ix = {d:i for i,d in enumerate(dates)}
turn = ac.grid_of(df,"turnover_rate",dates,symbols)
tradable = np.isfinite(mats["close"]) & (mats["close"]>0) & np.isfinite(turn) & (turn>0)
to20 = ac.grid_of(df,"to20",dates,symbols)
o,c = mats["open"], mats["close"]

def grid(kind):
    out = np.full(o.shape, np.nan)
    with np.errstate(invalid="ignore"):
        if kind=="gap":        r = o[1:]/c[:-1]-1.0
        elif kind=="intraday": r = c[1:]/o[1:]-1.0
        elif kind=="c2c":      r = c[1:]/c[:-1]-1.0
    out[0:r.shape[0]]=r
    return np.where(np.isfinite(r), r, np.nan)

G = {k:grid(k) for k in ("gap","intraday","c2c")}
N_DEC=10
f=lambda a: f"{a.mean():+8.1f} (t {a.mean()/(a.std(ddof=0)/np.sqrt(a.size)):+5.2f}) n={a.size}"
for arm,asc in [("top20",False),("bottom20",True)]:
    pk = ap.pick_daily(df,"mom20",asc).select("date","symbols").to_dicts()
    res={k:{"pick":[], "uni":[], "match":[]} for k in ("gap","intraday","c2c")}
    for r in pk:
        i0=d_ix[str(r["date"])]
        if i0+1>=T: continue
        ok = tradable[i0] ; pool=np.flatnonzero(ok)
        if pool.size<ap.N_STOCK: continue
        sel=np.array([s_map[s] for s in r["symbols"]],dtype=np.int64); sel=sel[ok[sel]]
        if sel.size<5: continue
        vals=to20[i0][pool]; order=np.argsort(vals,kind="stable")
        rk=np.empty(pool.size,dtype=np.int64); rk[order]=np.arange(pool.size)
        dec=np.minimum(N_DEC-1,(rk*N_DEC)//pool.size)
        decf=np.full(S,-1,dtype=np.int64); decf[pool]=dec
        dsel=decf[sel]; cnt=np.bincount(dsel[dsel>=0],minlength=N_DEC).astype(float); w=cnt/cnt.sum()
        for k in ("gap","intraday","c2c"):
            vp=G[k][i0][pool]
            dm=np.array([float(np.nanmean(vp[dec==j])) if (dec==j).any() else np.nan for j in range(N_DEC)])
            res[k]["pick"].append(float(np.nanmean(G[k][i0][sel]))*1e4)
            res[k]["uni"].append(float(np.nanmean(vp))*1e4)
            res[k]["match"].append(float(np.nansum(w*dm))*1e4)
    print("="*78); print(arm)
    for k in ("gap","intraday","c2c"):
        p=np.array(res[k]["pick"]);u=np.array(res[k]["uni"]);m=np.array(res[k]["match"])
        print(f"  {k:9s} raw {f(p)}")
        print(f"  {'':9s} uni {f(u)}   match {f(m)}")
        print(f"  {'':9s} act-uni {f(p-u)}")
        print(f"  {'':9s} act-match {f(p-m)}")
    dg=np.mean(np.array(res['gap']['pick'])-np.array(res['gap']['match']))
    di=np.mean(np.array(res['intraday']['pick'])-np.array(res['intraday']['match']))
    dc=np.mean(np.array(res['c2c']['pick'])-np.array(res['c2c']['match']))
    print(f"  identity act-match: gap{di and ''}{dg:+.1f} + intra{di:+.1f} = {dg+di:+.1f} vs c2c {dc:+.1f}  diff={dg+di-dc:+.1f}")
