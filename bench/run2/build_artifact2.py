#!/usr/bin/env python
"""Render the best-of-2 three-way benchmark (all fixes applied) into a self-contained HTML report."""
import json, math, statistics, html

def load(*n): return [json.load(open(x)) for x in n]
SR = load("results_sqlite.json","results_sqliteb.json")
P1 = load("results_pg1.json","results_pg1b.json")
P4 = load("results_pg4.json","results_pg4b.json")
def index(runs): return [{r["id"]:r for r in run["rows"]} for run in runs]
sr,p1,p4 = index(SR),index(P1),index(P4)
order=[r["id"] for r in SR[0]["rows"]]

def best(idxs,rid):
    recs=[ix.get(rid) for ix in idxs if ix.get(rid)]
    withr=[r for r in recs if r.get("rps")]
    return max(withr,key=lambda r:r["rps"]) if withr else (recs[0] if recs else None)
def rps(r): return r.get("rps") if r else None
def annotated(rid): return bool(sr[0].get(rid,{}).get("annotate"))
def clean(rid):
    if annotated(rid): return False
    for idxs in (sr,p1,p4):
        for ix in idxs:
            r=ix.get(rid)
            if not r or not r.get("rps") or r.get("n",0)!=r.get("ok",-1): return False
    return True

def frps(r):
    v=rps(r)
    if v is None: return "—"
    return f"{v:,.0f}" if v>=100 else (f"{v:.1f}" if v>=10 else f"{v:.2f}")
def td_rps(r):
    v=rps(r)
    if v is None: return "<td class='num'>—</td>"
    return f"<td class='num'>{frps(r)}</td>"
def td_lat(r):
    if not r or not r.get("lat_ms"): return "<td class='num lat'>—</td>"
    l=r["lat_ms"]; f=lambda v: f"{v:,.0f}" if v>=100 else f"{v:.1f}"
    return f"<td class='num lat'>{f(l['p50'])}<span class='sep'>/</span>{f(l['p95'])}</td>"
def td_ratio(a,b):
    ra,rb=rps(a),rps(b)
    if not ra or not rb: return "<td class='num'>—</td>"
    x=rb/ra; cls="up" if x>=1.05 else ("down" if x<=0.95 else "flat")
    return f"<td class='num {cls}'>{x:.2f}×</td>"

PHASES=[("meta","元数据 / 协议层","不触及集合数据，衡量 HTTP + 分发层"),
        ("setup","建库写入 + 可加性检查","建模型/牌组/加笔记 + canAdd 查重"),
        ("read","数据读取","查询、批量信息、模型元数据、标签、媒体（deck: 查询针对基准自建牌组，三组数据等同）"),
        ("write","数据写入","笔记/卡片/标签/调度/模型的全部修改类接口"),
        ("stats","统计","复习记录与统计接口（含现已修复的 getCollectionStatsHTML）"),
        ("global","全库维护写","扫描整个集合的维护型写"),
        ("export","导入导出","apkg 导出/导入（样本量小，仅参考）"),
        ("sweep","并发扫描 C=1 / C=8","低并发补充点，配合正文 C=16/32 观察扩展性"),
        ("cleanup","删除清理","基准数据成组删除")]

def section(ph,title,sub):
    ids=[rid for rid in order if sr[0].get(rid,{}).get("phase")==ph and sr[0].get(rid,{}).get("kind")!="mixed"]
    if not ids: return ""
    body=[]
    for rid in ids:
        a,b,c=best(sr,rid),best(p1,rid),best(p4,rid)
        mark="<span class='annot'>‡</span>" if annotated(rid) else ""
        body.append("<tr><th scope='row'>"+html.escape(rid)+mark+f"</th><td class='num c'>{a['C']}</td>"
            +td_rps(a)+td_rps(b)+td_rps(c)+td_ratio(a,b)+td_ratio(a,c)+td_ratio(b,c)
            +td_lat(a)+td_lat(b)+td_lat(c)+"</tr>")
    return f"""<section><h2>{title}</h2><p class="sub">{sub}</p>
<div class="tw"><table><thead><tr><th>接口</th><th class="num">C</th>
<th class="num">SQLite<br><span class="u">rps</span></th><th class="num">PG×1<br><span class="u">rps</span></th><th class="num">PG×4<br><span class="u">rps</span></th>
<th class="num">PG×1<br><span class="u">/SQLite</span></th><th class="num">PG×4<br><span class="u">/SQLite</span></th><th class="num">PG×4<br><span class="u">/PG×1</span></th>
<th class="num">SQLite<br><span class="u">p50/p95</span></th><th class="num">PG×1<br><span class="u">p50/p95</span></th><th class="num">PG×4<br><span class="u">p50/p95</span></th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div></section>"""

