#!/usr/bin/env python3
"""NSE day-trading report bot (runs on GitHub Actions, or locally).

  python3 bot.py morning   # 8:00 IST: full market analysis + trade setups -> email (PDF + Excel)
  python3 bot.py run       # every 15 min in market hours: top 10 from today's setups -> email (PDF)

Flags: --force (ignore the time window), --no-send (build files only), --now "YYYY-MM-DD HH:MM" (IST, testing)

Email settings come from environment variables (GitHub secrets) or a local email.env file:
  EMAIL_FROM, EMAIL_APP_PASSWORD, EMAIL_TO   (Gmail App Password; EMAIL_TO may hold several, comma-separated)

Educational technical scan on free, delayed Yahoo data. Not investment advice.
"""
import datetime, json, math, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HOME = os.path.dirname(os.path.abspath(__file__))
os.chdir(HOME)
sys.path.insert(0, HOME)
import scan  # noqa: E402  NSE calendar + IST helpers

PY = sys.executable
OUT = os.path.join(HOME, "reports")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}
LABEL = {"breakout_20d": "20D Breakout", "bull_flag": "Bull Flag", "reclaim_ma20": "Reclaim 20DMA",
         "oversold": "Oversold", "oversold_bounce": "Oversold Bounce", "orb": "ORB"}
INDICES = [("NIFTY 50", "^NSEI"), ("NIFTY BANK", "^NSEBANK"), ("SENSEX", "^BSESN"), ("INDIA VIX", "^INDIAVIX"),
           ("NIFTY IT", "^CNXIT"), ("NIFTY AUTO", "^CNXAUTO"), ("NIFTY PHARMA", "^CNXPHARMA"), ("NIFTY METAL", "^CNXMETAL"),
           ("NIFTY FMCG", "^CNXFMCG"), ("NIFTY PSU BANK", "^CNXPSUBANK"), ("NIFTY REALTY", "^CNXREALTY"),
           ("NIFTY ENERGY", "^CNXENERGY")]
NEWS_FEEDS = ["https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
              "https://news.google.com/rss/search?q=Sensex%20OR%20Nifty%20OR%20%22stock%20market%22%20India%20when:1d&hl=en-IN&gl=IN&ceid=IN:en"]


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a), flush=True)


def now_ist(arg=None):
    if arg:
        return datetime.datetime.strptime(arg, "%Y-%m-%d %H:%M")
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def tick(p):
    return 0.01 if p < 250 else 0.05 if p < 1000 else 0.10 if p < 5000 else 0.50 if p < 10000 else 1.0 if p < 20000 else 5.0


def rdown(x, t): return round(math.floor(x / t + 1e-9) * t, 2)
def rup(x, t): return round(math.ceil(x / t - 1e-9) * t, 2)


def latin(s):
    s = str(s).replace("₹", "Rs ").replace("‘", "'").replace("’", "'").replace("“", '"') \
        .replace("”", '"').replace("–", "-").replace("—", "-").replace("…", "...")
    return s.encode("latin-1", "replace").decode("latin-1")


def chart(ysym, rng="5d", interval="5m"):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym), rng, interval)
    for i in range(3):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))
            return r["chart"]["result"][0]
        except Exception:
            time.sleep(1 + i)
    return None


def daily_rows(ysym, before=None):
    res = chart(ysym, "6mo", "1d")
    if not res:
        return None
    q = res["indicators"]["quote"][0]
    rows = [(scan.ts_to_ist(t).date(), o, h, l, c, v or 0) for t, o, h, l, c, v in
            zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]) if None not in (o, h, l, c)]
    if before:
        rows = [r for r in rows if r[0] < before]
    return rows


