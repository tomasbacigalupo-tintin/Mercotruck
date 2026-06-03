#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercotruck — builder del Tablero de Supervisión de Programación.
Corre en GitHub Actions: lee Airtable (REST), recalcula KPIs, cifra los datos
(AES-256-GCM) y arma ./public (index.html + data.enc.json) para GitHub Pages.

ENV:
  AIRTABLE_PAT   (token de solo lectura a la base Mercotruck 1.1)
  SITE_PASSWORD  (contraseña para descifrar el tablero en el browser)
  BUILD_FROM_FILES=1  (modo test: lee JSON locales en vez de la API)
"""
import os, json, base64, hashlib, datetime, statistics
from collections import defaultdict, Counter

BASE="appPyVjXBRoOln9fG"
T_SDP="tblsTKiWwt5Tqk1a7"; T_OP="tblV9e6v8lhdMCqUG"
ITER=250000
PW=os.environ.get("SITE_PASSWORD","")

# ---- field ids (Solicitudes de Programación) ----
SEM="fldhymAXWvH38wpHd";FEC="fldJCriSk6yQmfybH";EST="fldYUwIe6C0vW38QW";COM="fldgbjIll7aeH0xta";CLI="fld7MWO2lyUWVazaA"
PV="fldR5Bq35gYC4J3mn";PC="fldASPkyPwjv3cMEG";RUSD="fldTV4JYuqAMROZhb";RB="fldHI04B0efLvym1r";CAM="fldz4NO00crIgincU"
SUP="fldSwDowByQYl21I1";SOL="fldZl7Pmo5C4YZ6qK";RC="fldLYjFntZTRVfDz6";RM="fldpqTbsZqGAaRLG1";COC="fldgawzYLpyDi5prc";COM2="fld2POEajeCmAB6nA"
TRESP="fld0Zml0RE0UopAkI";THAC="fldlLBX6ZBCWUsutA";ANT="fldbXL1KUXWYCcjqb"
SDP_FIELDS=[SEM,FEC,EST,COM,CLI,PV,PC,RUSD,RB,CAM,SUP,SOL,RC,RM,COC,COM2,TRESP,THAC,ANT]
# ---- field ids (Operaciones / Órdenes de Viaje) ----
OFC="fldgtVytkvPgu2eNT";OVTA="fldWeiw8Jr8xKLrgh";OCMP="fldy8PszuK9FHl4OZ";OEST="fldSWTYcu37uCqulw"
OP_FIELDS=[OFC,OVTA,OCMP,OEST]

CUR_DEFAULT=None  # se calcula como semana ISO actual
CLOSED={"EMBARQUE CONFIRMADO","CAMIÓN EN PLANTA"}
ABBR={"Argentina":"AR","Brasil":"BR","Chile":"CL","Bolivia":"BO"}
MES={"01":"Ene","02":"Feb","03":"Mar","04":"Abr","05":"May","06":"Jun","07":"Jul","08":"Ago","09":"Sep","10":"Oct","11":"Nov","12":"Dic"}

# ---------- accessors robustos (sirven para REST y para archivos MCP) ----------
def F(rec):
    return rec.get("fields") if "fields" in rec else rec.get("cellValuesByFieldId",{})
def num(v):
    if v is None: return 0.0
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,dict):
        if "valuesByLinkedRecordId" in v:
            s=0.0
            for a in v["valuesByLinkedRecordId"].values():
                for x in (a if isinstance(a,list) else [a]):
                    if isinstance(x,(int,float)): s+=float(x)
            return s
        return 0.0
    if isinstance(v,list): return sum(float(x) for x in v if isinstance(x,(int,float)))
    if isinstance(v,str):
        try: return float(v.replace(",",""))
        except: return 0.0
    return 0.0
def nm(v):
    if v is None: return ""
    if isinstance(v,str): return v
    if isinstance(v,dict):
        if "valuesByLinkedRecordId" in v:
            for a in v["valuesByLinkedRecordId"].values():
                if isinstance(a,list) and a: return str(a[0])
                if a: return str(a)
            return ""
        return v.get("name","")
    if isinstance(v,list) and v:
        return nm(v[0]) if not isinstance(v[0],str) else v[0]
    return ""
def g(rec,fid): return F(rec).get(fid)
def dstr(v): return v[:10] if isinstance(v,str) else ""
def ab(p): return ABBR.get(p,(p[:2].upper() if p else "?"))
def supmap(s):
    if s=="✅": return "Aprobada"
    if s=="❌": return "Rechazada"
    return "Pendiente"
def isoweek(d):
    try:
        dt=datetime.date.fromisoformat(d[:10]); y,w,_=dt.isocalendar(); return f"{y}-W{w:02d}"
    except: return None
def sfbucket(rb): return "Negativa" if rb<0 else ("Bajo mínimo" if rb<250 else "OK")

# ---------- fetch ----------
def fetch_table(table_id, field_ids):
    import requests
    url=f"https://api.airtable.com/v0/{BASE}/{table_id}"
    headers={"Authorization":"Bearer "+os.environ["AIRTABLE_PAT"].strip()}
    out=[]; offset=None
    while True:
        params=[("returnFieldsByFieldId","true"),("pageSize","100")]+[("fields[]",f) for f in field_ids]
        if offset: params.append(("offset",offset))
        r=requests.get(url,headers=headers,params=params,timeout=60); r.raise_for_status()
        j=r.json(); out+=j.get("records",[]); offset=j.get("offset")
        if not offset: break
    return out

def load_records():
    if os.environ.get("BUILD_FROM_FILES")=="1":
        import glob
        sd=json.load(open(os.environ["SDP_FILE"]))["structuredContent"]["records"]
        op=json.load(open(os.environ["OP_FILE"]))["structuredContent"]["records"]
        return sd,op
    return fetch_table(T_SDP,SDP_FIELDS), fetch_table(T_OP,OP_FIELDS)

# ---------- compute ----------
def compute(sdp, op):
    cur=datetime.date.today().isocalendar(); CUR=f"{cur[0]}-W{cur[1]:02d}"
    valid=[r for r in sdp if isinstance(g(r,SEM),str) and g(r,SEM) and "NaN" not in g(r,SEM)]
    # si no hay registros en la semana actual (p.ej. lunes temprano), usar la última semana con datos
    if not any(g(r,SEM)==CUR for r in valid):
        wks=sorted({g(r,SEM) for r in valid})
        if wks: CUR=wks[-1]
    closed=lambda r: nm(g(r,EST)) in CLOSED
    detalle=[]
    for r in sdp:
        est=nm(g(r,EST))
        if not est: continue
        pv=nm(g(r,PV)); pc=nm(g(r,PC))
        detalle.append({"w":(g(r,SEM) if isinstance(g(r,SEM),str) and "NaN" not in g(r,SEM) else ""),
            "f":dstr(g(r,FEC)),"cli":nm(g(r,CLI))[:28],"com":(nm(g(r,COM)) or "").split(" ")[0],
            "est":est,"ruta":(ab(pv)+"→"+ab(pc)) if (pv and pc) else "","r":round(num(g(r,RUSD))),
            "ap":supmap(nm(g(r,SUP))),"cam":int(num(g(r,CAM)) or 0),"sf":sfbucket(num(g(r,RB)))})
    active=[r for r in sdp if nm(g(r,EST))]  # todo el pipeline activo (tenga o no fecha de presentación)
    curr=[r for r in valid if g(r,SEM)==CUR]
    sol=sum(num(g(r,SOL)) for r in curr); cerr=sum(1 for r in curr if closed(r))
    emb=sum(1 for r in curr if nm(g(r,EST))=="EMBARQUE CONFIRMADO"); planta=sum(1 for r in curr if nm(g(r,EST))=="CAMIÓN EN PLANTA")
    falta=sum(1 for r in curr if nm(g(r,EST))=="FALTA TTE")
    sup_cur=Counter(supmap(nm(g(r,SUP))) for r in curr)
    sup_all=Counter(supmap(nm(g(r,SUP))) for r in active)
    est_cur=Counter(nm(g(r,EST)) for r in curr if nm(g(r,EST)))
    com_cur=defaultdict(lambda:[0,0])
    for r in curr:
        c=nm(g(r,COM)) or "(s/c)"; com_cur[c][0]+=int(num(g(r,SOL))); com_cur[c][1]+=1 if closed(r) else 0
    corr=defaultdict(lambda:defaultdict(float)); sank=defaultdict(int); fv=defaultdict(lambda:defaultdict(float))
    for r in active:
        if not closed(r): continue
        pv=nm(g(r,PV)); pc=nm(g(r,PC))
        if not pv or not pc: continue
        k=ab(pv)+"→"+ab(pc); corr[k]["cam"]+=num(g(r,CAM)) or 1; corr[k]["renta"]+=num(g(r,RUSD)); corr[k]["n"]+=1
        sank[("Vta "+ab(pv),"Cmp "+ab(pc))]+=1
        fv[pv]["n"]+=1; fv[pv]["renta"]+=num(g(r,RUSD)); fv[pv]["cam"]+=num(g(r,CAM)) or 1
    def stats(vals):
        vals=sorted(v for v in vals if v and v>0)
        if not vals: return {"n":0,"avg":0,"med":0,"p90":0,"max":0}
        return {"n":len(vals),"avg":round(sum(vals)/len(vals),1),"med":round(statistics.median(vals),1),
                "p90":round(vals[min(len(vals)-1,int(len(vals)*0.9))],1),"max":round(max(vals),1)}
    tresp=[num(g(r,TRESP)) for r in active]
    B=[("<2h",0,2),("2-8h",2,8),("8-24h",8,24),("24-48h",24,48),(">48h",48,1e9)]
    trv=[v for v in tresp if v and v>0]
    fin=defaultdict(lambda:defaultdict(float)); mon=defaultdict(lambda:defaultdict(float)); ytd=defaultdict(float); opdet=[]
    for r in op:
        if nm(g(r,OEST))=="Cancelado": continue
        d=g(r,OFC)
        if not d: continue
        wk=isoweek(d); v=num(g(r,OVTA)); c=num(g(r,OCMP))
        if wk: fin[wk]["n"]+=1; fin[wk]["v"]+=v; fin[wk]["c"]+=c; fin[wk]["r"]+=(v-c)
        mk=d[:7]; mon[mk]["n"]+=1; mon[mk]["v"]+=v; mon[mk]["c"]+=c; mon[mk]["r"]+=(v-c)
        ytd["n"]+=1; ytd["v"]+=v; ytd["c"]+=c; ytd["r"]+=(v-c)
        opdet.append({"f":dstr(d),"w":(wk or "").split("-")[-1] if wk else "","m":MES.get(d[5:7],d[:7]),"est":nm(g(r,OEST)),"v":round(v),"c":round(c),"r":round(v-c)})
    def mlabel(mk):
        y,mm=mk.split("-"); lab=MES.get(mm,mk);
        return lab+("*" if mk==datetime.date.today().strftime("%Y-%m") else "")
    return {
      "actualizado":datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M")+" (ART)",
      "semanaActual":CUR,
      "enCurso":{"sol":int(sol),"cerr":int(cerr),"emb":int(emb),"planta":int(planta),"pctCierre":round(cerr/sol,4) if sol else 0,
        "falta":int(falta),"reproCli":int(sum(num(g(r,RC)) for r in curr)),"reproInt":int(sum(num(g(r,RM)) for r in curr)),
        "rentaCerr":round(sum(num(g(r,RUSD)) for r in curr if closed(r))),"costoOp":round(sum(num(g(r,COC))+num(g(r,COM2)) for r in curr)),
        "pend":int(sup_cur.get("Pendiente",0)),"rech":int(sup_cur.get("Rechazada",0)),"aprob":int(sup_cur.get("Aprobada",0))},
      "proxima":{"wk":"W"+str(cur[1]+1),"sol":int(sum(num(g(r,SOL)) for r in valid if g(r,SEM)==f"{cur[0]}-W{cur[1]+1:02d}")),
        "falta":int(sum(1 for r in valid if g(r,SEM)==f"{cur[0]}-W{cur[1]+1:02d}" and nm(g(r,EST))=="FALTA TTE"))},
      "aprob":[{"k":k,"v":v} for k,v in sup_all.items()],
      "estados":[{"k":k,"v":v} for k,v in est_cur.most_common()],
      "comercial":[{"nombre":c,"sol":s,"cerr":k,"conv":round(k/s,4) if s else 0} for c,(s,k) in sorted(com_cur.items(),key=lambda x:-x[1][1]) if c!="(s/c)"],
      "corredor":[{"ruta":k,"camiones":int(v["cam"]),"renta":round(v["renta"]),"n":int(v["n"])} for k,v in sorted(corr.items(),key=lambda x:-x[1]["renta"])],
      "sankey":{"nodes":sorted({n for k in sank for n in k}),"links":[{"source":s,"target":t,"value":v} for (s,t),v in sorted(sank.items(),key=lambda x:-x[1])]},
      "facturaVenta":[{"pais":k,"n":int(v["n"]),"renta":round(v["renta"]),"cam":int(v["cam"])} for k,v in sorted(fv.items(),key=lambda x:-x[1]["renta"])],
      "tiempos":{"resp":stats(tresp),"buckets":[{"k":l,"v":sum(1 for x in trv if lo<=x<hi)} for l,lo,hi in B]},
      "repro":{"cliente":int(sum(num(g(r,RC)) for r in active)),"interna":int(sum(num(g(r,RM)) for r in active)),
        "coCliente":round(sum(num(g(r,COC)) for r in active)),"coMtk":round(sum(num(g(r,COM2)) for r in active))},
      "semaforo":{"verde":sum(1 for d in detalle if d["sf"]=="OK"),"amarillo":sum(1 for d in detalle if d["sf"]=="Bajo mínimo"),"rojo":sum(1 for d in detalle if d["sf"]=="Negativa")},
      "ytd":{"cargas":int(ytd["n"]),"venta":round(ytd["v"]),"compra":round(ytd["c"]),"renta":round(ytd["r"]),"pct":round(ytd["r"]/ytd["v"],4) if ytd["v"] else 0},
      "semanal":[{"w":w.split("-")[-1],"cargas":int(fin[w]["n"]),"venta":round(fin[w]["v"]),"compra":round(fin[w]["c"]),"renta":round(fin[w]["r"]),"pct":round(fin[w]["r"]/fin[w]["v"],4) if fin[w]["v"] else 0} for w in sorted(fin)],
      "mensual":[{"m":mlabel(mk),"cargas":int(mon[mk]["n"]),"renta":round(mon[mk]["r"]),"pct":round(mon[mk]["r"]/mon[mk]["v"],4) if mon[mk]["v"] else 0} for mk in sorted(mon)],
      "detalle":detalle,"opdetalle":opdet,
    }

def encrypt(obj, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt=os.urandom(16); iv=os.urandom(12)
    key=hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER, 32)
    ct=AESGCM(key).encrypt(iv, json.dumps(obj,ensure_ascii=False).encode(), None)
    return base64.b64encode(salt+iv+ct).decode()

def main():
    if not PW: raise SystemExit("Falta SITE_PASSWORD")
    sdp,op=load_records()
    data=compute(sdp,op)
    os.makedirs("public",exist_ok=True)
    payload=encrypt(data,PW)
    json.dump({"v":1,"iter":ITER,"updated":data["actualizado"],"payload":payload}, open("public/data.enc.json","w"))
    here=os.path.dirname(os.path.abspath(__file__))
    tpl=open(os.path.join(here,"index_template.html"),encoding="utf-8").read()
    open("public/index.html","w",encoding="utf-8").write(tpl)
    print(f"OK · SDP={len(sdp)} OP={len(op)} · semana={data['semanaActual']} · cerradas={data['enCurso']['cerr']} · YTD renta={data['ytd']['renta']} · payload={len(payload)} b64")

if __name__=="__main__":
    main()