# geomeans
def geo(v):
    v=[x for x in v if x]; return math.exp(statistics.fmean(math.log(x) for x in v)) if v else None
def ratio(a,b):
    ra,rb=rps(a),rps(b); return rb/ra if ra and rb else None
groups={"读接口":lambda r:r["kind"]=="read","写接口":lambda r:r["kind"]=="write","元数据":lambda r:r["kind"]=="meta" and r["id"]!="httpProbe"}
agg={}
for g,pred in groups.items():
    ids=[rid for rid in order if pred(sr[0].get(rid,{})) and sr[0].get(rid,{}).get("phase")!="sweep" and clean(rid)]
    agg[g]=dict(n=len(ids),
        r1=geo([ratio(best(sr,i),best(p1,i)) for i in ids]),
        r4=geo([ratio(best(sr,i),best(p4,i)) for i in ids]),
        r41=geo([ratio(best(p1,i),best(p4,i)) for i in ids]))
def rc(x): return f"<td class='num {'up' if x>=1.05 else ('down' if x<=0.95 else 'flat')}'>{x:.2f}×</td>"
agg_rows="".join(f"<tr><th scope='row'>{g}</th><td class='num'>{v['n']}</td>{rc(v['r1'])}{rc(v['r4'])}{rc(v['r41'])}</tr>" for g,v in agg.items())

# mixed best-of-2
def mixed_best(runs):
    ms=[next(r for r in run["rows"] if r["kind"]=="mixed")["mixed"] for run in runs]
    return max(ms,key=lambda m:m["read"]["rps"]+m["write"]["rps"])
mm={"SQLite":mixed_best(SR),"PG×1":mixed_best(P1),"PG×4":mixed_best(P4)}
def mrow(k,zh):
    v=[mm[n][k]["rps"] for n in ("SQLite","PG×1","PG×4")]
    l=[f"{mm[n][k]['p50_ms']:.0f}<span class='sep'>/</span>{mm[n][k]['p95_ms']:.0f}" for n in ("SQLite","PG×1","PG×4")]
    cells="".join(f"<td class='num'>{x:,.0f}</td>" for x in v)
    lats="".join(f"<td class='num lat'>{x}</td>" for x in l)
    return f"<tr><th scope='row'>{zh}</th>{cells}<td class='num up'>{v[2]/v[0]:.2f}×</td><td class='num up'>{v[2]/v[1]:.2f}×</td>{lats}</tr>"
tot=[mm[n]["read"]["rps"]+mm[n]["write"]["rps"] for n in ("SQLite","PG×1","PG×4")]
mixed_tbl=f"""<div class="tw"><table><thead><tr><th>指标</th>
<th class="num">SQLite</th><th class="num">PG×1</th><th class="num">PG×4</th><th class="num">PG×4/SQLite</th><th class="num">PG×4/PG×1</th>
<th class="num">SQLite<br><span class="u">p50/p95</span></th><th class="num">PG×1<br><span class="u">p50/p95</span></th><th class="num">PG×4<br><span class="u">p50/p95</span></th></tr></thead><tbody>
{mrow("read","读吞吐 rps (notesInfo/findCards/cardsInfo)")}
{mrow("write","写吞吐 rps (updateNoteFields)")}
<tr class="total"><th scope='row'>总吞吐 rps</th>{''.join(f"<td class='num'>{t:,.0f}</td>" for t in tot)}<td class='num up'>{tot[2]/tot[0]:.2f}×</td><td class='num up'>{tot[2]/tot[1]:.2f}×</td><td colspan="3"></td></tr>
</tbody></table></div>"""