def stats(rows):
    c = [r[4] for r in rows]; h = [r[2] for r in rows]; l = [r[3] for r in rows]; v = [r[5] for r in rows]
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(rows))]
    g = [max(c[i] - c[i - 1], 0) for i in range(len(c) - 14, len(c))]
    ls = [max(c[i - 1] - c[i], 0) for i in range(len(c) - 14, len(c))]
    P = (h[-1] + l[-1] + c[-1]) / 3
    return dict(date=rows[-1][0].isoformat(), close=c[-1], high=h[-1], low=l[-1], atr=sum(trs[-14:]) / 14,
                ma20=sum(c[-20:]) / 20, ma50=sum(c[-50:]) / 50, rsi=100 - 100 / (1 + sum(g) / max(sum(ls), 1e-9)),
                pivot=P, r1=2 * P - l[-1], s1=2 * P - h[-1], r2=P + h[-1] - l[-1], s2=P - (h[-1] - l[-1]),
                chg1=(c[-1] / c[-2] - 1) * 100, chg5=(c[-1] / c[-6] - 1) * 100,
                vr=v[-1] / max(sum(v[-21:-1]) / 20, 1))


def last_price(ysym, upto_ts=None):
    res = chart(ysym, "5d", "5m")
    if not res:
        return None, None
    pts = [(t, c) for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"])
           if c is not None and (upto_ts is None or t < upto_ts)]
    if not pts:
        return None, None
    day = scan.ts_to_ist(pts[-1][0]).date()
    before = [c for t, c in pts if scan.ts_to_ist(t).date() < day]
    return pts[-1][1], (before[-1] if before else None)


def fetch_news(limit=12):
    items, seen = [], set()
    for url in NEWS_FEEDS:
        try:
            root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read())
            for it in root.iter("item"):
                t = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
                key = t.lower()[:60]
                if not t or key in seen:
                    continue
                seen.add(key)
                src = it.findtext("source") or ("Economic Times" if "economictimes" in url else "")
                items.append(dict(title=t, link=it.findtext("link") or "", src=src, date=(it.findtext("pubDate") or "")[:22]))
        except Exception as e:
            log("news feed failed", url[:50], e)
    return items[:limit]


# ======================================================================= MORNING (8:00 IST)
def cmd_morning(args):
    now = now_ist(args.get("now")); d = now.date()
    ok, why = scan.trading_day(d)
    if not ok and not args.get("force"):
        log("morning: market closed today (%s)" % why); return
    r = subprocess.run([PY, "scan.py", "daily", "--out", "trader/daily.json", "--quiet"], capture_output=True, text=True)
    log("daily scan exit", r.returncode, (r.stdout + r.stderr).strip()[-300:])
    daily = json.load(open("trader/daily.json"))
    hits = daily["hits"]

    ctx = {}
    for name, ys in INDICES:
        rows = daily_rows(ys, d)
        if rows and len(rows) > 55:
            ctx[name] = stats(rows)
        time.sleep(0.2)

    by_turn = sorted(hits, key=lambda h: -h["price"] * h["volume"])
    setups = []
    for h in by_turn[:60]:
        rows = daily_rows(h["symbol"] + ".NS", d); time.sleep(0.2)
        if not rows or len(rows) < 55:
            continue
        s = stats(rows); t = tick(s["close"])
        entry = rup(s["high"] * 1.001, t)
        stop = rdown(entry - 0.5 * s["atr"], t); risk = entry - stop
        if risk <= 0:
            continue
        score = (2 if s["close"] > s["ma20"] > s["ma50"] else 1 if s["close"] > s["ma20"] else 0) \
            + (1 if s["vr"] >= 1.5 else 0) + (1 if 55 <= s["rsi"] <= 75 else 0) + (1 if len(h["setups"]) > 1 else 0) \
            - (1 if s["rsi"] > 80 else 0) - (1 if set(h["setups"]) <= {"oversold"} else 0)
        note = []
        if s["rsi"] > 80: note.append("RSI>80 overbought - avoid chasing")
        if s["vr"] >= 1.5: note.append("Volume %.1fx avg" % s["vr"])
        if s["close"] < s["ma50"]: note.append("Below 50DMA")
        setups.append(dict(sym=h["symbol"], setup=", ".join(LABEL[x] for x in h["setups"]), score=score, s=s,
                           entry=entry, stop=stop, t1=rup(entry + risk, t), t2=rup(entry + 2 * risk, t), risk=risk,
                           note="; ".join(note)))
    setups.sort(key=lambda x: (-x["score"], -x["s"]["close"] * x["s"]["vr"]))
    json.dump({"date": d.isoformat(), "setups": [{k: v for k, v in x.items() if k != "s"} | {"close": x["s"]["close"]}
                                                  for x in setups]}, open("trader/plan.json", "w"), indent=1)
    news = fetch_news()
    view = market_view(ctx, daily)
    pdf = morning_pdf(now, ctx, view, news, setups, daily)
    xlsx = morning_xlsx(now, ctx, view, news, setups, by_turn)
    log("morning report", pdf, xlsx)
    if not args.get("no_send"):
        send_email("NSE Pre-Market Analysis & Trade Setups - %s" % now.strftime("%a %d %b %Y"),
                   morning_html(now, ctx, view, news, setups), [pdf, xlsx])


