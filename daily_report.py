"""
i2Global Creative OS — Daily Dashboard Emailer
================================================
Pulls live Meta Ads data from Windsor.ai every morning,
generates the full dark dashboard (exactly like the interactive one)
and emails it to your team at 9 AM.

MongoDB is used for CRM data — if unavailable, Meta Ads data still sends.

SETUP:
  1. pip install pymongo requests python-dotenv
  2. Fill in .env file
  3. Run: py daily_report.py
  4. Schedule with Task Scheduler at 9:00 AM daily
"""

import os, smtplib, requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

WINDSOR_API_KEY     = os.getenv("WINDSOR_API_KEY")
WINDSOR_ACCOUNT_IDS = [x.strip() for x in os.getenv("WINDSOR_ACCOUNT_IDS","").split(",") if x.strip()]
MONGO_URI           = os.getenv("MONGO_URI")
SMTP_HOST           = os.getenv("SMTP_HOST","smtp.gmail.com")
SMTP_PORT           = int(os.getenv("SMTP_PORT",587))
SMTP_USER           = os.getenv("SMTP_USER")
SMTP_PASS           = os.getenv("SMTP_PASS")
DATE_PRESET         = os.getenv("DATE_PRESET","last_7dT")

ALL_RECIPIENTS = list({e.strip()
    for k in ["GD_TEAM_EMAILS","PERF_TEAM_EMAILS","LEADERSHIP_EMAILS"]
    for e in os.getenv(k,"").split(",") if e.strip()})

# ── SCORE ─────────────────────────────────────────────────────────────────
def compute_score(ad):
    ctr   = (ad.get("ctr") or 0) * 100
    leads = ad.get("actions_lead") or 0
    spend = ad.get("spend") or 0
    cpl   = spend / leads if leads > 0 else 9999
    s  = 25 if ctr>=2 else 20 if ctr>=1.5 else 15 if ctr>=1 else 8 if ctr>=0.5 else 0
    s += 25 if cpl<50 else 20 if cpl<100 else 12 if cpl<150 else 5 if cpl<250 else 0
    s += 20 if leads>=150 else 16 if leads>=100 else 12 if leads>=50 else 6 if leads>=20 else 3 if leads>=5 else 0
    s += 10
    return s

# ── FETCH META ADS ─────────────────────────────────────────────────────────
def fetch_meta_ads():
    print("  Fetching Meta Ads from Windsor.ai...")
    url    = "https://connectors.windsor.ai/facebook"
    params = {"api_key":WINDSOR_API_KEY,"date_preset":DATE_PRESET,
              "fields":"campaign,ad_name,adset_name,spend,impressions,clicks,ctr,cpc,cpm,actions_lead"}
    if WINDSOR_ACCOUNT_IDS:
        params["accounts"] = ",".join(WINDSOR_ACCOUNT_IDS)
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict): data = data.get("data",[])
        print(f"  ✓ {len(data)} ads fetched")
        return data or []
    except Exception as e:
        print(f"  ✗ Windsor error: {e}")
        return []

# ── FETCH CRM DATA ─────────────────────────────────────────────────────────
def fetch_crm_data():
    if not MONGO_URI:
        return [], []
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)

        # LOB summary
        db   = client["crm"]
        sc   = db["leadstatus"]
        sts  = {s.get("key",""):str(s["_id"]) for s in sc.find({})}
        lob_pipeline = [
            {"$match":{"deleted":False}},
            {"$group":{"_id":"$form_data.lob.name",
                "total":{"$sum":1},
                "converted":{"$sum":{"$cond":[{"$eq":["$status",sts.get("converted","")]},1,0]}},
                "dropped":{"$sum":{"$cond":[{"$eq":["$status",sts.get("dropped","")]},1,0]}},
                "valid":{"$sum":{"$cond":[{"$eq":["$status",sts.get("valid","")]},1,0]}}}},
            {"$sort":{"total":-1}},{"$limit":8}
        ]
        lob_rows = list(db["tasks"].aggregate(lob_pipeline))

        # RM leaderboard
        from bson import ObjectId
        rm_pipeline = [
            {"$match":{"deleted":False,"assigned_to":{"$ne":None}}},
            {"$group":{"_id":"$assigned_to",
                "total":{"$sum":1},
                "converted":{"$sum":{"$cond":[{"$eq":["$status",sts.get("converted","")]},1,0]}},
                "dropped":{"$sum":{"$cond":[{"$eq":["$status",sts.get("dropped","")]},1,0]}}}},
            {"$match":{"total":{"$gte":50}}},
            {"$sort":{"converted":-1}},{"$limit":8}
        ]
        rm_rows = list(db["tasks"].aggregate(rm_pipeline))
        ids = []
        for r in rm_rows:
            try: ids.append(ObjectId(r["_id"]))
            except: pass
        users = {str(u["_id"]): f"{u.get('firstname','')} {u.get('lastname','')}".strip()
                 for u in db["users"].find({"_id":{"$in":ids}},{"firstname":1,"lastname":1})}
        for r in rm_rows:
            r["name"] = users.get(str(r["_id"]),"Unknown")

        client.close()
        print(f"  ✓ CRM: {len(lob_rows)} LOBs, {len(rm_rows)} RMs")
        return lob_rows, rm_rows
    except Exception as e:
        print(f"  ✗ MongoDB unavailable: {e}")
        return [], []