def cpu(r): return round(r["meta"]["cpu1"]["cpu_s"]-r["meta"]["cpu0"]["cpu_s"],1)
meta_tbl=f"""<div class="tw"><table><thead><tr><th></th><th class="num">SQLite</th><th class="num">PG×1</th><th class="num">PG×4</th></tr></thead><tbody>
<tr><th scope='row'>单轮基准时长 (s)</th><td class='num'>{statistics.mean(r['meta']['wall_total_s'] for r in SR):.0f}</td><td class='num'>{statistics.mean(r['meta']['wall_total_s'] for r in P1):.0f}</td><td class='num'>{statistics.mean(r['meta']['wall_total_s'] for r in P4):.0f}</td></tr>
<tr><th scope='row'>服务端 CPU (s)¹</th><td class='num'>{statistics.mean(cpu(r) for r in SR):.0f}</td><td class='num'>{statistics.mean(cpu(r) for r in P1):.0f}</td><td class='num'>{statistics.mean(cpu(r) for r in P4):.0f}</td></tr>
<tr><th scope='row'>服务进程数</th><td class='num'>1</td><td class='num'>1</td><td class='num'>5</td></tr>
<tr><th scope='row'>常驻内存 (MB)</th><td class='num'>{statistics.mean(r['meta']['cpu1']['rss_kb'] for r in SR)//1024:.0f}</td><td class='num'>{statistics.mean(r['meta']['cpu1']['rss_kb'] for r in P1)//1024:.0f}</td><td class='num'>{statistics.mean(r['meta']['cpu1']['rss_kb'] for r in P4)//1024:.0f}</td></tr>
<tr><th scope='row'>底库笔记数</th><td class='num'>8,207</td><td class='num'>21,932</td><td class='num'>21,932</td></tr>
<tr><th scope='row'>6 次运行数据还原</th><td class='num'>✓</td><td class='num'>✓</td><td class='num'>✓</td></tr>
</tbody></table></div>"""