def market_view(ctx, daily):
    v = []
    nf, bn, vix = ctx.get("NIFTY 50"), ctx.get("NIFTY BANK"), ctx.get("INDIA VIX")
    if nf:
        trend = "uptrend" if nf["close"] > nf["ma20"] > nf["ma50"] else "downtrend" if nf["close"] < nf["ma20"] < nf["ma50"] else "sideways"
        v.append("NIFTY 50 closed %s (%+.2f%% day, %+.2f%% week), RSI %.0f, %s (20DMA %s, 50DMA %s)." % (
            format(nf["close"], ",.2f"), nf["chg1"], nf["chg5"], nf["rsi"], trend, format(nf["ma20"], ",.0f"), format(nf["ma50"], ",.0f")))
        v.append("NIFTY levels today: Pivot %s | R1 %s / R2 %s | S1 %s / S2 %s. Above pivot = bullish bias; below S1 = avoid new longs." % tuple(
            format(nf[k], ",.0f") for k in ("pivot", "r1", "r2", "s1", "s2")))
    if bn:
        v.append("BANK NIFTY %s (%+.2f%% day). Pivot %s, R1 %s, S1 %s." % (format(bn["close"], ",.2f"), bn["chg1"],
                 format(bn["pivot"], ",.0f"), format(bn["r1"], ",.0f"), format(bn["s1"], ",.0f")))
    if vix:
        v.append("INDIA VIX %.2f (%+.2f%%): %s." % (vix["close"], vix["chg1"], "low - trends tend to hold" if vix["close"] < 14
                 else "moderate" if vix["close"] < 18 else "elevated - use smaller size, wider stops"))
    secs = sorted([(n, s) for n, s in ctx.items() if n not in ("NIFTY 50", "SENSEX", "INDIA VIX", "NIFTY BANK")], key=lambda x: -x[1]["chg1"])
    if secs:
        v.append("Sectors (last session): strongest " + ", ".join("%s %+.1f%%" % (n.replace("NIFTY ", ""), s["chg1"]) for n, s in secs[:3])
                 + "; weakest " + ", ".join("%s %+.1f%%" % (n.replace("NIFTY ", ""), s["chg1"]) for n, s in secs[-3:]) + ".")
    cnt = {}
    for h in daily["hits"]:
        for s in h["setups"]:
            cnt[s] = cnt.get(s, 0) + 1
    v.append("Scanner: %s liquid NSE stocks scanned, %d matched a setup (%s)." % (daily.get("scanned"), len(daily["hits"]),
             ", ".join("%d %s" % (n, LABEL[k]) for k, n in sorted(cnt.items(), key=lambda x: -x[1]))))
    v.append("Plan: wait for the 9:15-9:30 opening range, enter only on a 5-min close above the trigger, stop in at once, no entries after 15:00, square off by 15:15.")
    return v


