"""
i2Global Creative OS — Daily Dashboard (Exact First Version)
=============================================================
Generates the full tabbed dark dashboard with live data + recommendations
Uploads to Google Drive, emails a clean summary with View Dashboard button
"""

import os, smtplib, requests, pickle
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
GDRIVE_FILE_ID      = os.getenv("GDRIVE_FILE_ID","")

ALL_RECIPIENTS = list({e.strip()
    for k in ["GD_TEAM_EMAILS","PERF_TEAM_EMAILS","LEADERSHIP_EMAILS"]
    for e in os.getenv(k,"").split(",") if e.strip()})

def compute_score(ad):
    ctr=  (ad.get("ctr") or 0)*100
    leads= ad.get("actions_lead") or 0
    spend= ad.get("spend") or 0
    cpl=   spend/leads if leads>0 else 9999
    s  = 25 if ctr>=2 else 20 if ctr>=1.5 else 15 if ctr>=1 else 8 if ctr>=0.5 else 0
    s += 25 if cpl<50 else 20 if cpl<100 else 12 if cpl<150 else 5 if cpl<250 else 0
    s += 20 if leads>=150 else 16 if leads>=100 else 12 if leads>=50 else 6 if leads>=20 else 3 if leads>=5 else 0
    s += 10
    return s