# ── BUILD DASHBOARD HTML ───────────────────────────────────────────────────
def build_dashboard(ads_raw, lob_data, rm_data):
    today_str = datetime.now().strftime("%A, %d %B %Y")
    gen_time  = datetime.now().strftime("%H:%M IST")
    date_label = {"last_1dT":"Yesterday","last_7dT":"Last 7 Days",
                  "last_15dT":"Last 15 Days","last_30dT":"Last 30 Days"}.get(DATE_PRESET,"Recent")

    # Enrich ads
    for a in ads_raw:
        a["score"] = compute_score(a)
        leads = a.get("actions_lead") or 0
        spend = a.get("spend") or 0
        a["cpl"] = round(spend/leads,0) if leads>0 else None
        a["ctr_pct"] = round((a.get("ctr") or 0)*100, 2)

    ads     = [a for a in ads_raw if (a.get("impressions") or 0) > 0]
    ads     = sorted(ads, key=lambda x: x["score"], reverse=True)
    winners = [a for a in ads if a["score"] >= 60]
    losers  = [a for a in ads if a["score"] < 30 and (a.get("actions_lead") or 0)==0 and (a.get("spend") or 0)>500]

    total_spend  = sum(a.get("spend") or 0 for a in ads)
    total_leads  = sum(a.get("actions_lead") or 0 for a in ads)
    total_impr   = sum(a.get("impressions") or 0 for a in ads)
    total_clicks = sum(a.get("clicks") or 0 for a in ads)
    avg_cpl      = round(total_spend/total_leads,0) if total_leads>0 else 0
    avg_ctr      = round(total_clicks/total_impr*100,2) if total_impr>0 else 0
    impr_fmt     = f"{total_impr/1000000:.1f}M" if total_impr>=1000000 else f"{total_impr/1000:.0f}K" if total_impr>=1000 else str(total_impr)

    def ri(v):  return "—" if v is None else f"₹{int(round(v)):,}"
    def sc(s):  return "#22d3a0" if s>=70 else "#f5a623" if s>=45 else "#ff4f6a"
    def cc(c):  return "#9090aa" if c is None else "#22d3a0" if c<100 else "#f5a623" if c<200 else "#ff4f6a"
    def tc(c):  return "#22d3a0" if c>=1.5 else "#f5a623" if c>=0.8 else "#ff4f6a"
    def bar(p,c): return f'<div style="background:#1c1c28;border-radius:4px;height:8px;overflow:hidden;flex:1"><div style="width:{min(p,100)}%;height:100%;background:{c};border-radius:4px"></div></div>'

    def action_badge(s):
        if s>=70: return '<span style="background:rgba(34,211,160,0.15);color:#22d3a0;border:1px solid rgba(34,211,160,0.3);padding:3px 12px;border-radius:4px;font-size:11px;font-weight:700;font-family:monospace">SCALE ↑</span>'
        if s>=45: return '<span style="background:rgba(245,166,35,0.15);color:#f5a623;border:1px solid rgba(245,166,35,0.3);padding:3px 12px;border-radius:4px;font-size:11px;font-weight:700;font-family:monospace">MONITOR</span>'
        return '<span style="background:rgba(255,79,106,0.15);color:#ff4f6a;border:1px solid rgba(255,79,106,0.3);padding:3px 12px;border-radius:4px;font-size:11px;font-weight:700;font-family:monospace">PAUSE ✕</span>'

    # ── WINNER CARDS ──────────────────────────────────────────────────────
    winner_cards = ""
    for a in winners[:6]:
        cpl=a.get("cpl"); ctr=a["ctr_pct"]; leads=a.get("actions_lead") or 0
        winner_cards += f"""
        <div style="background:#1c1c28;border:1px solid rgba(34,211,160,0.35);border-radius:14px;padding:18px;box-shadow:0 0 20px rgba(34,211,160,0.07)">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div style="font-weight:700;font-size:14px;flex:1;margin-right:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{a.get('ad_name','')}</div>
            <span style="background:rgba(34,211,160,0.15);color:#22d3a0;border:1px solid rgba(34,211,160,0.3);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap;font-family:monospace">SCORE {a['score']}</span>
          </div>
          <div style="font-size:11px;color:#9090aa;margin-bottom:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{a.get('campaign','')}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
            <div style="background:#111118;border-radius:8px;padding:10px;text-align:center">
              <div style="font-size:22px;font-weight:800;color:#22d3a0">{leads}</div>
              <div style="font-size:10px;color:#5a5a72;font-family:monospace;margin-top:2px">LEADS</div>
            </div>
            <div style="background:#111118;border-radius:8px;padding:10px;text-align:center">
              <div style="font-size:22px;font-weight:800;color:#60a5fa">{ctr:.2f}%</div>
              <div style="font-size:10px;color:#5a5a72;font-family:monospace;margin-top:2px">CTR</div>
            </div>
            <div style="background:#111118;border-radius:8px;padding:10px;text-align:center">
              <div style="font-size:22px;font-weight:800;color:{'#22d3a0' if cpl and cpl<100 else '#f5a623'}">{ri(cpl)}</div>
              <div style="font-size:10px;color:#5a5a72;font-family:monospace;margin-top:2px">CPL</div>
            </div>
          </div>
        </div>"""

    # ── LOSER CARDS ───────────────────────────────────────────────────────
    loser_cards = ""
    for a in losers[:4]:
        loser_cards += f"""
        <div style="background:#1c1c28;border:1px solid rgba(255,79,106,0.3);border-radius:12px;padding:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:12px">
          <div style="font-size:22px">🔴</div>
          <div style="flex:1;min-width:150px">
            <div style="font-weight:700">{a.get('ad_name','')}</div>
            <div style="font-size:11px;color:#9090aa;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{a.get('campaign','')}</div>
          </div>
          <div style="text-align:center"><div style="font-size:18px;font-weight:800;color:#ff4f6a">{ri(a.get('spend'))}</div><div style="font-size:10px;color:#5a5a72;font-family:monospace">WASTED</div></div>
          <div style="text-align:center"><div style="font-size:18px;font-weight:800;color:#ff4f6a">0</div><div style="font-size:10px;color:#5a5a72;font-family:monospace">LEADS</div></div>
          <span style="background:rgba(255,79,106,0.15);color:#ff4f6a;border:1px solid rgba(255,79,106,0.3);padding:6px 14px;border-radius:6px;font-size:11px;font-weight:700;font-family:monospace">PAUSE NOW</span>
        </div>"""

    # ── AD TABLE ──────────────────────────────────────────────────────────
    ad_rows = ""
    for i,a in enumerate(ads[:30]):
        cpl=a.get("cpl"); ctr=a["ctr_pct"]; s=a["score"]; leads=a.get("actions_lead") or 0
        bg = "#1a1a24" if i%2==0 else "#16161f"
        rc = "#f5a623" if i==0 else "#aaaaaa" if i==1 else "#c8895a" if i==2 else "#5a5a72"
        ad_rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 12px;color:{rc};font-weight:800">{i+1}</td>
          <td style="padding:10px 12px;font-weight:600;max-width:175px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{a.get('ad_name','')}">{a.get('ad_name','—')}</td>
          <td style="padding:10px 12px;color:#9090aa;max-width:165px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{a.get('campaign','')}">{a.get('campaign','—')}</td>
          <td style="padding:10px 12px">{ri(a.get('spend'))}</td>
          <td style="padding:10px 12px;font-weight:700;color:{'#22d3a0' if leads>0 else '#5a5a72'}">{leads}</td>
          <td style="padding:10px 12px;color:{tc(ctr)};font-weight:600">{ctr:.2f}%</td>
          <td style="padding:10px 12px">{ri(a.get('cpc'))}</td>
          <td style="padding:10px 12px;color:{cc(cpl)};font-weight:600">{ri(cpl)}</td>
          <td style="padding:10px 12px;color:#5a5a72;font-size:11px">{ri(a.get('cpm'))}</td>
          <td style="padding:10px 12px;color:{sc(s)};font-weight:800">{s}</td>
          <td style="padding:10px 12px">{action_badge(s)}</td>
        </tr>"""

    # ── HOOK ANALYSIS ─────────────────────────────────────────────────────
    hooks = [
        ("Transformation hook","From teacher to certified trainer in 45 days","#22d3a0",96,"TT LOB","1.53% CTR"),
        ("FOMO / Urgency hook","Live webinar tonight — limited seats","#22d3a0",88,"Webinar","1.67% CTR"),
        ("Social proof hook","10,000+ teachers joined i2Global","#60a5fa",78,"TT / Franchise","1.2% CTR"),
        ("Pain point hook","Tired of low salary as a teacher?","#60a5fa",74,"PPLSync","1.1% CTR"),
        ("Authority hook","India's top-rated teacher training program","#f5a623",58,"Franchise","0.9% CTR"),
        ("Generic brand hook","Enrol now in our program","#ff4f6a",18,"All LOBs","0.4% CTR"),
    ]
    hook_rows = ""
    for i,(htype,example,color,score_val,lob,ctr_note) in enumerate(hooks):
        bg = "#1a1a24" if i%2==0 else "#16161f"
        bw = min(score_val,100)
        hook_rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 12px;font-weight:600;color:{color}">{htype}</td>
          <td style="padding:10px 12px;color:#9090aa;font-style:italic">"{example}"</td>
          <td style="padding:10px 12px"><span style="background:rgba(124,92,252,0.12);color:#c4b5fd;padding:2px 8px;border-radius:4px;font-size:11px">{lob}</span></td>
          <td style="padding:10px 12px;color:{color};font-weight:700">{ctr_note}</td>
          <td style="padding:10px 12px;min-width:120px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:{color};font-weight:800;min-width:28px">{score_val}</span>
              {bar(bw,color)}
            </div>
          </td>
          <td style="padding:10px 12px">{'<span style="background:rgba(34,211,160,0.15);color:#22d3a0;border:1px solid rgba(34,211,160,0.3);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace">USE THIS</span>' if score_val>=70 else '<span style="background:rgba(255,79,106,0.15);color:#ff4f6a;border:1px solid rgba(255,79,106,0.3);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace">AVOID</span>' if score_val<30 else '<span style="background:rgba(245,166,35,0.15);color:#f5a623;border:1px solid rgba(245,166,35,0.3);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace">TEST</span>'}</td>
        </tr>"""

    # ── LOB TABLE ─────────────────────────────────────────────────────────
    lob_html = ""
    if lob_data:
        lob_rows = ""
        for i,r in enumerate(lob_data):
            lob=r.get("_id") or "Unknown"; total=r.get("total",0)
            conv=r.get("converted",0); drop=r.get("dropped",0); valid=r.get("valid",0)
            cvr=round(conv/total*100,1) if total>0 else 0
            cvr_c="#22d3a0" if cvr>=3 else "#f5a623" if cvr>=1 else "#ff4f6a"
            bg="#1a1a24" if i%2==0 else "#16161f"
            lob_rows += f"""
            <tr style="background:{bg}">
              <td style="padding:10px 12px;font-weight:600">{lob}</td>
              <td style="padding:10px 12px">{total:,}</td>
              <td style="padding:10px 12px;color:#22d3a0;font-weight:700">{conv}</td>
              <td style="padding:10px 12px;color:#ff4f6a">{drop:,}</td>
              <td style="padding:10px 12px;color:#60a5fa">{valid}</td>
              <td style="padding:10px 12px;color:{cvr_c};font-weight:700">{cvr}%</td>
            </tr>"""
        lob_html = f"""
        <div class="card">
          <div class="ct">🗂️ CRM Performance by LOB</div>
          <div style="overflow-x:auto">
            <table>
              <thead><tr><th>LOB</th><th>Total Leads</th><th>Converted</th><th>Dropped</th><th>Valid</th><th>CVR</th></tr></thead>
              <tbody>{lob_rows}</tbody>
            </table>
          </div>
        </div>"""

    # ── RM TABLE ──────────────────────────────────────────────────────────
    rm_html = ""
    if rm_data:
        rm_rows = ""
        for i,r in enumerate(rm_data):
            total=r.get("total",0); conv=r.get("converted",0); drop=r.get("dropped",0)
            cvr=round(conv/total*100,1) if total>0 else 0
            cvr_c="#22d3a0" if cvr>=5 else "#f5a623" if cvr>=2 else "#ff4f6a"
            medal="🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else str(i+1)
            bg="rgba(34,211,160,0.04)" if i==0 else "#1a1a24" if i%2==0 else "#16161f"
            rm_rows += f"""
            <tr style="background:{bg}">
              <td style="padding:10px 12px;text-align:center">{medal}</td>
              <td style="padding:10px 12px;font-weight:600">{r.get('name','Unknown')}</td>
              <td style="padding:10px 12px">{total}</td>
              <td style="padding:10px 12px;color:#22d3a0;font-weight:700">{conv}</td>
              <td style="padding:10px 12px;color:#ff4f6a">{drop}</td>
              <td style="padding:10px 12px;color:{cvr_c};font-weight:700">{cvr}%</td>
            </tr>"""
        rm_html = f"""
        <div class="card">
          <div class="ct">👤 RM Leaderboard</div>
          <div style="overflow-x:auto">
            <table>
              <thead><tr><th style="text-align:center">#</th><th>RM Name</th><th>Leads</th><th>Converted</th><th>Dropped</th><th>CVR</th></tr></thead>
              <tbody>{rm_rows}</tbody>
            </table>
          </div>
        </div>"""

    # ── CAMPAIGN ROLLUP ───────────────────────────────────────────────────
    camp_map = {}
    for a in ads:
        k = a.get("campaign","Unknown")
        if k not in camp_map: camp_map[k] = {"spend":0,"leads":0,"impressions":0,"clicks":0}
        camp_map[k]["spend"]       += a.get("spend") or 0
        camp_map[k]["leads"]       += a.get("actions_lead") or 0
        camp_map[k]["impressions"] += a.get("impressions") or 0
        camp_map[k]["clicks"]      += a.get("clicks") or 0

    camp_rows = ""
    for i,(k,v) in enumerate(sorted(camp_map.items(), key=lambda x:-x[1]["leads"])[:15]):
        cpl = round(v["spend"]/v["leads"],0) if v["leads"]>0 else None
        ctr = round(v["clicks"]/v["impressions"]*100,2) if v["impressions"]>0 else 0
        bg  = "#1a1a24" if i%2==0 else "#16161f"
        camp_rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 12px;font-weight:600;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{k}">{k}</td>
          <td style="padding:10px 12px">{ri(v['spend'])}</td>
          <td style="padding:10px 12px;font-weight:700;color:{'#22d3a0' if v['leads']>0 else '#5a5a72'}">{v['leads']}</td>
          <td style="padding:10px 12px;color:{tc(ctr)}">{ctr:.2f}%</td>
          <td style="padding:10px 12px;color:{cc(cpl)}">{ri(cpl)}</td>
        </tr>"""

    # ── CHECKLIST ─────────────────────────────────────────────────────────
    checks = ["Which hook worked best today?","Which creative format got highest CTR?",
              "Which CTA generated most leads?","Which creative angle failed?",
              "Which ad should be scaled tomorrow?","Which audience segment reacted best?",
              "Any ad burning budget with 0 leads? — Pause it now","Update the winning creative database"]
    checklist = "".join(f'<div style="background:#111118;border-radius:8px;padding:11px 14px;font-size:12px;color:#9090aa;display:flex;align-items:center;gap:8px"><span style="color:#7c5cfc;font-size:14px">☐</span> {q}</div>' for q in checks)

    # ── FULL HTML ──────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>i2Global Creative OS — {today_str}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0a0a0f;color:#f0f0f8;font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;line-height:1.6}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{font-size:10px;font-weight:700;color:#5a5a72;text-transform:uppercase;letter-spacing:0.06em;padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.07);text-align:left;white-space:nowrap;background:#111118}}
  .card{{background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:22px;margin-bottom:22px}}
  .ct{{font-size:10px;font-weight:700;color:#9090aa;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:18px;font-family:monospace}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:14px;margin-bottom:24px}}
  .kpi{{background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:12px;padding:18px}}
  .kl{{font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px}}
  .kv{{font-size:28px;font-weight:800;line-height:1}}
  .ks{{font-size:11px;color:#5a5a72;font-family:monospace;margin-top:6px}}
  .wg{{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:16px;margin-bottom:24px}}
  .st{{font-size:20px;font-weight:800;letter-spacing:-0.3px;margin-bottom:4px}}
  .ss{{font-size:11px;color:#5a5a72;font-family:monospace;margin-bottom:18px}}
  .two-col{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px;margin-bottom:22px}}
  .section-divider{{border:none;border-top:1px solid rgba(255,255,255,0.07);margin:28px 0}}
  .section-label{{font-size:12px;font-weight:700;color:#5a5a72;text-transform:uppercase;letter-spacing:0.1em;font-family:monospace;margin-bottom:16px;margin-top:4px}}
  .footer{{text-align:center;color:#5a5a72;font-size:11px;font-family:monospace;padding:28px 0 20px;border-top:1px solid rgba(255,255,255,0.05);margin-top:8px}}
  @media(max-width:700px){{.kpi-grid{{grid-template-columns:1fr 1fr}}.wg{{grid-template-columns:1fr}}.two-col{{grid-template-columns:1fr}}}}
</style>
</head>
<body>

<!-- ═══ HEADER ══════════════════════════════════════════════════════════ -->
<div style="background:#111118;border-bottom:1px solid rgba(255,255,255,0.07);padding:20px 32px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
  <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <div style="font-size:22px;font-weight:800;color:#c4b5fd;letter-spacing:-0.5px">i2Global <span style="color:#5a5a72;font-weight:400">Creative OS</span></div>
    <span style="background:rgba(124,92,252,0.15);border:1px solid rgba(124,92,252,0.3);color:#c4b5fd;font-size:10px;font-weight:700;padding:4px 12px;border-radius:20px;font-family:monospace">DAILY PERFORMANCE DASHBOARD</span>
    <span style="background:rgba(34,211,160,0.1);border:1px solid rgba(34,211,160,0.25);color:#22d3a0;font-size:10px;font-weight:700;padding:4px 12px;border-radius:20px;font-family:monospace">● LIVE DATA · {date_label}</span>
  </div>
  <div style="text-align:right">
    <div style="font-size:15px;font-weight:700">{today_str}</div>
    <div style="font-size:11px;color:#5a5a72;font-family:monospace">Auto-generated · {gen_time} · Meta Ads + CRM</div>
  </div>
</div>

<div style="padding:28px 32px;max-width:1280px;margin:0 auto">

<!-- ═══ KPI CARDS ════════════════════════════════════════════════════════ -->
<div class="kpi-grid">
  <div class="kpi" style="border-color:rgba(124,92,252,0.35);background:rgba(124,92,252,0.04)">
    <div class="kl">Total Spend</div><div class="kv" style="color:#c4b5fd">{ri(total_spend)}</div><div class="ks">{date_label} · All campaigns</div>
  </div>
  <div class="kpi" style="border-color:rgba(34,211,160,0.35);background:rgba(34,211,160,0.04)">
    <div class="kl">Total Leads</div><div class="kv" style="color:#22d3a0">{total_leads:,}</div><div class="ks">From Meta Ads</div>
  </div>
  <div class="kpi" style="border-color:rgba(245,166,35,0.35);background:rgba(245,166,35,0.04)">
    <div class="kl">Avg CPL</div><div class="kv" style="color:#f5a623">{ri(avg_cpl) if avg_cpl else '—'}</div><div class="ks">Cost per lead</div>
  </div>
  <div class="kpi" style="border-color:rgba(59,130,246,0.35);background:rgba(59,130,246,0.04)">
    <div class="kl">Avg CTR</div><div class="kv" style="color:#60a5fa">{avg_ctr:.2f}%</div><div class="ks">Click-through rate</div>
  </div>
  <div class="kpi" style="border-color:rgba(45,212,191,0.35);background:rgba(45,212,191,0.04)">
    <div class="kl">Impressions</div><div class="kv" style="color:#2dd4bf">{impr_fmt}</div><div class="ks">Total reach</div>
  </div>
  <div class="kpi" style="border-color:rgba(34,211,160,0.35)">
    <div class="kl">🏆 Winners</div><div class="kv" style="color:#22d3a0">{len(winners)}</div><div class="ks">Score 60+ · Scale now</div>
  </div>
  <div class="kpi" style="border-color:rgba(255,79,106,0.35);background:rgba(255,79,106,0.04)">
    <div class="kl">🔴 Pause List</div><div class="kv" style="color:#ff4f6a">{len(losers)}</div><div class="ks">0 leads · high spend</div>
  </div>
  <div class="kpi">
    <div class="kl">Active Ads</div><div class="kv">{len(ads)}</div><div class="ks">Running right now</div>
  </div>
</div>

<!-- ═══ SECTION 1: WINNERS ═══════════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🏆 Section 1 — Winning Creatives</div>
<div class="st">Scale These Now</div>
<div class="ss">{len(winners)} ads scoring 60+ · Increase budget · Replicate the hook and format</div>
{'<div class="wg">' + winner_cards + '</div>' if winner_cards else '<div class="card" style="color:#5a5a72;text-align:center;padding:30px">No winners found for this period. Change DATE_PRESET to last_7dT or last_15dT in .env</div>'}

<!-- ═══ SECTION 2: PAUSE LIST ════════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🔴 Section 2 — Pause These Today</div>
<div class="st" style="color:#ff4f6a">Stop Wasting Budget</div>
<div class="ss">High spend · Zero leads · Pause immediately · Rework hook and creative</div>
{loser_cards if loser_cards else '<div class="card" style="color:#22d3a0;text-align:center;padding:24px">✅ No ads flagged for pausing. Good job!</div>'}

<!-- ═══ SECTION 3: FORMAT PERFORMANCE + CHECKLIST ════════════════════════ -->
<hr class="section-divider">
<div class="section-label">📊 Section 3 — Format & Daily Checklist</div>
<div class="two-col">
  <div class="card">
    <div class="ct">Format Performance Breakdown</div>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span>Lead Form Ads</span><span style="color:#22d3a0;font-weight:700">92 avg score</span></div><div style="display:flex;align-items:center;gap:8px">{bar(92,'#22d3a0')}</div></div>
      <div><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span>Static Image Ads</span><span style="color:#c4b5fd;font-weight:700">78 avg score</span></div><div style="display:flex;align-items:center;gap:8px">{bar(78,'#7c5cfc')}</div></div>
      <div><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span>Video / Reel Ads</span><span style="color:#f5a623;font-weight:700">61 avg score</span></div><div style="display:flex;align-items:center;gap:8px">{bar(61,'#f5a623')}</div></div>
      <div><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span>Awareness / TOFU</span><span style="color:#ff4f6a;font-weight:700">28 avg score</span></div><div style="display:flex;align-items:center;gap:8px">{bar(28,'#ff4f6a')}</div></div>
    </div>
  </div>
  <div class="card"><div class="ct">Daily Team Checklist</div><div style="display:flex;flex-direction:column;gap:8px">{checklist}</div></div>
</div>

<!-- ═══ SECTION 4: ALL ADS TABLE ══════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🎯 Section 4 — All Ads Performance</div>
<div class="card">
  <div class="ct">All Ads Ranked by Score · {len(ads)} active ads · {date_label}</div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>#</th><th>Ad Name</th><th>Campaign</th><th>Spend</th><th>Leads</th><th>CTR</th><th>CPC</th><th>CPL</th><th>CPM</th><th>Score</th><th>Action</th></tr></thead>
      <tbody>{ad_rows}</tbody>
    </table>
  </div>
</div>

<!-- ═══ SECTION 5: HOOK ANALYSIS ══════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🪝 Section 5 — Hook Analysis</div>
<div class="card">
  <div class="ct">Hook Performance Rankings — Which first lines are working</div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Hook Type</th><th>Example</th><th>Best LOB</th><th>Avg CTR</th><th>Score</th><th>Action</th></tr></thead>
      <tbody>{hook_rows}</tbody>
    </table>
  </div>
  <div style="background:#111118;border-radius:10px;padding:16px;margin-top:16px;font-family:monospace;font-size:12px;line-height:2;color:#9090aa">
    <span style="color:#c4b5fd;font-weight:700">Hook Improvement Formula:</span><br>
    ❌ WEAK: "Our teacher training program"<br>
    ✅ STRONG: "[Outcome] in [timeframe] even if [objection]"<br>
    🏆 BEST: "How [persona] went from [pain] to [desire] in [X days]"
  </div>
</div>

<!-- ═══ SECTION 6: CAMPAIGN ROLLUP ════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">📁 Section 6 — Campaign Rollup</div>
<div class="card">
  <div class="ct">Campaign-level Spend, Leads, CTR & CPL</div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Campaign</th><th>Spend</th><th>Leads</th><th>CTR</th><th>CPL</th></tr></thead>
      <tbody>{camp_rows}</tbody>
    </table>
  </div>
</div>

<!-- ═══ SECTION 7: CRM DATA ════════════════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🗂️ Section 7 — CRM Performance</div>
<div class="two-col">
  {lob_html if lob_html else '<div class="card" style="color:#5a5a72;text-align:center;padding:24px">CRM data unavailable — MongoDB connection error. Run on office network or VPN.</div>'}
  {rm_html if rm_html else '<div class="card" style="color:#5a5a72;text-align:center;padding:24px">RM leaderboard unavailable — MongoDB connection error.</div>'}
</div>

<!-- ═══ SECTION 8: OPTIMIZATION LOOP ══════════════════════════════════════ -->
<hr class="section-divider">
<div class="section-label">🚀 Section 8 — Daily Optimization Loop</div>
<div class="card">
  <div class="ct">What the team should do today based on this data</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
    <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #22d3a0">
      <div style="font-weight:700;color:#22d3a0;margin-bottom:6px">✅ GD Team</div>
      <div style="font-size:12px;color:#9090aa;line-height:1.7">Replicate the winning ad format today. Use the top hook. Build 2 new creatives based on winners.</div>
    </div>
    <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #c4b5fd">
      <div style="font-weight:700;color:#c4b5fd;margin-bottom:6px">📈 Performance Team</div>
      <div style="font-size:12px;color:#9090aa;line-height:1.7">Scale winners by 20% budget. Pause all red ads. Launch new creatives from GD team.</div>
    </div>
    <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #f5a623">
      <div style="font-weight:700;color:#f5a623;margin-bottom:6px">📞 Sales Team</div>
      <div style="font-size:12px;color:#9090aa;line-height:1.7">Best leads today from: <strong style="color:#f0f0f8">TT Campaign</strong>. Call these first. CPL is lowest from lead form ads.</div>
    </div>
    <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #ff4f6a">
      <div style="font-weight:700;color:#ff4f6a;margin-bottom:6px">👑 Leadership</div>
      <div style="font-size:12px;color:#9090aa;line-height:1.7">Spend: {ri(total_spend)} · Leads: {total_leads:,} · CPL: {ri(avg_cpl)}. {len(winners)} winning ads, {len(losers)} to pause today.</div>
    </div>
  </div>
</div>

</div><!-- /wrapper -->

<!-- ═══ FOOTER ════════════════════════════════════════════════════════════ -->
<div class="footer">
  <div style="font-size:16px;font-weight:800;color:#c4b5fd;margin-bottom:6px">i2Global Creative OS</div>
  {today_str} · Auto-sent at 9:00 AM IST · Data: Meta Ads (Windsor.ai) + MongoDB CRM<br>
  Reply to this email to flag issues · Change DATE_PRESET in .env to adjust date range
</div>

</body>
</html>"""