r=lambda g:agg[g]
page=f"""<title>ankiweb 三后端基准 — 全修复 + 优化后复测</title>
<style>
:root {{ --bg:#F6F7F8; --card:#FFF; --ink:#1B2730; --mut:#5B6B77; --hair:#DEE4E9; --accent:#2F6B8F; --accent-ink:#245676;
  --good:#1F7A50; --bad:#AC5228; --flat:#7A8791; --chip:#EAF0F4; --mono:"SF Mono","Cascadia Mono","JetBrains Mono",Consolas,monospace; }}
@media (prefers-color-scheme:dark){{:root{{--bg:#121A20;--card:#1A232B;--ink:#E2E9EF;--mut:#92A3B0;--hair:#2A3742;--accent:#6FA8C9;--accent-ink:#8FBCD6;--good:#4FBF8B;--bad:#D98B5F;--flat:#7E8E9A;--chip:#212F3A;}}}}
:root[data-theme="dark"]{{--bg:#121A20;--card:#1A232B;--ink:#E2E9EF;--mut:#92A3B0;--hair:#2A3742;--accent:#6FA8C9;--accent-ink:#8FBCD6;--good:#4FBF8B;--bad:#D98B5F;--flat:#7E8E9A;--chip:#212F3A;}}
:root[data-theme="light"]{{--bg:#F6F7F8;--card:#FFF;--ink:#1B2730;--mut:#5B6B77;--hair:#DEE4E9;--accent:#2F6B8F;--accent-ink:#245676;--good:#1F7A50;--bad:#AC5228;--flat:#7A8791;--chip:#EAF0F4;}}
*{{box-sizing:border-box;}}
body{{background:var(--bg);color:var(--ink);margin:0;padding:0 20px 96px;font:15px/1.65 -apple-system,"Segoe UI","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;}}
main,header{{max-width:1240px;margin-left:auto;margin-right:auto;}}
header{{margin-top:44px;}}
.eyebrow{{font-family:var(--mono);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent-ink);}}
h1{{font-size:28px;line-height:1.3;margin:8px 0 10px;text-wrap:balance;}}
.lede{{max-width:74ch;color:var(--mut);margin:0;}} .lede b{{color:var(--ink);}}
h2{{font-size:19px;margin:0 0 2px;}} section{{margin-top:42px;}}
.sub{{color:var(--mut);font-size:13.5px;margin:2px 0 12px;max-width:82ch;}}
.tw{{overflow-x:auto;background:var(--card);border:1px solid var(--hair);border-radius:6px;}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;}}
thead th{{position:sticky;top:0;background:var(--card);color:var(--mut);font-weight:600;font-size:12px;text-align:right;padding:9px 10px;border-bottom:1px solid var(--hair);white-space:nowrap;line-height:1.35;}}
thead th:first-child{{text-align:left;}}
.u{{font-family:var(--mono);font-size:10.5px;font-weight:400;letter-spacing:.02em;}}
tbody th{{text-align:left;font-weight:500;padding:7px 10px;border-bottom:1px solid var(--hair);font-family:var(--mono);font-size:12.5px;white-space:nowrap;}}
td{{padding:7px 10px;border-bottom:1px solid var(--hair);white-space:nowrap;}}
tr:last-child td,tr:last-child th{{border-bottom:none;}}
.num{{text-align:right;font-family:var(--mono);font-size:12.8px;font-variant-numeric:tabular-nums;}}
.c{{color:var(--mut);}} .lat{{color:var(--mut);font-size:11.6px;}} .sep{{opacity:.45;padding:0 1px;}}
.up{{color:var(--good);font-weight:600;}} .down{{color:var(--bad);}} .flat{{color:var(--flat);}}
.annot{{color:var(--accent-ink);font-weight:700;}}
.total th,.total td{{border-top:1.5px solid var(--hair);font-weight:600;}}
.callout{{background:var(--card);border:1px solid var(--hair);border-radius:6px;padding:16px 22px;margin-top:14px;}}
.callout ul{{margin:0;padding-left:1.2em;}} .callout li{{margin:7px 0;max-width:90ch;}} .callout li::marker{{color:var(--accent);}}
code{{font-family:var(--mono);font-size:12px;background:var(--chip);border-radius:4px;padding:1px 5px;}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin-top:16px;}}
.kpi{{background:var(--card);border:1px solid var(--hair);border-radius:8px;padding:14px 18px;min-width:150px;flex:1;}}
.kpi .v{{font-family:var(--mono);font-size:26px;font-weight:700;color:var(--accent-ink);font-variant-numeric:tabular-nums;}}
.kpi .l{{font-size:12.5px;color:var(--mut);margin-top:3px;}}
.legend{{color:var(--mut);font-size:13px;max-width:90ch;}} .legend p{{margin:6px 0;}}
.ok{{color:var(--good);font-weight:600;}}
a{{color:var(--accent-ink);}}
@media (prefers-reduced-motion:no-preference){{html{{scroll-behavior:smooth;}}}}
</style>
<header>
<div class="eyebrow">ankiweb · AnkiConnect :18765 · 2026-07-07 · best-of-2</div>
<h1>SQLite vs PostgreSQL 单进程 vs PostgreSQL 4-Worker — 全修复后的多牌组高并发复测</h1>
<p class="lede">同机 16 核、机器空闲（load ≈1.5），按三条启动命令依次压测，每版本跑两轮取较优。基准自建 16 牌组 / ~2.6k 笔记 / ~5.1k 卡片，覆盖 95 个接口（每版本 ~3.3 万次调用 + 30 秒混合负载），测完全部还原。
本轮所有服务端修复与 review_agent 热路径优化均已就位——<b>6 次运行零错误</b>，首轮暴露的 4 个 bug（prefork +40ms、notetype 跨 worker 失配、createDeck 竞态、getCollectionStatsHTML 在 PG 100% 失败）<b>全部验证修复</b>。</p>
<div class="kpis">
<div class="kpi"><div class="v">{tot[2]/tot[1]:.2f}×</div><div class="l">多进程优势 · 混合负载总吞吐<br>(PG×4 vs PG×1)</div></div>
<div class="kpi"><div class="v">{r('读接口')['r41']:.2f}× / {r('写接口')['r41']:.2f}×</div><div class="l">多进程优势 · 读 / 写<br>(PG×4 vs PG×1 几何平均)</div></div>
<div class="kpi"><div class="v">{tot[2]/tot[0]:.2f}×</div><div class="l">PG×4 vs 原版 SQLite<br>混合负载总吞吐</div></div>
<div class="kpi"><div class="v ok">0</div><div class="l">6 次运行错误总数<br>(首轮 PG×4 曾 96+ 失败)</div></div>
</div>
</header>
<main>
<section><h2>核心结论</h2><div class="callout"><ul>
<li><b>多进程优势（本次测量的核心）：混合负载下 PG×4 是 PG×1 的 {tot[2]/tot[1]:.2f}×</b>（{tot[2]:.0f} vs {tot[1]:.0f} rps）；分接口几何平均读 <b>{r('读接口')['r41']:.2f}×</b>、写 <b>{r('写接口')['r41']:.2f}×</b>。因两者代码完全相同，这是最干净的"开 4 worker 值不值"answer——受 PG 行锁/共享对象序列化影响未到理论 4×，但 2.2–3.2× 的提升覆盖几乎所有数据面接口。</li>
<li><b>对比原版 SQLite：PG×4 读几何平均 {r('读接口')['r4']:.2f}×、写 {r('写接口')['r4']:.2f}×，混合负载 {tot[2]/tot[0]:.2f}×，读写 p50 延迟约为一半</b>（混合读 39ms vs 68ms、写 32ms vs 68ms）。读接口几乎全面反超（含批量 notesInfo[100] <b>5.34×</b>）；写接口里"批量/多卡"操作反超（updateNoteFields 1.28×、suspend 1.67×、relearnCards 1.98×），只有"改单个共享对象"的小写入（模型 schema、deck 配置）仍慢于 SQLite。</li>
<li><b>PG 单进程仍不值得为性能用</b>（读 {r('读接口')['r1']:.2f}×、写 {r('写接口')['r1']:.2f}×、混合 {tot[1]/tot[0]:.2f}× vs SQLite）——每操作跨 TCP 到 PostgreSQL，串行模式无法摊薄网络往返。PG 的价值只在 <code>ANKIWEB_WORKERS=N</code> 下兑现。</li>
<li><b>review_agent 优化生效</b>：canAddNotes(25) 现为 SQLite 的 1.73×（PG×4）/ 批量查重往返数 -87%；模型元数据读（modelFieldNames 等）经 rust 缓存路由后在 PG×4 全部反超 SQLite（1.1–1.4×），首轮它们还是 0.5×。</li>
<li><b>getCollectionStatsHTML 现在能用了</b>：PG×4 29.3 rps（首轮 100% 失败）。元数据/协议层三版本齐平（~1.0×），证明分发层无回归。</li>
</ul></div></section>

<section><h2>混合负载 — 最接近真实使用的场景</h2>
<p class="sub">30 秒持续压测，32 并发连接 = 24 读 slot（notesInfo/findCards/cardsInfo 轮转）+ 8 写 slot（updateNoteFields），多牌组数据；取两轮较优。</p>
{mixed_tbl}</section>

<section><h2>几何平均加速比</h2>
<p class="sub">仅统计 6 次运行全部成功、无 ‡ 注解（跨后端不可比）的行；不含并发扫描补充点。吞吐按同并发下 rps 之比。</p>
<div class="tw"><table><thead><tr><th>类别</th><th class="num">行数</th><th class="num">PG×1/SQLite</th><th class="num">PG×4/SQLite</th><th class="num">PG×4/PG×1</th></tr></thead><tbody>{agg_rows}</tbody></table></div></section>

{''.join(section(ph,t,s) for ph,t,s in PHASES)}

<section><h2>怎么读这张表</h2><div class="callout"><ul>
<li><b>PG×4/PG×1 列 = 多进程净收益</b>（同代码，唯一变量是 worker 数）。稳定落在 2–3×，是"要不要开多 worker"最干净的依据。</li>
<li><b>PG×4/SQLite 列 = 换后端的真实体验</b>（SQLite 跑原版 <code>ankiweb</code>，PG 跑带优化的 <code>ankiweb_pg</code>——用户实际部署的两套代码）。绿=PG×4 更快，红=SQLite 更快。</li>
<li><b>PG 天生强</b>：批量读（notesInfo[100] 5.34×、50-卡批量读 3.5–4.3×）、媒体读写删（1.8–4×）、按天统计聚合（4.6×）。<b>天生弱</b>：单条小写入串成的流程（deleteNotes 0.16×、addTags 0.64×）与"改共享对象"（模型 schema 0.28–0.46×、deck 配置 0.74–0.94×）——每次都要一次 PG 事务往返。</li>
<li><b>C=1 补充点</b>：单并发时 PG×4≈PG×1（多进程不帮单客户端），并发一上来（C=8/16/32）优势立刻兑现——正是 SQLite 内部全串行、加并发吞吐不再增长的地方。</li>
</ul></div></section>

<section><h2>与首轮基准的关键差异（本轮全部修复已就位）</h2><div class="callout"><ul>
<li><span class="ok">✓</span> <b>prefork +40ms 已修</b>：keep-alive 往返 41ms → 0.3ms（实测）。首轮未修的 PG×4 廉价操作被钉在 ~730 rps，本轮 version 达 3183、deckNames 达 2193。</li>
<li><span class="ok">✓</span> <b>notetype 跨 worker 一致性已修</b>：首轮 PG×4 modelFieldRemove 72/96 失败、脏读；本轮 0 失败。代价：单进程 PG 的模型元数据读因新增一次 epoch 轮询略降（modelFieldNames PG×1 1474→1012），但多 worker 完全盖过——且单进程 PG 本就不推荐。</li>
<li><span class="ok">✓</span> <b>createDeck 父牌组竞态已修</b>：首轮 1–3/80 失败；本轮 96 并发建牌组 0 失败。</li>
<li><span class="ok">✓</span> <b>getCollectionStatsHTML 已修</b>：剥掉 5 层 SQLite 方言问题（lastIvl 大小写、WHERE 引用列别名、无别名派生表、GROUP BY 裸列、NUMERIC 参数绑定），SQLite/PG 两后端双模式均通过。</li>
</ul></div></section>

<section><h2>环境与运行元信息</h2>{meta_tbl}
<p class="legend">¹ 仅 ankiweb 进程 CPU；PG 模式另有 docker 内 PostgreSQL 的 CPU 未计入。三版本共用同一客户端：4 进程 × asyncio，每行固定并发（C 列），先预热再计时，每行新建 TCP 连接以重新随机化 SO_REUSEPORT 分配。每版本跑两轮，逐行取较优 rps 及其对应延迟。</p></section>

<section><h2>标注与方法学</h2><div class="legend">
<p><b>‡</b> 全库作用域接口，底库不同（SQLite 8,207 笔记 / 50,919 revlog；PG realcol 21,932 笔记 / 82 revlog），跨后端仅供参考、已排除出几何平均。其中 findNotes[全库] 对 PG 更不利（扫 2.7× 数据），getNumCardsReviewedByDay/statsHTML 对 PG 更有利（revlog 少 620×）。其余所有行都在基准自建的等同数据上，可直接比较。</p>
<p><b>稳定性</b>：同配置两轮 rps 比值几何平均 1.09×、中位 1.05×；混合负载三版本两轮几乎重合（SQLite 434/426、PG×1 236/237、PG×4 763/762）。纯 HTTP 的 httpProbe 因受客户端 CPU 抖动影响波动略大，故未纳入几何平均。</p>
<p><b>代码</b>：SQLite 跑 <code>/mnt/sda/git/web/ankiweb</code>（原版 handler），PG×1/PG×4 跑 <code>/mnt/sda/git/web/ankiweb_pg</code>（含全部修复 + review_agent 优化）——即三条命令实际启动的代码。多进程优势（PG×4/PG×1）两侧同码，最干净。跳过 24 个 gui*、sync、loadProfile/reloadCollection 等有状态/副作用接口；覆盖 95/127 动作 + multi + HTTP 探针。</p>
<p><b>数据完整性</b>：6 次运行后两库笔记/牌组/模型/标签/媒体计数均回到基线；压测产生的 revlog（每轮 ~1.1 万条）按运行时间戳范围删除，SQLite 恢复 50,919、PG 恢复 82。</p>
</div></section>
</main>"""
open("report2.html","w").write(page)
print("report2.html:",len(page),"bytes | mixed tot",tot,"| read geo pg4/sqlite",round(agg['读接口']['r4'],2),"pg4/pg1",round(agg['读接口']['r41'],2))