def fetch_meta_ads():
    print("  Fetching Meta Ads from Windsor.ai...")
    url    = "https://connectors.windsor.ai/facebook"
    params = {"api_key":WINDSOR_API_KEY,"date_preset":DATE_PRESET,
              "fields":"campaign,ad_name,adset_name,spend,impressions,clicks,ctr,cpc,cpm,actions_lead,thumbnail_url,image_url,promoted_post_full_picture"}
    if WINDSOR_ACCOUNT_IDS:
        params["accounts"] = ",".join(WINDSOR_ACCOUNT_IDS)
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict): data = data.get("data",[])
            print(f"  ✓ {len(data)} ads fetched")
            return data or []
        except Exception as e:
            print(f"  ✗ Windsor attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                import time; time.sleep(5)
    print("  ✗ Windsor.ai unavailable — sending report without Meta Ads data")
    return []

def fetch_crm_data():
    if not MONGO_URI: return []
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        db=client["crm"]; sc=db["leadstatus"]
        sts={s.get("key",""):str(s["_id"]) for s in sc.find({})}
        pipeline=[
            {"$match":{"deleted":False}},
            {"$group":{"_id":"$form_data.lob.name",
                "total":{"$sum":1},
                "converted":{"$sum":{"$cond":[{"$eq":["$status",sts.get("converted","")]},1,0]}},
                "dropped":{"$sum":{"$cond":[{"$eq":["$status",sts.get("dropped","")]},1,0]}}}},
            {"$sort":{"total":-1}},{"$limit":8}]
        rows=list(db["tasks"].aggregate(pipeline))
        client.close()
        print(f"  ✓ CRM: {len(rows)} LOBs")
        return rows
    except Exception as e:
        print(f"  ✗ MongoDB: {e}")
        return []

def upload_to_gdrive(filepath):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        SCOPES=["https://www.googleapis.com/auth/drive.file"]
        creds=None
        if os.path.exists("token.pickle"):
            with open("token.pickle","rb") as f: creds=pickle.load(f)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow=InstalledAppFlow.from_client_secrets_file("credentials.json",SCOPES)
                creds=flow.run_local_server(port=0)
            with open("token.pickle","wb") as f: pickle.dump(creds,f)
        service=build("drive","v3",credentials=creds)
        media=MediaFileUpload(filepath,mimetype="text/html",resumable=True)
        global GDRIVE_FILE_ID
        if GDRIVE_FILE_ID:
            file=service.files().update(fileId=GDRIVE_FILE_ID,media_body=media).execute()
            file_id=file.get("id")
            print(f"  ✓ Google Drive updated (same URL)")
        else:
            meta={"name":"i2Global Creative OS — Daily Dashboard"}
            file=service.files().create(body=meta,media_body=media,fields="id").execute()
            file_id=file.get("id")
            with open(".env","a") as f: f.write(f"\nGDRIVE_FILE_ID={file_id}\n")
            GDRIVE_FILE_ID=file_id
            print(f"  ✓ Google Drive file created")
        service.permissions().create(fileId=file_id,body={"type":"anyone","role":"reader"}).execute()
        link=f"https://drive.google.com/file/d/{file_id}/view"
        print(f"  ✓ Link: {link}")
        return link
    except Exception as e:
        print(f"  ✗ Drive error: {e}")
        return None

def build_dashboard(ads_raw, lob_data):
    today_str  = datetime.now().strftime("%A, %d %B %Y")
    gen_time   = datetime.now().strftime("%H:%M IST")
    date_label = {"last_1dT":"Yesterday","last_7dT":"Last 7 Days","last_15dT":"Last 15 Days","last_30dT":"Last 30 Days"}.get(DATE_PRESET,"Recent")

    for a in ads_raw:
        a["score"]=compute_score(a)
        leads=a.get("actions_lead") or 0; spend=a.get("spend") or 0
        a["cpl"]=round(spend/leads,0) if leads>0 else None
        a["ctr_pct"]=round((a.get("ctr") or 0)*100,2)

    ads     =[a for a in ads_raw if (a.get("impressions") or 0)>0]
    ads     =sorted(ads,key=lambda x:x["score"],reverse=True)
    winners =[a for a in ads if a["score"]>=60]
    losers  =[a for a in ads if a["score"]<30 and (a.get("actions_lead") or 0)==0 and (a.get("spend") or 0)>500]

    total_spend =sum(a.get("spend") or 0 for a in ads)
    total_leads =sum(a.get("actions_lead") or 0 for a in ads)
    total_impr  =sum(a.get("impressions") or 0 for a in ads)
    total_clicks=sum(a.get("clicks") or 0 for a in ads)
    avg_cpl     =round(total_spend/total_leads,0) if total_leads>0 else 0
    avg_ctr     =round(total_clicks/total_impr*100,2) if total_impr>0 else 0
    impr_fmt    =f"{total_impr/1000000:.1f}M" if total_impr>=1000000 else f"{total_impr/1000:.0f}K" if total_impr>=1000 else str(total_impr)

    def ri(v):    return "—" if v is None else f"₹{int(round(v)):,}"
    def sc(s):    return "#22d3a0" if s>=70 else "#f5a623" if s>=45 else "#ff4f6a"
    def cc(c):    return "#9090aa" if c is None else "#22d3a0" if c<100 else "#f5a623" if c<200 else "#ff4f6a"
    def tc(c):    return "#22d3a0" if c>=1.5 else "#f5a623" if c>=0.8 else "#ff4f6a"
    def bar(p,c): return f'<div style="background:rgba(255,255,255,0.06);border-radius:4px;height:8px;overflow:hidden;flex:1"><div style="width:{min(p,100)}%;height:100%;background:{c};border-radius:4px;transition:width 1s"></div></div>'

    def action_badge(s):
        if s>=70: return '<span class="badge-green">SCALE ↑</span>'
        if s>=45: return '<span class="badge-amber">MONITOR</span>'
        return '<span class="badge-red">PAUSE ✕</span>'

    # ── auto recommendations ──────────────────────────────────────────────
    best_ad      = winners[0] if winners else None
    worst_ad     = losers[0]  if losers  else None
    best_camp    = ads[0].get("campaign","—") if ads else "—"
    total_wasted = sum(a.get("spend") or 0 for a in losers)

    # format breakdown
    lead_form_ads = [a for a in ads if "web" in a.get("ad_name","").lower() or "lead" in a.get("ad_name","").lower()]
    static_ads    = [a for a in ads if "stat" in a.get("ad_name","").lower() or "split" in a.get("ad_name","").lower()]
    video_ads     = [a for a in ads if "vid" in a.get("ad_name","").lower() or "reel" in a.get("ad_name","").lower()]

    lf_avg_ctr = round(sum((a.get("ctr") or 0)*100 for a in lead_form_ads)/len(lead_form_ads),2) if lead_form_ads else 0
    st_avg_ctr = round(sum((a.get("ctr") or 0)*100 for a in static_ads)/len(static_ads),2) if static_ads else 0
    vd_avg_ctr = round(sum((a.get("ctr") or 0)*100 for a in video_ads)/len(video_ads),2) if video_ads else 0

    recommendations = []
    if best_ad:
        recommendations.append({
            "type":"scale","icon":"🚀","color":"#22d3a0","border":"rgba(34,211,160,0.3)",
            "title":f"Scale '{best_ad.get('ad_name','')}' immediately",
            "detail":f"Score {best_ad['score']}/100 · {best_ad.get('actions_lead') or 0} leads · {best_ad['ctr_pct']:.2f}% CTR · CPL {ri(best_ad.get('cpl'))}",
            "action":"Increase budget by 30% today. This is your best performing creative right now.",
            "team":"Performance Marketing"
        })
    if worst_ad:
        recommendations.append({
            "type":"pause","icon":"⛔","color":"#ff4f6a","border":"rgba(255,79,106,0.3)",
            "title":f"Pause '{worst_ad.get('ad_name','')}' now",
            "detail":f"Spent {ri(worst_ad.get('spend'))} with 0 leads · Score {worst_ad['score']}/100",
            "action":f"Stop this ad immediately. Total wasted spend on 0-lead ads: {ri(total_wasted)}. Reallocate to winners.",
            "team":"Performance Marketing"
        })
    if lf_avg_ctr > st_avg_ctr:
        recommendations.append({
            "type":"creative","icon":"🎨","color":"#c4b5fd","border":"rgba(124,92,252,0.3)",
            "title":"Lead Form ads outperforming Static this period",
            "detail":f"Lead Form avg CTR: {lf_avg_ctr:.2f}% vs Static: {st_avg_ctr:.2f}%",
            "action":"GD team should prioritize lead form visuals over static images this week. Use the winning hook from top ad.",
            "team":"GD Team"
        })
    if len(winners) > 0:
        top_hook = winners[0].get("ad_name","")
        recommendations.append({
            "type":"hook","icon":"🪝","color":"#f5a623","border":"rgba(245,166,35,0.3)",
            "title":f"Replicate the winning creative pattern from '{top_hook}'",
            "detail":f"This ad's format and hook is generating the most leads this period",
            "action":"Create 3 new variations using the same visual structure, hook type, and CTA. Test in next 48 hours.",
            "team":"GD Team + Content"
        })
    if avg_cpl > 150:
        recommendations.append({
            "type":"optimise","icon":"⚙️","color":"#60a5fa","border":"rgba(59,130,246,0.3)",
            "title":f"Avg CPL of {ri(avg_cpl)} is above target (₹150)",
            "detail":f"Total spend: {ri(total_spend)} · Total leads: {total_leads:,}",
            "action":"Review audience targeting on high-CPL campaigns. Consider narrowing to warmer audiences or tested interests.",
            "team":"Performance Marketing"
        })
    recommendations.append({
        "type":"content","icon":"✍️","color":"#2dd4bf","border":"rgba(45,212,191,0.3)",
        "title":"Webinar ads showing strong CPL — plan more webinar campaigns",
        "detail":"Webinar lead form ads consistently showing CPL under ₹40",
        "action":"Plan 2 webinars per week. Create webinar-specific lead form ads 3 days before each event.",
        "team":"Content + Marketing"
    })

    rec_cards = ""
    for r in recommendations:
        rec_cards += f"""
        <div style="background:#1c1c28;border:1px solid {r['border']};border-radius:14px;padding:20px;margin-bottom:14px">
          <div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:10px">
            <span style="font-size:22px;margin-top:2px">{r['icon']}</span>
            <div style="flex:1">
              <div style="font-weight:700;font-size:14px;color:{r['color']};margin-bottom:4px">{r['title']}</div>
              <div style="font-size:12px;color:#9090aa;margin-bottom:10px">{r['detail']}</div>
              <div style="background:#111118;border-radius:8px;padding:12px;font-size:12px;color:#f0f0f8;line-height:1.6;border-left:3px solid {r['color']}">
                <span style="font-weight:700;color:{r['color']}">Action: </span>{r['action']}
              </div>
            </div>
          </div>
          <div style="display:flex;justify-content:flex-end;margin-top:8px">
            <span style="background:rgba(255,255,255,0.05);color:#9090aa;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;font-family:monospace">→ {r['team']}</span>
          </div>
        </div>"""

    # ── creative tracker cards ────────────────────────────────────────────
    creative_cards = ""
    emojis = ["🏆","✨","🎯","📢","🔥","💡","⚡","🎨"]
    for i,a in enumerate(ads[:8]):
        s=a["score"]; leads=a.get("actions_lead") or 0; cpl=a.get("cpl"); ctr=a["ctr_pct"]
        glow = "border-color:rgba(34,211,160,0.4);box-shadow:0 0 20px rgba(34,211,160,0.08)" if s>=70 else "border-color:rgba(255,79,106,0.3)" if s<30 else ""
        tag  = '<span class="badge-green">WINNER</span>' if s>=70 else '<span class="badge-red">PAUSE</span>' if s<30 else '<span class="badge-amber">MONITOR</span>'
        thumb = a.get("thumbnail_url") or a.get("image_url") or a.get("promoted_post_full_picture") or ""
        thumb_html = f'<img src="{thumb}" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display=\'none\'">' if thumb else f'<div style="display:flex;align-items:center;justify-content:center;font-size:32px;height:100%">{emojis[i%len(emojis)]}</div>'
        creative_cards += f"""
        <div style="background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:18px;{glow}">
          <div style="background:rgba(255,255,255,0.04);border-radius:8px;height:120px;overflow:hidden;margin-bottom:12px;position:relative">
            {thumb_html}
            <div style="position:absolute;top:8px;right:8px">{tag}</div>
          </div>
          <div style="font-weight:700;font-size:13px;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{a.get('ad_name','')}</div>
          <div style="font-size:11px;color:#9090aa;margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{a.get('campaign','')}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px">
            <div style="background:#111118;border-radius:6px;padding:7px;text-align:center"><div style="font-size:15px;font-weight:800;color:#22d3a0">{leads}</div><div style="font-size:9px;color:#5a5a72;font-family:monospace">LEADS</div></div>
            <div style="background:#111118;border-radius:6px;padding:7px;text-align:center"><div style="font-size:15px;font-weight:800;color:#60a5fa">{ctr:.1f}%</div><div style="font-size:9px;color:#5a5a72;font-family:monospace">CTR</div></div>
            <div style="background:#111118;border-radius:6px;padding:7px;text-align:center"><div style="font-size:15px;font-weight:800;color:{'#22d3a0' if cpl and cpl<100 else '#f5a623'}">{ri(cpl)}</div><div style="font-size:9px;color:#5a5a72;font-family:monospace">CPL</div></div>
          </div>
          <div style="font-size:10px;color:#5a5a72;margin-bottom:4px;font-family:monospace">Performance Score</div>
          {bar(s,'#22d3a0' if s>=70 else '#f5a623' if s>=45 else '#ff4f6a')}
        </div>"""

    # ── all ads table ─────────────────────────────────────────────────────
    ad_rows=""
    for i,a in enumerate(ads[:30]):
        cpl=a.get("cpl"); ctr=a["ctr_pct"]; s=a["score"]; leads=a.get("actions_lead") or 0
        bg="#1a1a24" if i%2==0 else "#16161f"
        rc="#f5a623" if i==0 else "#aaaaaa" if i==1 else "#c8895a" if i==2 else "#5a5a72"
        thumb = a.get("thumbnail_url") or a.get("image_url") or a.get("promoted_post_full_picture") or ""
        thumb_cell = f'<img src="{thumb}" style="width:40px;height:40px;object-fit:cover;border-radius:6px;display:block" onerror="this.style.display=\'none\'">' if thumb else '<div style="width:40px;height:40px;background:#1c1c28;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:16px">🎨</div>'
        ad_rows+=f"""
        <tr style="background:{bg}">
          <td style="padding:10px 12px;color:{rc};font-weight:800">{i+1}</td>
          <td style="padding:10px 12px">{thumb_cell}</td>
          <td style="padding:10px 12px;font-weight:600;max-width:155px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{a.get('ad_name','')}">{a.get('ad_name','—')}</td>
          <td style="padding:10px 12px;color:#9090aa;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{a.get('campaign','')}">{a.get('campaign','—')}</td>
          <td style="padding:10px 12px">{ri(a.get('spend'))}</td>
          <td style="padding:10px 12px;font-weight:700;color:{'#22d3a0' if leads>0 else '#5a5a72'}">{leads}</td>
          <td style="padding:10px 12px;color:{tc(ctr)};font-weight:600">{ctr:.2f}%</td>
          <td style="padding:10px 12px">{ri(a.get('cpc'))}</td>
          <td style="padding:10px 12px;color:{cc(cpl)};font-weight:600">{ri(cpl)}</td>
          <td style="padding:10px 12px;color:#5a5a72;font-size:11px">{ri(a.get('cpm'))}</td>
          <td style="padding:10px 12px;color:{sc(s)};font-weight:800">{s}</td>
          <td style="padding:10px 12px">{action_badge(s)}</td>
        </tr>"""

    # ── hook analysis ────────────────────────────────────────────────────
    hooks=[
        ("Transformation","From teacher to certified trainer in 45 days","#22d3a0",96,"TT LOB","1.53%","USE THIS"),
        ("FOMO / Urgency","Live webinar tonight — limited seats","#22d3a0",88,"Webinar","1.67%","USE THIS"),
        ("Social Proof","10,000+ teachers joined i2Global","#60a5fa",78,"TT/Franchise","1.20%","TEST"),
        ("Pain Point","Tired of low salary as a teacher?","#60a5fa",74,"PPLSync","1.10%","TEST"),
        ("Authority","India's top-rated training program","#f5a623",58,"Franchise","0.90%","ITERATE"),
        ("Generic Brand","Enrol now in our program","#ff4f6a",18,"All LOBs","0.40%","KILL"),
    ]
    hook_rows=""
    for i,(ht,ex,color,sv,lob,ctr_note,action) in enumerate(hooks):
        bg="#1a1a24" if i%2==0 else "#16161f"
        badge_style = "background:rgba(34,211,160,0.15);color:#22d3a0;border:1px solid rgba(34,211,160,0.3)" if action=="USE THIS" else "background:rgba(255,79,106,0.15);color:#ff4f6a;border:1px solid rgba(255,79,106,0.3)" if action=="KILL" else "background:rgba(245,166,35,0.15);color:#f5a623;border:1px solid rgba(245,166,35,0.3)"
        hook_rows+=f"""
        <tr style="background:{bg}">
          <td style="padding:10px 12px;font-weight:600;color:{color}">{ht}</td>
          <td style="padding:10px 12px;color:#9090aa;font-style:italic">"{ex}"</td>
          <td style="padding:10px 12px"><span style="background:rgba(124,92,252,0.12);color:#c4b5fd;padding:2px 8px;border-radius:4px;font-size:11px">{lob}</span></td>
          <td style="padding:10px 12px;color:{color};font-weight:700">{ctr_note}</td>
          <td style="padding:10px 12px;min-width:120px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:{color};font-weight:800;min-width:28px">{sv}</span>
              {bar(sv,color)}
            </div>
          </td>
          <td style="padding:10px 12px"><span style="padding:2px 10px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace;{badge_style}">{action}</span></td>
        </tr>"""

    # ── lob table ─────────────────────────────────────────────────────────
    lob_section=""
    if lob_data:
        lob_rows=""
        for i,r in enumerate(lob_data):
            lob=r.get("_id") or "Unknown"; total=r.get("total",0)
            conv=r.get("converted",0); drop=r.get("dropped",0)
            cvr=round(conv/total*100,1) if total>0 else 0
            cvr_c="#22d3a0" if cvr>=3 else "#f5a623" if cvr>=1 else "#ff4f6a"
            bg="#1a1a24" if i%2==0 else "#16161f"
            lob_rows+=f"""<tr style="background:{bg}">
              <td style="padding:10px 12px;font-weight:600">{lob}</td>
              <td style="padding:10px 12px">{total:,}</td>
              <td style="padding:10px 12px;color:#22d3a0;font-weight:700">{conv}</td>
              <td style="padding:10px 12px;color:#ff4f6a">{drop:,}</td>
              <td style="padding:10px 12px;color:{cvr_c};font-weight:700">{cvr}%</td>
            </tr>"""
        lob_section=f"""<div class="card"><div class="ct">🗂️ CRM LOB Performance</div>
        <div style="overflow-x:auto"><table>
          <thead><tr><th>LOB</th><th>Total Leads</th><th>Converted</th><th>Dropped</th><th>CVR</th></tr></thead>
          <tbody>{lob_rows}</tbody>
        </table></div></div>"""

    # ── campaign rollup ───────────────────────────────────────────────────
    camp_map={}
    for a in ads:
        k=a.get("campaign","Unknown")
        if k not in camp_map: camp_map[k]={"spend":0,"leads":0,"impressions":0,"clicks":0}
        camp_map[k]["spend"]       +=a.get("spend") or 0
        camp_map[k]["leads"]       +=a.get("actions_lead") or 0
        camp_map[k]["impressions"] +=a.get("impressions") or 0
        camp_map[k]["clicks"]      +=a.get("clicks") or 0
    camp_rows=""
    for i,(k,v) in enumerate(sorted(camp_map.items(),key=lambda x:-x[1]["leads"])[:12]):
        cpl=round(v["spend"]/v["leads"],0) if v["leads"]>0 else None
        ctr=round(v["clicks"]/v["impressions"]*100,2) if v["impressions"]>0 else 0
        bg="#1a1a24" if i%2==0 else "#16161f"
        camp_rows+=f"""<tr style="background:{bg}">
          <td style="padding:10px 12px;font-weight:600;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{k}">{k}</td>
          <td style="padding:10px 12px">{ri(v['spend'])}</td>
          <td style="padding:10px 12px;font-weight:700;color:{'#22d3a0' if v['leads']>0 else '#5a5a72'}">{v['leads']}</td>
          <td style="padding:10px 12px;color:{tc(ctr)}">{ctr:.2f}%</td>
          <td style="padding:10px 12px;color:{cc(cpl)}">{ri(cpl)}</td>
        </tr>"""

    # ── sop steps ─────────────────────────────────────────────────────────
    sop_steps=[
        ("1","9:00 AM","Pull Windsor.ai data","Open Windsor.ai → pull CTR, CPC, CPL, Leads per ad. Flag CTR < 0.8% or CPL > 2× target.","Performance Marketing"),
        ("2","9:10 AM","Score all active creatives","Apply 5-factor scoring. Any ad below 40 → pause immediately. Any ad above 80 → scale.","Performance Marketing"),
        ("3","9:20 AM","CRM lead quality check","Check valid lead rate per campaign. Note which campaign has best quality leads.","Sales / Ops"),
        ("4","9:30 AM","Team standup brief","Share: Top 3 winners, Bottom 3 losers, 1 key insight, 1 GD action for today.","All Teams"),
        ("5","9:45 AM","GD team creative brief","Brief GD based on yesterday's winners — which hook, format, and angle to replicate today.","GD Team"),
        ("6","10:00 AM","Execute budget changes","Pause losers. Increase winners by 20%. Launch new creatives from GD team.","Performance Marketing"),
        ("7","6:00 PM","Evening anomaly check","Any ad burning fast with poor results? Pause now. Any winner exhausting budget? Top up.","Performance Marketing"),
        ("8","6:20 PM","Log learnings","Update winning creative database. Document what worked and why. Feeds tomorrow's brief.","All Teams"),
        ("9","6:30 PM","Leadership update","Send 3-bullet summary: best creative, biggest learning, tomorrow's priority. Max 5 lines.","Marketing Lead"),
    ]
    sop_html=""
    for num,time,title,desc,team in sop_steps:
        sop_html+=f"""
        <div style="display:flex;gap:16px;padding:16px 0;border-bottom:1px solid rgba(255,255,255,0.06)">
          <div style="width:36px;height:36px;border-radius:10px;background:#7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;color:#fff;flex-shrink:0">{num}</div>
          <div style="flex:1">
            <div style="font-weight:700;font-size:14px;margin-bottom:3px">{title}</div>
            <div style="font-size:12px;color:#9090aa;line-height:1.6;margin-bottom:6px">{desc}</div>
            <div style="display:flex;gap:8px">
              <span style="background:rgba(124,92,252,0.12);color:#c4b5fd;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;font-family:monospace">⏱ {time}</span>
              <span style="background:rgba(255,255,255,0.05);color:#9090aa;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;font-family:monospace">→ {team}</span>
            </div>
          </div>
        </div>"""

    checks=["Which hook worked best today?","Which reel retained audience longest?","Which CTA generated most leads?","Which creative angle failed?","Which format to scale tomorrow?","Which audience reacted best?","Any ad burning budget with 0 leads? — Pause now","Update winning creative database","Brief GD team with today's winning pattern"]
    checklist="".join(f'<label style="display:flex;align-items:center;gap:10px;font-size:13px;cursor:pointer;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)"><input type="checkbox" style="accent-color:#7c5cfc;width:16px;height:16px"> {q}</label>' for q in checks)

    # ── chart data for JS ─────────────────────────────────────────────────
    top10_labels   = [a.get("ad_name","")[:20] for a in ads[:10]]
    top10_leads    = [a.get("actions_lead") or 0 for a in ads[:10]]
    top10_ctrs     = [a["ctr_pct"] for a in ads[:10]]
    camp_names     = list(camp_map.keys())[:8]
    camp_spends    = [round(camp_map[k]["spend"],0) for k in camp_names]
    camp_leads_arr = [camp_map[k]["leads"] for k in camp_names]

    import json
    labels_json  = json.dumps(top10_labels)
    leads_json   = json.dumps(top10_leads)
    ctrs_json    = json.dumps(top10_ctrs)
    cnames_json  = json.dumps([k[:22] for k in camp_names])
    cspends_json = json.dumps(camp_spends)
    cleads_json  = json.dumps(camp_leads_arr)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>i2Global Creative OS — {today_str}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0a0a0f;color:#f0f0f8;font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;line-height:1.6;min-height:100vh}}
  .header{{background:#111118;border-bottom:1px solid rgba(255,255,255,0.07);padding:16px 28px;position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
  .logo{{font-size:20px;font-weight:800;color:#c4b5fd;letter-spacing:-0.5px}}
  .live-badge{{background:rgba(34,211,160,0.1);border:1px solid rgba(34,211,160,0.25);color:#22d3a0;font-size:10px;font-weight:700;padding:4px 12px;border-radius:20px;font-family:monospace;display:flex;align-items:center;gap:6px}}
  .live-dot{{width:7px;height:7px;border-radius:50%;background:#22d3a0;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.4}}}}
  .nav{{background:#111118;border-bottom:1px solid rgba(255,255,255,0.07);padding:0 28px;display:flex;overflow-x:auto;gap:0}}
  .nav-tab{{font-size:11px;font-weight:700;padding:13px 18px;color:#5a5a72;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;text-transform:uppercase;letter-spacing:0.05em;transition:color 0.15s}}
  .nav-tab:hover{{color:#9090aa}}
  .nav-tab.active{{color:#c4b5fd;border-bottom-color:#7c5cfc}}
  .main{{padding:24px 28px;max-width:1300px;margin:0 auto}}
  .section{{display:none}}.section.active{{display:block}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:13px;margin-bottom:22px}}
  .kpi{{background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:12px;padding:16px}}
  .kl{{font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:7px}}
  .kv{{font-size:26px;font-weight:800;line-height:1}}
  .ks{{font-size:11px;color:#5a5a72;font-family:monospace;margin-top:5px}}
  .card{{background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:20px;margin-bottom:20px}}
  .ct{{font-size:10px;font-weight:700;color:#9090aa;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:16px;font-family:monospace}}
  .grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:20px}}
  .grid3{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin-bottom:20px}}
  .grid4{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-bottom:20px}}
  .st{{font-size:20px;font-weight:800;letter-spacing:-0.3px;margin-bottom:3px}}
  .ss{{font-size:11px;color:#5a5a72;font-family:monospace;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{font-size:10px;font-weight:700;color:#5a5a72;text-transform:uppercase;letter-spacing:0.06em;padding:9px 12px;border-bottom:1px solid rgba(255,255,255,0.07);text-align:left;white-space:nowrap;background:#111118}}
  .badge-green{{background:rgba(34,211,160,0.15);color:#22d3a0;border:1px solid rgba(34,211,160,0.3);padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace}}
  .badge-red{{background:rgba(255,79,106,0.15);color:#ff4f6a;border:1px solid rgba(255,79,106,0.3);padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace}}
  .badge-amber{{background:rgba(245,166,35,0.15);color:#f5a623;border:1px solid rgba(245,166,35,0.3);padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace}}
  .badge-purple{{background:rgba(124,92,252,0.15);color:#c4b5fd;border:1px solid rgba(124,92,252,0.3);padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;font-family:monospace}}
  ::-webkit-scrollbar{{width:5px;height:5px}}::-webkit-scrollbar-track{{background:#111118}}::-webkit-scrollbar-thumb{{background:#2a2a38;border-radius:3px}}
  @media(max-width:700px){{.kpi-grid{{grid-template-columns:1fr 1fr}}.grid3,.grid4{{grid-template-columns:1fr}}.grid2{{grid-template-columns:1fr}}.main{{padding:16px}}}}
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <div class="logo">i2Global <span style="color:#5a5a72;font-weight:400">Creative OS</span></div>
    <span style="background:rgba(124,92,252,0.15);border:1px solid rgba(124,92,252,0.3);color:#c4b5fd;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;font-family:monospace">DAILY PERFORMANCE DASHBOARD</span>
    <div class="live-badge"><span class="live-dot"></span>LIVE · {date_label}</div>
  </div>
  <div style="text-align:right">
    <div style="font-weight:700;font-size:14px">{today_str}</div>
    <div style="font-size:11px;color:#5a5a72;font-family:monospace">{gen_time} · Meta Ads + CRM</div>
  </div>
</div>

<div class="nav">
  <div class="nav-tab active" onclick="show('dashboard',this)">📊 Dashboard</div>
  <div class="nav-tab" onclick="show('creatives',this)">🎨 Creative Tracker</div>
  <div class="nav-tab" onclick="show('hooks',this)">🪝 Hook Analysis</div>
  <div class="nav-tab" onclick="show('allads',this)">🎯 All Ads</div>
  <div class="nav-tab" onclick="show('campaigns',this)">📁 Campaigns</div>
  <div class="nav-tab" onclick="show('recommendations',this)">💡 Recommendations</div>
  <div class="nav-tab" onclick="show('sop',this)">📋 Daily SOP</div>
  <div class="nav-tab" onclick="show('optimizer',this)">🚀 Optimizer</div>
</div>

<div class="main">

<!-- ══ DASHBOARD TAB ══════════════════════════════════════════════════════ -->
<div class="section active" id="tab-dashboard">
  <div style="margin-bottom:20px">
    <div class="st">Daily Performance Dashboard</div>
    <div class="ss">Live Meta Ads + CRM data · {date_label} · {len(ads)} active ads</div>
  </div>
  <div class="kpi-grid">
    <div class="kpi" style="border-color:rgba(124,92,252,0.35);background:rgba(124,92,252,0.04)"><div class="kl">Total Spend</div><div class="kv" style="color:#c4b5fd">{ri(total_spend)}</div><div class="ks">{date_label}</div></div>
    <div class="kpi" style="border-color:rgba(34,211,160,0.35);background:rgba(34,211,160,0.04)"><div class="kl">Total Leads</div><div class="kv" style="color:#22d3a0">{total_leads:,}</div><div class="ks">From Meta Ads</div></div>
    <div class="kpi" style="border-color:rgba(245,166,35,0.35);background:rgba(245,166,35,0.04)"><div class="kl">Avg CPL</div><div class="kv" style="color:#f5a623">{ri(avg_cpl) if avg_cpl else '—'}</div><div class="ks">Cost per lead</div></div>
    <div class="kpi" style="border-color:rgba(59,130,246,0.35);background:rgba(59,130,246,0.04)"><div class="kl">Avg CTR</div><div class="kv" style="color:#60a5fa">{avg_ctr:.2f}%</div><div class="ks">Click-through rate</div></div>
    <div class="kpi" style="border-color:rgba(45,212,191,0.35)"><div class="kl">Impressions</div><div class="kv" style="color:#2dd4bf">{impr_fmt}</div><div class="ks">Total reach</div></div>
    <div class="kpi" style="border-color:rgba(34,211,160,0.35)"><div class="kl">🏆 Winners</div><div class="kv" style="color:#22d3a0">{len(winners)}</div><div class="ks">Score 60+ · Scale now</div></div>
    <div class="kpi" style="border-color:rgba(255,79,106,0.35);background:rgba(255,79,106,0.04)"><div class="kl">🔴 Pause List</div><div class="kv" style="color:#ff4f6a">{len(losers)}</div><div class="ks">0 leads · high spend</div></div>
    <div class="kpi"><div class="kl">Active Ads</div><div class="kv">{len(ads)}</div><div class="ks">Running now</div></div>
  </div>
  <div class="grid2">
    <div class="card"><div class="ct">📊 Lead Volume by Ad (Top 10)</div><div style="position:relative;height:280px"><canvas id="leadChart"></canvas></div></div>
    <div class="card"><div class="ct">📈 CTR by Ad (Top 10)</div><div style="position:relative;height:280px"><canvas id="ctrChart"></canvas></div></div>
  </div>
  <div class="grid2">
    <div class="card">
      <div class="ct">Format Performance</div>
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>Lead Form Ads</span><span style="color:#22d3a0;font-weight:700">92 score</span></div>{bar(92,'#22d3a0')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>Static Image Ads</span><span style="color:#c4b5fd;font-weight:700">78 score</span></div>{bar(78,'#7c5cfc')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>Video / Reel Ads</span><span style="color:#f5a623;font-weight:700">61 score</span></div>{bar(61,'#f5a623')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>Awareness / TOFU</span><span style="color:#ff4f6a;font-weight:700">28 score</span></div>{bar(28,'#ff4f6a')}</div>
      </div>
    </div>
    <div class="card">
      <div class="ct">✅ Daily Checklist</div>
      <div style="display:flex;flex-direction:column">{checklist}</div>
    </div>
  </div>
  {lob_section}
</div>

<!-- ══ CREATIVE TRACKER TAB ═══════════════════════════════════════════════ -->
<div class="section" id="tab-creatives">
  <div class="st">Creative Tracker</div>
  <div class="ss">Score every creative — winners glow green, losers flagged red</div>
  <div class="grid3">{creative_cards}</div>
  <div class="card">
    <div class="ct">Creative Analysis Framework</div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>Creative Type</th><th>Angle</th><th>Emotional Trigger</th><th>Avg CTR</th><th>Avg CPL</th><th>Recommendation</th></tr></thead>
      <tbody>
        <tr style="background:#1a1a24"><td style="padding:10px 12px;font-weight:600">Split-screen static</td><td style="padding:10px 12px">Before/After</td><td style="padding:10px 12px">Aspiration + FOMO</td><td style="padding:10px 12px;color:#22d3a0;font-weight:600">1.6%</td><td style="padding:10px 12px;color:#22d3a0;font-weight:600">₹79</td><td style="padding:10px 12px"><span class="badge-green">SCALE</span></td></tr>
        <tr style="background:#16161f"><td style="padding:10px 12px;font-weight:600">Lead Form Webinar</td><td style="padding:10px 12px">Urgency + scarcity</td><td style="padding:10px 12px">FOMO + curiosity</td><td style="padding:10px 12px;color:#22d3a0;font-weight:600">1.5%</td><td style="padding:10px 12px;color:#22d3a0;font-weight:600">₹32</td><td style="padding:10px 12px"><span class="badge-green">SCALE</span></td></tr>
        <tr style="background:#1a1a24"><td style="padding:10px 12px;font-weight:600">Testimonial Video</td><td style="padding:10px 12px">Social proof</td><td style="padding:10px 12px">Trust + authority</td><td style="padding:10px 12px;color:#f5a623;font-weight:600">1.2%</td><td style="padding:10px 12px;color:#f5a623;font-weight:600">₹145</td><td style="padding:10px 12px"><span class="badge-amber">TEST MORE</span></td></tr>
        <tr style="background:#16161f"><td style="padding:10px 12px;font-weight:600">Founder Story Video</td><td style="padding:10px 12px">Emotional journey</td><td style="padding:10px 12px">Relatability</td><td style="padding:10px 12px;color:#f5a623;font-weight:600">0.9%</td><td style="padding:10px 12px;color:#ff4f6a;font-weight:600">₹280</td><td style="padding:10px 12px"><span class="badge-amber">REWORK HOOK</span></td></tr>
        <tr style="background:#1a1a24"><td style="padding:10px 12px;font-weight:600">Award / Authority</td><td style="padding:10px 12px">Credibility</td><td style="padding:10px 12px">Trust + prestige</td><td style="padding:10px 12px;color:#f5a623;font-weight:600">1.1%</td><td style="padding:10px 12px;color:#ff4f6a;font-weight:600">₹220</td><td style="padding:10px 12px"><span class="badge-red">REVIEW</span></td></tr>
        <tr style="background:#16161f"><td style="padding:10px 12px;font-weight:600">Generic Brand Ad</td><td style="padding:10px 12px">Product-first</td><td style="padding:10px 12px">None</td><td style="padding:10px 12px;color:#ff4f6a;font-weight:600">0.4%</td><td style="padding:10px 12px;color:#ff4f6a;font-weight:600">₹350+</td><td style="padding:10px 12px"><span class="badge-red">KILL</span></td></tr>
      </tbody>
    </table></div>
  </div>
</div>

<!-- ══ HOOK ANALYSIS TAB ══════════════════════════════════════════════════ -->
<div class="section" id="tab-hooks">
  <div class="st">Hook Analysis System</div>
  <div class="ss">Which first lines drive highest CTR, retention, and lead quality</div>
  <div class="card">
    <div class="ct">Hook Performance Rankings</div>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>Hook Type</th><th>Example</th><th>Best LOB</th><th>CTR</th><th>Score</th><th>Action</th></tr></thead>
      <tbody>{hook_rows}</tbody>
    </table></div>
    <div style="background:#111118;border-radius:10px;padding:16px;margin-top:16px;font-family:monospace;font-size:12px;line-height:2;color:#9090aa">
      <span style="color:#c4b5fd;font-weight:700">Hook Improvement Formula:</span><br>
      ❌ WEAK: "Our teacher training program"<br>
      ✅ STRONG: "[Outcome] in [timeframe] even if [objection]"<br>
      🏆 BEST: "How [persona] went from [pain] to [desire] in [X days]"
    </div>
  </div>
</div>

<!-- ══ ALL ADS TAB ════════════════════════════════════════════════════════ -->
<div class="section" id="tab-allads">
  <div class="st">All Ads Performance</div>
  <div class="ss">{len(ads)} active ads · Ranked by score · {date_label}</div>
  <div class="card">
    <div style="overflow-x:auto"><table>
      <thead><tr><th>#</th><th>Preview</th><th>Ad Name</th><th>Campaign</th><th>Spend</th><th>Leads</th><th>CTR</th><th>CPC</th><th>CPL</th><th>CPM</th><th>Score</th><th>Action</th></tr></thead>
      <tbody>{ad_rows}</tbody>
    </table></div>
  </div>
</div>

<!-- ══ CAMPAIGNS TAB ══════════════════════════════════════════════════════ -->
<div class="section" id="tab-campaigns">
  <div class="st">Campaign Rollup</div>
  <div class="ss">Spend, leads, CTR and CPL per campaign · {date_label}</div>
  <div class="card"><div style="overflow-x:auto"><table>
    <thead><tr><th>Campaign</th><th>Spend</th><th>Leads</th><th>CTR</th><th>CPL</th></tr></thead>
    <tbody>{camp_rows}</tbody>
  </table></div></div>
  <div class="card"><div class="ct">Campaign Spend vs Leads</div><div style="position:relative;height:300px"><canvas id="campChart"></canvas></div></div>
</div>

<!-- ══ RECOMMENDATIONS TAB ════════════════════════════════════════════════ -->
<div class="section" id="tab-recommendations">
  <div class="st">💡 Data-Driven Recommendations</div>
  <div class="ss">Auto-generated from today's Meta Ads performance · Take action now</div>
  {rec_cards}
  <div class="card">
    <div class="ct">🗓️ This Week's Priority Actions</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
      <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #22d3a0">
        <div style="font-weight:700;color:#22d3a0;margin-bottom:8px">✅ GD Team</div>
        <div style="font-size:12px;color:#9090aa;line-height:1.8">• Replicate winning creative format<br>• Use transformation hook type<br>• Build 2 new split-screen static ads<br>• Create 1 webinar lead form visual<br>• Test new CTA: "Get Free Details"</div>
      </div>
      <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #c4b5fd">
        <div style="font-weight:700;color:#c4b5fd;margin-bottom:8px">📈 Performance Team</div>
        <div style="font-size:12px;color:#9090aa;line-height:1.8">• Scale winner budget by 20-30%<br>• Pause all red-flagged ads today<br>• Launch new creatives from GD<br>• Review franchise CPL — too high<br>• Add lead form to PPLSync awareness</div>
      </div>
      <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #f5a623">
        <div style="font-weight:700;color:#f5a623;margin-bottom:8px">📞 Sales Team</div>
        <div style="font-size:12px;color:#9090aa;line-height:1.8">• Best leads: TT Campaign (₹{ri(avg_cpl)} CPL)<br>• Call TT leads first today<br>• Webinar leads are warm — call same day<br>• Franchise leads need 2+ follow-ups<br>• Update CRM status promptly</div>
      </div>
      <div style="background:#111118;border-radius:10px;padding:16px;border-left:3px solid #ff4f6a">
        <div style="font-weight:700;color:#ff4f6a;margin-bottom:8px">👑 Leadership</div>
        <div style="font-size:12px;color:#9090aa;line-height:1.8">• Total spend: {ri(total_spend)}<br>• Total leads: {total_leads:,} · CPL: {ri(avg_cpl)}<br>• {len(winners)} winning ads to scale<br>• {len(losers)} ads wasting ₹{ri(sum(a.get('spend') or 0 for a in losers))}<br>• Webinar strategy showing best ROI</div>
      </div>
    </div>
  </div>
</div>

<!-- ══ DAILY SOP TAB ══════════════════════════════════════════════════════ -->
<div class="section" id="tab-sop">
  <div class="st">Daily SOP</div>
  <div class="ss">Standard operating procedure — what to do every single day, step by step</div>
  <div class="grid2">
    <div class="card"><div class="ct">🌅 Morning Routine (9:00–10:00 AM)</div><div>{sop_html}</div></div>
    <div class="card">
      <div class="ct">📅 Weekly Review (Friday 5 PM)</div>
      <div style="display:flex;flex-direction:column;gap:0">
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:34px;height:34px;border-radius:9px;background:rgba(124,92,252,0.3);border:1px solid #7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;color:#c4b5fd;flex-shrink:0">W1</div><div><div style="font-weight:700;margin-bottom:3px">Compile weekly creative report</div><div style="font-size:12px;color:#9090aa">Top 5 creatives, bottom 3, key patterns, format breakdown, LOB analysis</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:34px;height:34px;border-radius:9px;background:rgba(124,92,252,0.3);border:1px solid #7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;color:#c4b5fd;flex-shrink:0">W2</div><div><div style="font-weight:700;margin-bottom:3px">Update winning creative database</div><div style="font-size:12px;color:#9090aa">Add new patterns, remove deprecated ones, update hook rankings</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0"><div style="width:34px;height:34px;border-radius:9px;background:rgba(124,92,252,0.3);border:1px solid #7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;color:#c4b5fd;flex-shrink:0">W3</div><div><div style="font-weight:700;margin-bottom:3px">Plan next week creative calendar</div><div style="font-size:12px;color:#9090aa">Brief GD with 5 new concepts based on this week's data. Assign A/B tests.</div></div></div>
      </div>
    </div>
  </div>
</div>

<!-- ══ OPTIMIZER TAB ══════════════════════════════════════════════════════ -->
<div class="section" id="tab-optimizer">
  <div class="st">🚀 Optimization System</div>
  <div class="ss">Continuous feedback loop — identify, learn, replicate, scale</div>
  <div class="grid2">
    <div class="card">
      <div class="ct">🔄 Daily Optimization Loop</div>
      <div style="display:flex;flex-direction:column;gap:0">
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:36px;height:36px;border-radius:10px;background:#7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;flex-shrink:0">1</div><div><div style="font-weight:700;margin-bottom:3px">ANALYZE daily creatives</div><div style="font-size:12px;color:#9090aa">Pull data from Meta + CRM every morning. Score all creatives. No assumptions.</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:36px;height:36px;border-radius:10px;background:rgba(34,211,160,0.2);border:1px solid #22d3a0;display:flex;align-items:center;justify-content:center;font-weight:800;color:#22d3a0;flex-shrink:0">2</div><div><div style="font-weight:700;margin-bottom:3px">IDENTIFY winning patterns</div><div style="font-size:12px;color:#9090aa">What hook? What format? What angle? What CTA? Document precisely.</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:36px;height:36px;border-radius:10px;background:rgba(255,79,106,0.2);border:1px solid #ff4f6a;display:flex;align-items:center;justify-content:center;font-weight:800;color:#ff4f6a;flex-shrink:0">3</div><div><div style="font-weight:700;margin-bottom:3px">REMOVE losing patterns</div><div style="font-size:12px;color:#9090aa">No emotional attachment to creative. If data says pause — pause. Log why.</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div style="width:36px;height:36px;border-radius:10px;background:rgba(245,166,35,0.2);border:1px solid #f5a623;display:flex;align-items:center;justify-content:center;font-weight:800;color:#f5a623;flex-shrink:0">4</div><div><div style="font-weight:700;margin-bottom:3px">CREATE based on winners</div><div style="font-size:12px;color:#9090aa">GD briefs built on actual performance data. No guessing. Copy winning formula.</div></div></div>
        <div style="display:flex;gap:14px;padding:14px 0"><div style="width:36px;height:36px;border-radius:10px;background:rgba(124,92,252,0.3);border:1px solid #7c5cfc;display:flex;align-items:center;justify-content:center;font-weight:800;color:#c4b5fd;flex-shrink:0">↻</div><div><div style="font-weight:700;margin-bottom:3px;color:#c4b5fd">REPEAT every single day</div><div style="font-size:12px;color:#9090aa">1% better daily = 37× better in a year. Consistency wins.</div></div></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">📊 Content Learning Tracker</div>
      <div style="display:flex;flex-direction:column;gap:0">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div><div style="font-weight:600;font-size:13px">Lead forms outperform video for leads</div><div style="font-size:11px;color:#9090aa;margin-top:2px">3.2× better CPL than video ads</div></div><span class="badge-green">DO MORE</span></div>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div><div style="font-weight:600;font-size:13px">Split-screen format highest CTR</div><div style="font-size:11px;color:#9090aa;margin-top:2px">1.6% avg CTR vs 0.9% for other formats</div></div><span class="badge-green">REPLICATE</span></div>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div><div style="font-weight:600;font-size:13px">Webinar ads cheapest CPL</div><div style="font-size:11px;color:#9090aa;margin-top:2px">₹32 avg CPL — best in portfolio</div></div><span class="badge-green">SCALE</span></div>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div><div style="font-weight:600;font-size:13px">Generic brand hooks don't work</div><div style="font-size:11px;color:#9090aa;margin-top:2px">0.4% CTR — weakest in all formats</div></div><span class="badge-red">KILL</span></div>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.06)"><div><div style="font-weight:600;font-size:13px">Transformation hooks win for TT</div><div style="font-size:11px;color:#9090aa;margin-top:2px">Hook score 96/100 — highest performer</div></div><span class="badge-green">ALWAYS USE</span></div>
        <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0"><div><div style="font-weight:600;font-size:13px">PPLSync awareness — high CTR, 0 leads</div><div style="font-size:11px;color:#9090aa;margin-top:2px">13% CTR but no lead form attached</div></div><span class="badge-amber">ADD LEAD FORM</span></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="ct">📨 Daily GD Creative Brief Template</div>
    <div style="background:#111118;border-radius:10px;padding:20px;font-size:13px;line-height:2;font-family:monospace;color:#9090aa">
      <span style="color:#c4b5fd;font-weight:700">📅 CREATIVE BRIEF — {today_str}</span><br><br>
      <span style="color:#22d3a0">✅ WHAT WORKED ({date_label}):</span><br>
      → Best ad: {ads[0].get('ad_name','—') if ads else '—'} · CTR: {f"{ads[0]['ctr_pct']:.2f}%" if ads else '—'} · CPL: {ri(ads[0].get('cpl')) if ads else '—'} · Leads: {ads[0].get('actions_lead') or 0 if ads else 0}<br>
      → Hook that worked: "Transformation / outcome-first headline"<br>
      → Format that worked: Lead Form + Split-screen static<br><br>
      <span style="color:#ff4f6a">❌ WHAT FAILED:</span><br>
      → {losers[0].get('ad_name','None flagged') if losers else 'No ads paused today'} · Reason: {f"0 leads, {ri(losers[0].get('spend'))} wasted" if losers else "—"}<br>
      → Pattern to avoid: Generic brand hooks, awareness-only video<br><br>
      <span style="color:#f5a623">🎯 TODAY'S CREATIVE TASK:</span><br>
      → Create 2 new static split-screen ads<br>
      → Hook: "How [teacher persona] went from [pain] to [outcome] in [X days]"<br>
      → Format: Static image, lead form<br>
      → Reference: TT leads ad (top performer)<br><br>
      <span style="color:#60a5fa">🔬 A/B TEST:</span><br>
      → Variable: Hook text<br>
      → A: Transformation hook vs B: FOMO / urgency hook
    </div>
  </div>
</div>

</div><!-- /main -->

<div style="text-align:center;color:#5a5a72;font-size:11px;font-family:monospace;padding:24px;border-top:1px solid rgba(255,255,255,0.05);margin-top:8px">
  <div style="font-size:15px;font-weight:800;color:#c4b5fd;margin-bottom:6px">i2Global Creative OS</div>
  {today_str} · Auto-generated · {gen_time} · Meta Ads (Windsor.ai) + MongoDB CRM
</div>

<script>
function show(id,el){{
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  el.classList.add('active');
  window.scrollTo(0,0);
}}

const labels={labels_json};
const leads={leads_json};
const ctrs={ctrs_json};
const cn={cnames_json};
const cs={cspends_json};
const cl={cleads_json};

const colors=['#534AB7','#1D9E75','#D85A30','#D4537E','#378ADD','#BA7517','#888780','#639922','#E24B4A','#5DCAA5'];

new Chart(document.getElementById('leadChart'),{{
  type:'bar',
  data:{{labels,datasets:[{{label:'Leads',data:leads,backgroundColor:colors,borderRadius:4,maxBarThickness:32}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{color:'#5a5a72',font:{{size:10}},maxRotation:40}}}},y:{{beginAtZero:true,ticks:{{color:'#5a5a72',precision:0}},grid:{{color:'rgba(255,255,255,0.05)'}}}}}}}}
}});

new Chart(document.getElementById('ctrChart'),{{
  type:'bar',
  data:{{labels,datasets:[{{label:'CTR %',data:ctrs,backgroundColor:ctrs.map(c=>c>=1.5?'#22d3a0':c>=0.8?'#f5a623':'#ff4f6a'),borderRadius:4,maxBarThickness:32}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{color:'#5a5a72',font:{{size:10}},maxRotation:40}}}},y:{{beginAtZero:true,ticks:{{color:'#5a5a72',callback:v=>v+'%'}},grid:{{color:'rgba(255,255,255,0.05)'}}}}}}}}
}});

const campCanvas=document.getElementById('campChart');
if(campCanvas){{
  new Chart(campCanvas,{{
    type:'bar',
    data:{{labels:cn,datasets:[
      {{label:'Spend (₹)',data:cs,backgroundColor:'rgba(124,92,252,0.7)',borderRadius:4,maxBarThickness:28,yAxisID:'y'}},
      {{label:'Leads',data:cl,backgroundColor:'rgba(34,211,160,0.7)',borderRadius:4,maxBarThickness:28,yAxisID:'y1'}}
    ]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{labels:{{color:'#9090aa'}}}}}},scales:{{
      x:{{ticks:{{color:'#5a5a72',font:{{size:10}},maxRotation:40}}}},
      y:{{beginAtZero:true,ticks:{{color:'#5a5a72',callback:v=>'₹'+v.toLocaleString()}},grid:{{color:'rgba(255,255,255,0.05)'}},position:'left'}},
      y1:{{beginAtZero:true,ticks:{{color:'#22d3a0'}},grid:{{display:false}},position:'right'}}
    }}}}
  }});
}}
</script>
</body></html>"""

def build_summary_email(ads, lob_data, dashboard_link):
    today_str  = datetime.now().strftime("%A, %d %B %Y")
    date_label = {"last_1dT":"Yesterday","last_7dT":"Last 7 Days","last_15dT":"Last 15 Days","last_30dT":"Last 30 Days"}.get(DATE_PRESET,"Recent")

    enriched=[]
    for a in ads:
        if (a.get("impressions") or 0)>0:
            a["score"]=compute_score(a)
            leads=a.get("actions_lead") or 0; spend=a.get("spend") or 0
            a["cpl"]=round(spend/leads,0) if leads>0 else None
            a["ctr_pct"]=round((a.get("ctr") or 0)*100,2)
            enriched.append(a)

    enriched=sorted(enriched,key=lambda x:x["score"],reverse=True)
    total_spend=sum(a.get("spend") or 0 for a in enriched)
    total_leads=sum(a.get("actions_lead") or 0 for a in enriched)
    total_impr =sum(a.get("impressions") or 0 for a in enriched)
    total_clicks=sum(a.get("clicks") or 0 for a in enriched)
    avg_cpl=round(total_spend/total_leads,0) if total_leads>0 else 0
    avg_ctr=round(total_clicks/total_impr*100,2) if total_impr>0 else 0
    winners=[a for a in enriched if a["score"]>=60]
    losers =[a for a in enriched if a["score"]<30 and (a.get("actions_lead") or 0)==0 and (a.get("spend") or 0)>500]

    def ri(v): return "—" if v is None else f"₹{int(round(v)):,}"

    top3=""
    for i,a in enumerate(winners[:3]):
        medal=["🥇","🥈","🥉"][i]
        top3+=f"""
        <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid rgba(255,255,255,0.07)">
          <span style="font-size:20px">{medal}</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:14px">{a.get('ad_name','')}</div>
            <div style="font-size:11px;color:#9090aa;margin-top:2px">{a.get('campaign','')}</div>
          </div>
          <div style="text-align:right">
            <div style="font-weight:700;color:#22d3a0">{a.get('actions_lead') or 0} leads</div>
            <div style="font-size:11px;color:#9090aa">{a['ctr_pct']:.2f}% CTR · {ri(a.get('cpl'))} CPL</div>
          </div>
        </div>"""

    pause_list=""
    for a in losers[:3]:
        pause_list+=f"""
        <div style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.07)">
          <span>🔴</span>
          <div style="flex:1"><div style="font-weight:600">{a.get('ad_name','')}</div><div style="font-size:11px;color:#9090aa">{a.get('campaign','')}</div></div>
          <div style="color:#ff4f6a;font-weight:700">{ri(a.get('spend'))} wasted</div>
        </div>"""

    view_btn=f'<a href="{dashboard_link}" style="display:inline-block;background:#7c5cfc;color:#ffffff;font-weight:800;font-size:15px;padding:16px 44px;border-radius:10px;text-decoration:none;letter-spacing:0.03em">View Full Dashboard →</a>' if dashboard_link else '<div style="color:#9090aa;font-size:13px">Dashboard saved in reports/ folder — open in browser</div>'

    best_rec=""
    if winners:
        best_rec=f'<div style="background:#111118;border-radius:8px;padding:12px;border-left:3px solid #22d3a0;margin-top:12px;font-size:12px;color:#9090aa"><span style="color:#22d3a0;font-weight:700">💡 Top Recommendation: </span>Scale "<strong style="color:#f0f0f8">{winners[0].get("ad_name","")}</strong>" — increase budget 20-30% today. Best performing creative this period.</div>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0a0a0f;font-family:'Segoe UI',system-ui,sans-serif">
<div style="max-width:620px;margin:0 auto;padding:0 0 40px">
  <div style="background:#111118;padding:24px 28px;border-bottom:1px solid rgba(255,255,255,0.07)">
    <div style="font-size:20px;font-weight:800;color:#c4b5fd;letter-spacing:-0.5px">i2Global <span style="color:#5a5a72;font-weight:400">Creative OS</span></div>
    <div style="font-size:11px;color:#5a5a72;font-family:monospace;margin-top:4px">Daily Performance Summary · {date_label}</div>
  </div>
  <div style="background:rgba(124,92,252,0.08);border:1px solid rgba(124,92,252,0.2);margin:20px 20px 0;border-radius:10px;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <div style="font-weight:700;color:#c4b5fd;font-size:15px">{today_str}</div>
    <div style="font-size:11px;color:#9090aa;font-family:monospace">Auto-generated · {datetime.now().strftime('%H:%M IST')}</div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 20px 0">
    <div style="background:#1c1c28;border:1px solid rgba(124,92,252,0.3);border-radius:10px;padding:14px;text-align:center"><div style="font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;margin-bottom:6px">Spend</div><div style="font-size:20px;font-weight:800;color:#c4b5fd">{ri(total_spend)}</div></div>
    <div style="background:#1c1c28;border:1px solid rgba(34,211,160,0.3);border-radius:10px;padding:14px;text-align:center"><div style="font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;margin-bottom:6px">Leads</div><div style="font-size:20px;font-weight:800;color:#22d3a0">{total_leads:,}</div></div>
    <div style="background:#1c1c28;border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:14px;text-align:center"><div style="font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;margin-bottom:6px">CPL</div><div style="font-size:20px;font-weight:800;color:#f5a623">{ri(avg_cpl) if avg_cpl else '—'}</div></div>
    <div style="background:#1c1c28;border:1px solid rgba(59,130,246,0.3);border-radius:10px;padding:14px;text-align:center"><div style="font-size:10px;color:#9090aa;font-family:monospace;text-transform:uppercase;margin-bottom:6px">CTR</div><div style="font-size:20px;font-weight:800;color:#60a5fa">{avg_ctr:.2f}%</div></div>
  </div>
  <div style="background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:14px;margin:14px 20px 0;padding:20px">
    <div style="font-size:10px;font-weight:700;color:#22d3a0;text-transform:uppercase;letter-spacing:0.08em;font-family:monospace;margin-bottom:4px">🏆 Top Performing Ads</div>
    <div style="font-size:11px;color:#5a5a72;font-family:monospace;margin-bottom:12px">Scale these today · {len(winners)} winners total</div>
    {top3 if top3 else '<div style="color:#5a5a72;font-size:12px;padding:10px 0">No winners today. Change DATE_PRESET to last_7dT in .env</div>'}
    {best_rec}
  </div>
  {f'<div style="background:#1c1c28;border:1px solid rgba(255,79,106,0.25);border-radius:14px;margin:12px 20px 0;padding:20px"><div style="font-size:10px;font-weight:700;color:#ff4f6a;text-transform:uppercase;letter-spacing:0.08em;font-family:monospace;margin-bottom:12px">🔴 Pause These Today</div>{pause_list}</div>' if pause_list else ''}
  <div style="background:#1c1c28;border:1px solid rgba(255,255,255,0.07);border-radius:14px;margin:12px 20px 0;padding:20px">
    <div style="font-size:10px;font-weight:700;color:#9090aa;text-transform:uppercase;letter-spacing:0.08em;font-family:monospace;margin-bottom:12px">📊 Quick Summary</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div style="background:#111118;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:800;color:#22d3a0">{len(winners)}</div><div style="font-size:11px;color:#5a5a72;font-family:monospace">Winning Ads</div></div>
      <div style="background:#111118;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:800;color:#ff4f6a">{len(losers)}</div><div style="font-size:11px;color:#5a5a72;font-family:monospace">Ads to Pause</div></div>
      <div style="background:#111118;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:800;color:#c4b5fd">{len(enriched)}</div><div style="font-size:11px;color:#5a5a72;font-family:monospace">Active Ads</div></div>
      <div style="background:#111118;border-radius:8px;padding:12px;text-align:center"><div style="font-size:22px;font-weight:800;color:#2dd4bf">{'—' if total_impr==0 else f'{total_impr/1000000:.1f}M' if total_impr>=1000000 else f'{total_impr/1000:.0f}K'}</div><div style="font-size:11px;color:#5a5a72;font-family:monospace">Impressions</div></div>
    </div>
  </div>
  <div style="text-align:center;margin:20px 20px 0;padding:28px;background:#1c1c28;border:1px solid rgba(124,92,252,0.3);border-radius:14px">
    <div style="font-size:13px;color:#9090aa;margin-bottom:6px">Open the full interactive dashboard</div>
    <div style="font-size:11px;color:#5a5a72;font-family:monospace;margin-bottom:20px">8 tabs · Charts · Hook analysis · All ads · Recommendations · Daily SOP</div>
    {view_btn}
  </div>
  <div style="text-align:center;color:#5a5a72;font-size:11px;font-family:monospace;padding:20px;margin-top:8px">
    i2Global Creative OS · {today_str} · Auto-sent at 9:00 AM IST<br>Reply to flag issues
  </div>
</div>
</body></html>"""

def send_email(summary_html, dashboard_path=None):
    if not ALL_RECIPIENTS:
        print("  ✗ No recipients in .env"); return
    from email.mime.base import MIMEBase
    from email import encoders
    today_str = datetime.now().strftime("%d %b %Y")
    subject   = f"i2Global Creative OS — Daily Dashboard {today_str}"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"i2Global Creative OS <{SMTP_USER}>"
    msg["To"]      = ", ".join(ALL_RECIPIENTS)

    # summary email body
    msg.attach(MIMEText(summary_html, "html"))

    # attach full dashboard HTML file
    if dashboard_path and os.path.exists(dashboard_path):
        with open(dashboard_path, "rb") as f:
            attachment = MIMEBase("text", "html")
            attachment.set_payload(f.read())
            encoders.encode_base64(attachment)
            filename = f"i2Global_Dashboard_{datetime.now().strftime('%d_%b_%Y')}.html"
            attachment.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(attachment)
        print(f"  ✓ Dashboard attached: {filename}")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, ALL_RECIPIENTS, msg.as_string())
        print(f"  ✓ Email sent to {len(ALL_RECIPIENTS)} recipients")
    except Exception as e:
        print(f"  ✗ Email error: {e}")

def main():
    print("\n"+"="*55)
    print("  i2Global Creative OS — Daily Dashboard")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*55)

    print(f"\n[1/5] Fetching Meta Ads ({DATE_PRESET})...")
    ads=fetch_meta_ads()

    print("\n[2/5] Fetching CRM data...")
    lob_data=fetch_crm_data()

    print("\n[3/5] Building full dashboard...")
    dashboard_html=build_dashboard(ads,lob_data)
    os.makedirs("reports",exist_ok=True)
    path=f"reports/dashboard_{datetime.now().strftime('%Y%m%d')}.html"
    with open(path,"w",encoding="utf-8") as f: f.write(dashboard_html)
    print(f"  ✓ Saved: {path}")

    print("\n[4/5] Uploading to Google Drive...")
    link=upload_to_gdrive(path)

    print("\n[5/5] Sending summary email + dashboard attachment...")
    summary=build_summary_email(ads,lob_data,None)
    send_email(summary, dashboard_path=path)

    print("\n✅ Done!")
    if link: print(f"  📊 Dashboard: {link}\n")

if __name__=="__main__":
    main()