def morning_pdf(now, ctx, view, news, setups, daily):
    from fpdf import FPDF
    pdf = FPDF(orientation="L", unit="mm", format="A4"); pdf.set_auto_page_break(True, 10); pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "NSE Pre-Market Analysis - %s" % now.strftime("%A %d %b %Y"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "Generated %s IST from previous-session closes (Yahoo Finance, free, delayed)." % now.strftime("%H:%M"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    cols = [("Index", 34), ("Close", 26), ("Day %", 18), ("Week %", 18), ("RSI", 14), ("Pivot", 24), ("R1", 24), ("R2", 24), ("S1", 24), ("S2", 24)]
    hdr(pdf, cols)
    for n, s in ctx.items():
        vals = [n, format(s["close"], ",.2f"), "%+.2f" % s["chg1"], "%+.2f" % s["chg5"], "%.0f" % s["rsi"]] + [format(s[k], ",.0f") for k in ("pivot", "r1", "r2", "s1", "s2")]
        for j, ((_, w), v) in enumerate(zip(cols, vals)):
            if j in (2, 3):
                pdf.set_fill_color(*((226, 239, 218) if not v.startswith("-") else (252, 228, 214)))
            pdf.cell(w, 6, v, border=1, align="L" if j == 0 else "R", fill=j in (2, 3))
        pdf.ln()
    pdf.ln(2); pdf.set_font("Helvetica", "B", 11); pdf.cell(0, 6, "Market view", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for v in view:
        pdf.multi_cell(0, 5, latin("- " + v), new_x="LMARGIN", new_y="NEXT")
    if news:
        pdf.ln(1); pdf.set_font("Helvetica", "B", 11); pdf.cell(0, 6, "Latest market news", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8.5)
        for n in news:
            pdf.multi_cell(0, 4.6, latin("- %s  (%s)" % (n["title"], n["src"] or n["date"])), new_x="LMARGIN", new_y="NEXT")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13); pdf.cell(0, 8, "Trade setups for today (top 20, long side)", new_x="LMARGIN", new_y="NEXT")
    setup_table(pdf, setups[:20])
    footer(pdf, "Trigger = previous high +0.1%. Stop = trigger - 0.5x ATR(14). T1 = 1R, T2 = 2R. Valid only on a 5-min close above trigger after 9:30.")
    path = os.path.join(OUT, "NSE_PreMarket_%s.pdf" % now.strftime("%Y-%m-%d")); pdf.output(path); return path


def hdr(pdf, cols):
    pdf.set_font("Helvetica", "B", 8.5); pdf.set_fill_color(31, 56, 100); pdf.set_text_color(255, 255, 255)
    for c, w in cols:
        pdf.cell(w, 7, c, border=1, align="C", fill=True)
    pdf.ln(); pdf.set_text_color(0, 0, 0); pdf.set_font("Helvetica", "", 8.5)


def setup_table(pdf, rows, live=False):
    cols = [("#", 8), ("Symbol", 28), ("Setup", 52), ("LTP" if live else "Close", 22), ("ENTRY (buy above)", 30),
            ("STOP LOSS", 24), ("TARGET 1", 22), ("TARGET 2", 22), ("Status" if live else "Notes", 69)]
    hdr(pdf, cols)
    fills = {4: (226, 239, 218), 5: (252, 228, 214), 6: (255, 242, 204), 7: (255, 242, 204)}
    if not rows:
        pdf.cell(0, 7, "No setup matched the rules.", border=1, new_x="LMARGIN", new_y="NEXT")
    for i, c in enumerate(rows, 1):
        vals = [str(i), c["sym"], c["setup"][:34], "%.2f" % (c["ltp"] if live else c.get("close", c.get("s", {}).get("close", 0))),
                "%.2f" % c["entry"], "%.2f" % c["stop"], "%.2f" % c["t1"], "%.2f" % c["t2"], latin((c["status"] if live else c["note"])[:46])]
        for j, ((_, w), v) in enumerate(zip(cols, vals)):
            if j in fills:
                pdf.set_fill_color(*fills[j])
            pdf.set_font("Helvetica", "B" if j in (1, 4, 5) else "", 8.5)
            pdf.cell(w, 6.5, v, border=1, align="L" if j in (1, 2, 8) else "R", fill=j in fills)
        pdf.ln()


def footer(pdf, rule):
    pdf.ln(2); pdf.set_font("Helvetica", "", 8.5); pdf.multi_cell(0, 4.6, rule, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(192, 0, 0); pdf.set_font("Helvetica", "I", 8.5)
    pdf.cell(0, 6, "Educational technical scan only - not SEBI-registered investment advice. A setup matched a rule; it is not a buy call.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def morning_xlsx(now, ctx, view, news, setups, by_turn):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook(); H = Font(bold=True, color="FFFFFF"); HF = PatternFill("solid", fgColor="1F3864")

    def sheet(ws, header, rows, widths):
        ws.append(header)
        for c in ws[1]:
            c.font = H; c.fill = HF; c.alignment = Alignment(wrap_text=True, horizontal="center")
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        ws.freeze_panes = "A2"
    ws = wb.active; ws.title = "Market"
    sheet(ws, ["Index", "Close", "Day %", "Week %", "RSI", "20DMA", "50DMA", "Pivot", "R1", "R2", "S1", "S2"],
          [[n, round(s["close"], 2), round(s["chg1"], 2), round(s["chg5"], 2), round(s["rsi"], 1), round(s["ma20"], 2), round(s["ma50"], 2)]
           + [round(s[k], 2) for k in ("pivot", "r1", "r2", "s1", "s2")] for n, s in ctx.items()], [16] + [11] * 11)
    ws.append([]); [ws.append([v]) for v in view]
    sheet(wb.create_sheet("Trade Setups"), ["Rank", "Symbol", "Setup", "Score", "Close", "RSI", "Vol x avg", "ENTRY (buy above)",
          "STOP LOSS", "TARGET 1", "TARGET 2", "Risk/share", "Qty for Rs1,000 risk", "Notes"],
          [[i, x["sym"], x["setup"], x["score"], x["s"]["close"], round(x["s"]["rsi"], 1), round(x["s"]["vr"], 1), x["entry"], x["stop"],
            x["t1"], x["t2"], round(x["risk"], 2), int(1000 // x["risk"]), x["note"]] for i, x in enumerate(setups, 1)],
          [6, 13, 28, 7, 11, 7, 9, 14, 11, 11, 11, 10, 12, 40])
    sheet(wb.create_sheet("News"), ["Headline", "Source", "Date", "Link"], [[n["title"], n["src"], n["date"], n["link"]] for n in news], [90, 20, 24, 60])
    sheet(wb.create_sheet("All Scanner Hits"), ["Symbol", "Setups", "Close", "RSI", "Volume"],
          [[h["symbol"], ", ".join(LABEL[x] for x in h["setups"]), h["price"], h["rsi"], h["volume"]] for h in by_turn], [14, 40, 11, 8, 14])
    path = os.path.join(OUT, "NSE_PreMarket_%s.xlsx" % now.strftime("%Y-%m-%d")); wb.save(path); return path


def table_html(rows, live):
    td = 'style="border:1px solid #bbb;padding:4px 8px"'
    th = "".join("<th %s>%s</th>" % (td, h) for h in ["#", "Symbol", "Setup", "LTP" if live else "Close", "ENTRY", "STOP LOSS",
                                                      "TARGET 1", "TARGET 2", "Status" if live else "Notes"])
    body = "".join(
        "<tr><td %s>%d</td><td %s><b>%s</b></td><td %s>%s</td><td %s align=right>%.2f</td>"
        "<td %s align=right style='background:#e2efda'><b>%.2f</b></td><td %s align=right style='background:#fce4d6'><b>%.2f</b></td>"
        "<td %s align=right>%.2f</td><td %s align=right>%.2f</td><td %s>%s</td></tr>"
        % (td, i, td, c["sym"], td, c["setup"], td, c["ltp"] if live else c["s"]["close"], td, c["entry"], td, c["stop"],
           td, c["t1"], td, c["t2"], td, c["status"] if live else c["note"]) for i, c in enumerate(rows, 1))
    return ("<table style='border-collapse:collapse;font-family:Arial;font-size:13px'><tr style='background:#1f3864;color:#fff'>%s</tr>%s</table>"
            % (th, body or "<tr><td colspan=9>No setup matched.</td></tr>"))


DISCLAIMER = "<p style='color:#c00000;font-size:12px'><i>Educational technical scan on free, delayed data - not investment advice.</i></p>"


def morning_html(now, ctx, view, news, setups):
    v = "".join("<li>%s</li>" % x for x in view)
    n = "".join("<li><a href='%s'>%s</a> <span style='color:#888'>(%s)</span></li>" % (x["link"], x["title"], x["src"] or x["date"]) for x in news)
    return ("<h2>NSE Pre-Market Analysis - %s</h2><h3>Market view</h3><ul>%s</ul><h3>Top 10 trade setups (buy above trigger)</h3>%s"
            "<p style='font-size:12px'>Trigger = previous high +0.1%%, Stop = trigger - 0.5x ATR, T1 = 1R, T2 = 2R. Full list (top 20) in the PDF; all setups in the Excel.</p>"
            "<h3>Latest news</h3><ul>%s</ul>%s" % (now.strftime("%A %d %b %Y"), v, table_html(setups[:10], False), n, DISCLAIMER))


# ======================================================================= INTRADAY (every 15 min)
def cmd_run(args):
    now = now_ist(args.get("now")); d = now.date()
    ok, why = scan.trading_day(d)
    if not ok and not args.get("force"):
        log("run: market closed (%s)" % why); return
    if not args.get("force") and not (datetime.time(9, 30) <= now.time() <= datetime.time(15, 25)):
        log("run: outside 9:30-15:25 IST, skip"); return
    daily = json.load(open("trader/daily.json"))
    dsetups = {h["symbol"]: h for h in daily["hits"]}
    plan = json.load(open("trader/plan.json")) if os.path.exists("trader/plan.json") else {"setups": []}
    cmd = [PY, "scan.py", "orb", "--watchlist", "trader/daily.json", "--no-write", "--json", "--quiet", "--max-confirm", "0"]
    if args.get("now"):
        cmd += ["--now", args["now"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        orb = json.loads(r.stdout)
    except Exception:
        log("orb scan:", (r.stderr or "")[-300:].strip()); orb = {"hits": [], "scanned": 0}
    log("orb scanned", orb.get("scanned"), "hits", len(orb.get("hits", [])))
    upto = int((now - datetime.timedelta(hours=5, minutes=30) - datetime.datetime(1970, 1, 1)).total_seconds()) if args.get("now") else None

    cands = []
    for h in orb.get("hits", []):
        sym = h["symbol"]; t = tick(h["or_high"])
        entry = rup(h["or_high"] + t, t)
        stop = rdown(max((h["or_high"] + h["or_low"]) / 2, entry * 0.985), t); risk = entry - stop
        if risk <= 0:
            continue
        ltp, _ = last_price(sym + ".NS", upto); time.sleep(0.2); ltp = ltp or h["price"]
        t1, t2 = rup(entry + risk, t), rup(entry + 2 * risk, t)
        if ltp < stop:
            continue
        status = "Above T1 - late, skip/trail" if ltp >= t1 else ("Triggered %s" % h["at"]) if ltp >= entry else "Below entry - wait"
        ext = (ltp - entry) / entry * 100
        dset = dsetups.get(sym, {}).get("setups", [])
        score = 3 + (1 if ltp >= entry else 0) + len(dset) + (1 if "breakout_20d" in dset else 0) - (2 if ltp >= t1 else 0) - (1 if ext > 1 else 0)
        cands.append(dict(sym=sym, setup=", ".join(["ORB"] + [LABEL[x] for x in dset]), ltp=ltp, entry=entry, stop=stop,
                          t1=t1, t2=t2, status=status, score=score, ext=ext))
    # top up from the morning plan (stocks still waiting for their trigger)
    have = {c["sym"] for c in cands}
    for p in plan["setups"]:
        if len(cands) >= 10:
            break
        if p["sym"] in have:
            continue
        ltp, _ = last_price(p["sym"] + ".NS", upto); time.sleep(0.2)
        if not ltp or ltp < p["stop"]:
            continue
        if ltp >= p["t1"]:
            continue
        st = ("Triggered (above entry)" if ltp >= p["entry"] else "Waiting: %.1f%% below entry" % ((p["entry"] - ltp) / p["entry"] * 100))
        cands.append(dict(sym=p["sym"], setup=p["setup"], ltp=ltp, entry=p["entry"], stop=p["stop"], t1=p["t1"], t2=p["t2"],
                          status=st, score=1 + (1 if ltp >= p["entry"] else 0), ext=0))
    cands.sort(key=lambda c: (-c["score"], abs(c["ext"])))
    top = cands[:10]
    idx = []
    for name, ys in INDICES[:4]:
        p, prev = last_price(ys, upto)
        if p and prev:
            idx.append((name, p, (p / prev - 1) * 100))
    pdf = intraday_pdf(now, top, idx, orb)
    log("pdf", pdf)
    if not args.get("no_send"):
        head = " | ".join("%s %s (%+.2f%%)" % (n, format(p, ",.2f"), c) for n, p, c in idx)
        html = ("<h3>NSE Top 10 Intraday Setups - %s IST</h3><p>%s</p>%s<p style='font-size:12px'>Entry = 9:15-9:30 range high + 1 tick "
                "(enter on a 5-min close above). Stop = range midpoint (max 1.5%%). T1 = 1R, T2 = 2R. No entries after 15:00; square off by 15:15.</p>%s"
                % (now.strftime("%d %b %Y %H:%M"), head, table_html(top, True), DISCLAIMER))
        send_email("NSE Top 10 Setups - %s IST" % now.strftime("%d %b %Y %H:%M"), html, [pdf])


def intraday_pdf(now, top, idx, orb):
    from fpdf import FPDF
    pdf = FPDF(orientation="L", unit="mm", format="A4"); pdf.add_page(); pdf.set_auto_page_break(True, 10)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "NSE Top 10 Intraday Setups - %s IST" % now.strftime("%d %b %Y, %H:%M"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "  |  ".join("%s %s (%+.2f%%)" % (n, format(p, ",.2f"), c) for n, p, c in idx) or "Index data unavailable", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Scanned %s charts from today's setup list; %d opening-range breakouts." % (orb.get("scanned", 0), len(orb.get("hits", []))),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    setup_table(pdf, top, live=True)
    footer(pdf, "Entry = 15-min opening-range high + 1 tick (enter only on a 5-min close above it); 'Waiting' rows use the morning trigger. "
                "Stop = range midpoint, max 1.5% risk. T1 = 1R, T2 = 2R. Book 50% at T1, move stop to entry. No new entries after 15:00; square off by 15:15 IST.")
    path = os.path.join(OUT, "NSE_Top10_%s.pdf" % now.strftime("%Y-%m-%d_%H%M")); pdf.output(path); return path


# ======================================================================= EMAIL
def settings():
    env = {}
    p = os.path.join(HOME, "email.env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"')
    for k in ("EMAIL_FROM", "EMAIL_APP_PASSWORD", "EMAIL_TO"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def send_email(subject, html, files):
    import smtplib, ssl
    from email.message import EmailMessage
    env = settings()
    frm, pw = env.get("EMAIL_FROM"), (env.get("EMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env.get("EMAIL_TO") or frm
    if not (frm and pw) or "PASTE" in pw:
        log("email: not configured - skipped"); return
    m = EmailMessage(); m["Subject"], m["From"], m["To"] = subject, frm, to
    m.set_content(re.sub(r"<[^>]+>", " ", html))
    m.add_alternative(html, subtype="html")
    for f in files:
        sub = "pdf" if f.endswith(".pdf") else "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        m.add_attachment(open(f, "rb").read(), maintype="application", subtype=sub, filename=os.path.basename(f))
    for i in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
                s.login(frm, pw); s.send_message(m)
            log("email sent to", to); return
        except Exception as e:
            log("email error:", e); time.sleep(5)


def main():
    a = sys.argv[1:]
    if not a or a[0] not in ("morning", "run"):
        print(__doc__); return
    args = {"now": a[a.index("--now") + 1] if "--now" in a else None, "no_send": "--no-send" in a, "force": "--force" in a}
    (cmd_morning if a[0] == "morning" else cmd_run)(args)


if __name__ == "__main__":
    main()