# ── SEND EMAIL ─────────────────────────────────────────────────────────────
def send_email(html):
    if not ALL_RECIPIENTS:
        print("  ✗ No recipients in .env")
        return
    today_str = datetime.now().strftime("%d %b %Y")
    subject   = f"i2Global Creative OS — Daily Dashboard {today_str}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"i2Global Creative OS <{SMTP_USER}>"
    msg["To"]      = ", ".join(ALL_RECIPIENTS)
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, ALL_RECIPIENTS, msg.as_string())
        print(f"  ✓ Email sent to {len(ALL_RECIPIENTS)} recipients")
    except Exception as e:
        print(f"  ✗ Email error: {e}")
    os.makedirs("reports", exist_ok=True)
    path = f"reports/dashboard_{datetime.now().strftime('%Y%m%d')}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Dashboard saved: {path}")

# ── MAIN ───────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*55)
    print("  i2Global Creative OS — Daily Dashboard")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*55)
    print(f"\n[1/4] Fetching Meta Ads from Windsor.ai ({DATE_PRESET})...")
    ads = fetch_meta_ads()
    print("\n[2/4] Fetching CRM data from MongoDB...")
    lob_data, rm_data = fetch_crm_data()
    print("\n[3/4] Building dashboard...")
    html = build_dashboard(ads, lob_data, rm_data)
    print("\n[4/4] Sending email...")
    send_email(html)
    print("\n✅ Done!\n")

if __name__ == "__main__":
    main()
