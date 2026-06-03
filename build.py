#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercotruck — builder del Tablero de Supervisión de Programación (GitHub Actions).
Lee Airtable (REST), recalcula KPIs (Solicitudes, Operaciones, Estadías, Tarjetas
Madre, unidades de negocio, lugares de facturación, tiempos), cifra los datos
(AES-256-GCM) y arma ./public (index.html + data.enc.json) para GitHub Pages.

ENV: AIRTABLE_PAT, SITE_PASSWORD
     BUILD_FROM_FILES=1 + SDP_FILE/OP_FILE/EST_FILE/TM_FILE (modo test local)
"""
import os, json, base64, hashlib, datetime, statistics
from collections import defaultdict, Counter

BASE="appPyVjXBRoOln9fG"
T_SDP="tblsTKiWwt5Tqk1a7"; T_OP="tblV9e6v8lhdMCqUG"; T_EST="tblFrBVmrTdGUFGzi"; T_TM="tblgNDyHnuG4pppWY"
ITER=250000; PW=os.environ.get("SITE_PASSWORD","")

# Solicitudes
SEM="fldhymAXWvH38wpHd";FEC="fldJCriSk6yQmfybH";EST="fldYUwIe6C0vW38QW";COM="fldgbjIll7aeH0xta";CLI="fld7MWO2lyUWVazaA"
PV="fldR5Bq35gYC4J3mn";PC="fldASPkyPwjv3cMEG";LC="fld6SXvHvDOJ4ipXr";RUSD="fldTV4JYuqAMROZhb";RB="fldHI04B0efLvym1r";CAM="fldz4NO00crIgincU"
SUP="fldSwDowByQYl21I1";SOL="fldZl7Pmo5C4YZ6qK";RC="fldLYjFntZTRVfDz6";RM="fldpqTbsZqGAaRLG1";COC="fldgawzYLpyDi5prc";CM2="fld2POEajeCmAB6nA";TRESP="fld0Zml0RE0UopAkI"
SDP_FIELDS=[SEM,FEC,EST,COM,CLI,PV,PC,LC,RUSD,RB,CAM,SUP,SOL,RC,RM,COC,CM2,TRESP]
# Operaciones
OFC="fldgtVytkvPgu2eNT";OFD="fldVts5K2a5m9Jxjc";OVTA="fldWeiw8Jr8xKLrgh";OCMP="fldy8PszuK9FHl4OZ";OEST="fldSWTYcu37uCqulw";OSEG="fldbTOj0hkm8JFR0F";OTIPO="fldtCuubcfdf62Gtl"
OP_FIELDS=[OFC,OFD,OVTA,OCMP,OEST,OSEG,OTIPO]
# Estadías
EVTA="fld5XutyJR4Exg6AE";ECOSTO="fldrc1LPsY09jGeb3";EESTADO="fldeEUExHDkEP74Zs";ECLI="fldvY3miAuJmUdGkF"
EST_FIELDS=[EVTA,ECOSTO,EESTADO,ECLI]
# Tarjetas Madre
TMEST="fldwV1N8wVJdIcgVz";TMDIAS="fldPBtwvaEIiDEBcZ";TMCAMP="fld7fXyFYicTGYJN0"
TM_FIELDS=[TMEST,TMDIAS,TMCAMP]

CLOSED={"EMBARQUE CONFIRMADO","CAMIÓN EN PLANTA"}
ABBR={"Argentina":"AR","Brasil":"BR","Chile":"CL","Bolivia":"BO"}
MES={"01":"Ene","02":"Feb","03":"Mar","04":"Abr","05":"May","06":"Jun","07":"Jul","08":"Ago","09":"Sep","10":"Oct","11":"Nov","12":"Dic"}

def F(r): return r.get("fields") if "fields" in r else r.get("cellValuesByFieldId",{})
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
                if isinstance(a,list) and a: return a[0]["name"] if isinstance(a[0],dict) else str(a[0])
            return ""
        return v.get("name","")
    if isinstance(v,list) and v: return nm(v[0])
    return ""
def g(r,fid): return F(r).get(fid)
def dstr(v): return v[:10] if isinstance(v,str) else ""
def ab(p): return ABBR.get(p,(p[:2].upper() if p else "?"))
def supmap(s): return "Aprobada" if s=="✅" else ("Rechazada" if s=="❌" else "Pendiente")
def sfb(rb): return "Negativa" if rb<0 else ("Bajo mínimo" if rb<250 else "OK")
def iso(d):
    try: dt=datetime.date.fromisoformat(d[:10]); y,w,_=dt.isocalendar(); return f"{y}-W{w:02d}"
    except: return None
def mlab(mk): return MES.get(mk[5:7],mk)+("*" if mk==datetime.date.today().strftime("%Y-%m") else "")

def fetch_table(tid, fids):
    import requests
    url=f"https://api.airtable.com/v0/{BASE}/{tid}"; headers={"Authorization":"Bearer "+os.environ["AIRTABLE_PAT"].strip()}
    out=[]; offset=None
    while True:
        params=[("returnFieldsByFieldId","true"),("pageSize","100")]+[("fields[]",f) for f in fids]
        if offset: params.append(("offset",offset))
        r=requests.get(url,headers=headers,params=params,timeout=60); r.raise_for_status()
        j=r.json(); out+=j.get("records",[]); offset=j.get("offset")
        if not offset: break
    return out
def load_file(env): return json.load(open(os.environ[env]))["structuredContent"]["records"]
def load_all():
    if os.environ.get("BUILD_FROM_FILES")=="1":
        return load_file("SDP_FILE"),load_file("OP_FILE"),load_file("EST_FILE"),load_file("TM_FILE")
    return fetch_table(T_SDP,SDP_FIELDS),fetch_table(T_OP,OP_FIELDS),fetch_table(T_EST,EST_FIELDS),fetch_table(T_TM,TM_FIELDS)

def compute(sdp,op,est,tm):
    cur=datetime.date.today().isocalendar(); CUR=f"{cur[0]}-W{cur[1]:02d}"
    closed=lambda r: nm(g(r,EST)) in CLOSED
    det=[]
    for r in sdp:
        e=nm(g(r,EST))
        if not e: continue
        pv=nm(g(r,PV)); pc=nm(g(r,PC))
        det.append({"w":(g(r,SEM) if isinstance(g(r,SEM),str) and "NaN" not in g(r,SEM) else ""),
            "f":dstr(g(r,FEC)),"cli":nm(g(r,CLI))[:30],"com":(nm(g(r,COM)) or "").split(" ")[0] or "(s/c)",
            "est":e,"ruta":(ab(pv)+"→"+ab(pc)) if (pv and pc) else "—","lc":nm(g(r,LC)) or "—",
            "r":round(num(g(r,RUSD))),"ap":supmap(nm(g(r,SUP))),"cam":int(num(g(r,CAM)) or 0),"sf":sfb(num(g(r,RB)))})
    active=[r for r in sdp if nm(g(r,EST))]
    tresp=[num(g(r,TRESP)) for r in active]; trv=sorted(x for x in tresp if x and x>0)
    def stats(v):
        v=sorted(x for x in v if x and x>0)
        return {"n":len(v),"avg":round(sum(v)/len(v),1),"med":round(statistics.median(v),1),"p90":round(v[min(len(v)-1,int(len(v)*.9))],1),"max":round(max(v),1)} if v else {"n":0,"avg":0,"med":0,"p90":0,"max":0}
    Bk=[("<2h",0,2),("2-8h",2,8),("8-24h",8,24),("24-48h",24,48),(">48h",48,9e9)]
    repro={"coCliente":round(sum(num(g(r,COC)) for r in active)),"coMtk":round(sum(num(g(r,CM2)) for r in active))}
    # Operaciones / financiero
    fin=defaultdict(lambda:defaultdict(float)); mon=defaultdict(lambda:defaultdict(float)); ytd=defaultdict(float)
    oest=Counter(); seg_ytd=0.0; seg_mon=defaultdict(float); transit=[]; tipo=Counter(); opdet=[]
    for r in op:
        e=nm(g(r,OEST))
        if e: oest[e]+=1
        if e=="Cancelado": continue
        seg=num(g(r,OSEG)); seg_ytd+=seg
        t=nm(g(r,OTIPO));
        if t: tipo[t]+=1
        d=g(r,OFC)
        if not d: continue
        v=num(g(r,OVTA)); c=num(g(r,OCMP)); wk=iso(d); mk=d[:7]
        if wk: fin[wk]["n"]+=1;fin[wk]["v"]+=v;fin[wk]["c"]+=c;fin[wk]["r"]+=(v-c)
        mon[mk]["n"]+=1;mon[mk]["v"]+=v;mon[mk]["c"]+=c;mon[mk]["r"]+=(v-c); seg_mon[mk]+=seg
        ytd["n"]+=1;ytd["v"]+=v;ytd["c"]+=c;ytd["r"]+=(v-c)
        fd=g(r,OFD)
        if isinstance(fd,str) and fd:
            try:
                dd=(datetime.date.fromisoformat(fd[:10])-datetime.date.fromisoformat(d[:10])).days
                if 0<=dd<=60: transit.append(dd)
            except: pass
        opdet.append({"f":dstr(d),"w":(wk or "").split("-")[-1] if wk else "","m":mlab(mk),"est":e,"v":round(v),"c":round(c),"r":round(v-c)})
    operaciones={"estados":[{"k":k,"v":v} for k,v in oest.most_common()],"prefacturadas":oest.get("Prefacturado",0),
        "listas":oest.get("Control supervisión",0),"transitoProm":round(statistics.mean(transit),1) if transit else 0,"total":sum(oest.values())}
    tipoUnidad=[{"u":k,"n":v} for k,v in tipo.most_common(8)]
    # Estadías
    e_conf=e_canc=0; e_venta=e_costo=0.0; e_cli=defaultdict(lambda:[0,0,0])
    for r in est:
        st=nm(g(r,EESTADO)); v=num(g(r,EVTA)); c=num(g(r,ECOSTO)); cl=nm(g(r,ECLI))[:22] or "(s/d)"
        if st=="Confirmada": e_conf+=1
        if st=="Cancelada": e_canc+=1
        e_venta+=v; e_costo+=c; e_cli[cl][0]+=1; e_cli[cl][1]+=v; e_cli[cl][2]+=c
    estadias={"n":len(est),"conf":e_conf,"canc":e_canc,"costo":round(e_costo),"venta":round(e_venta),"margen":round(e_venta-e_costo),
        "porCliente":[{"cli":k,"n":a[0],"venta":round(a[1]),"margen":round(a[1]-a[2])} for k,a in sorted(e_cli.items(),key=lambda x:-x[1][1])]}
    # Unidades de negocio
    unidades=[{"u":"Flete","venta":round(ytd["v"]),"renta":round(ytd["r"]),"n":int(ytd["n"])},
              {"u":"Seguro","venta":round(seg_ytd),"renta":round(seg_ytd),"n":sum(1 for r in op if num(g(r,OSEG))>0)},
              {"u":"Estadías","venta":round(e_venta),"renta":round(e_venta-e_costo),"n":len(est)}]
    # Tarjetas Madre
    tm_comp=sum(1 for r in tm if nm(g(r,TMEST))=="Completa"); tm_incomp=sum(1 for r in tm if num(g(r,TMCAMP))>0)
    tm_dias=[num(g(r,TMDIAS)) for r in tm if 0<num(g(r,TMDIAS))<900]
    tarjetas={"total":len(tm),"completas":tm_comp,"incompletas":tm_incomp,
        "diasProm":round(statistics.mean(tm_dias),1) if tm_dias else 0,"alerta":sum(1 for d in tm_dias if d>15)}
    return {
      "actualizado":datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M")+" (ART)","semanaActual":CUR,
      "detalle":det,"opdetalle":opdet,
      "tiempos":{"resp":stats(tresp),"buckets":[{"k":l,"v":sum(1 for x in trv if lo<=x<hi)} for l,lo,hi in Bk]},
      "repro":repro,
      "ytd":{"cargas":int(ytd["n"]),"venta":round(ytd["v"]),"compra":round(ytd["c"]),"renta":round(ytd["r"]),"pct":round(ytd["r"]/ytd["v"],4) if ytd["v"] else 0,"seguro":round(seg_ytd)},
      "semanal":[{"w":w.split("-")[-1],"cargas":int(fin[w]["n"]),"venta":round(fin[w]["v"]),"compra":round(fin[w]["c"]),"renta":round(fin[w]["r"]),"pct":round(fin[w]["r"]/fin[w]["v"],4) if fin[w]["v"] else 0} for w in sorted(fin)],
      "mensual":[{"m":mlab(mk),"cargas":int(mon[mk]["n"]),"renta":round(mon[mk]["r"]),"seguro":round(seg_mon[mk]),"pct":round(mon[mk]["r"]/mon[mk]["v"],4) if mon[mk]["v"] else 0} for mk in sorted(mon)],
      "operaciones":operaciones,"tipoUnidad":tipoUnidad,"estadias":estadias,"unidades":unidades,"tarjetas":tarjetas,
    }

def encrypt(obj,pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt=os.urandom(16); iv=os.urandom(12); key=hashlib.pbkdf2_hmac("sha256",pw.encode(),salt,ITER,32)
    ct=AESGCM(key).encrypt(iv,json.dumps(obj,ensure_ascii=False).encode(),None)
    return base64.b64encode(salt+iv+ct).decode()

def main():
    if not PW: raise SystemExit("Falta SITE_PASSWORD")
    sdp,op,est,tm=load_all(); data=compute(sdp,op,est,tm)
    os.makedirs("public",exist_ok=True)
    payload=encrypt(data,PW)
    json.dump({"v":1,"iter":ITER,"updated":data["actualizado"],"payload":payload},open("public/data.enc.json","w"))
    here=os.path.dirname(os.path.abspath(__file__))
    open("public/index.html","w",encoding="utf-8").write(open(os.path.join(here,"index_template.html"),encoding="utf-8").read())
    print(f"OK · SDP={len(sdp)} OP={len(op)} EST={len(est)} TM={len(tm)} · semana={data['semanaActual']} · YTD renta={data['ytd']['renta']} seguro={data['ytd']['seguro']} · payload={len(payload)} b64")

if __name__=="__main__": main()
