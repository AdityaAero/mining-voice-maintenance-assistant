"""End-to-end test: starts a private server (port 8799, temporary data folder, Wispr Flow keys simulated),
records a corrective job and a daily CAT 994K inspection, checks the save rules in both languages, the
local-only protection, the v2 -> v3 database migration and the Excel workbook. Your real records are never touched.

usage: python tests/test_api.py
"""
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
MACHINES = ROOT / "data" / "machines.example.csv"   # the tests always use the example fleet
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def call(method, path, data=None, raw=None, ctype=None, headers=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", ctype or "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = r.read()
            return r.status, (json.loads(payload) if r.headers.get_content_type() == "application/json" else payload)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def wav(name):
    return (SAMPLES / name).read_bytes()


def dictate(eid, campo, clip, detalle_id=None, item_id=None):
    q = f"campo={campo}" + (f"&detalle_id={detalle_id}" if detalle_id else "") + (f"&item_id={item_id}" if item_id else "")
    t = time.perf_counter()
    st, r = call("POST", f"/api/eventos/{eid}/grabaciones?{q}", raw=wav(clip), ctype="audio/wav")
    print(f"       {clip:14s} -> {campo:20s} {time.perf_counter() - t:4.1f}s  {r.get('text')!r}")
    return st, r


# ---- v2 -> v3 migration, on a database written with the previous schema ----
V2_SCHEMA = """
CREATE TABLE equipos (equipo_id TEXT PRIMARY KEY, equnr TEXT, flota TEXT, modelo TEXT, descripcion TEXT);
CREATE TABLE compartimientos (compartimiento TEXT PRIMARY KEY, conjunto_sap TEXT, grupo TEXT, orden INTEGER);
CREATE TABLE clases_orden (clase_orden TEXT PRIMARY KEY, descripcion TEXT, actividad_sugerida TEXT);
CREATE TABLE eventos (evento_id TEXT PRIMARY KEY, fecha_trabajo TEXT, turno TEXT, tecnico TEXT,
    equipo_id TEXT REFERENCES equipos(equipo_id), horometro REAL, actividad TEXT,
    clase_orden TEXT REFERENCES clases_orden(clase_orden), orden_trabajo TEXT, aviso TEXT, descripcion_trabajo TEXT,
    estado_equipo TEXT CHECK (estado_equipo IS NULL OR estado_equipo IN ('operativo','operativo_con_restriccion','detenido')),
    horas_detencion REAL, observaciones TEXT, estado TEXT NOT NULL DEFAULT 'borrador', creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL, confirmado_en TEXT);
CREATE TABLE detalles (detalle_id TEXT PRIMARY KEY, evento_id TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    linea INTEGER NOT NULL, compartimiento TEXT REFERENCES compartimientos(compartimiento),
    resultado TEXT NOT NULL DEFAULT 'sin_registrar', hallazgo TEXT, accion_realizada TEXT, accion_recomendada TEXT,
    actualizado_en TEXT NOT NULL);
CREATE TABLE grabaciones (grabacion_id TEXT PRIMARY KEY, evento_id TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    detalle_id TEXT, campo TEXT NOT NULL, archivo_audio TEXT NOT NULL, duracion_s REAL, transcripcion_original TEXT,
    idioma TEXT, modelo TEXT, motor TEXT, procesamiento_s REAL, creado_en TEXT NOT NULL);
CREATE TABLE cola_sap (evento_id TEXT PRIMARY KEY REFERENCES eventos(evento_id), estado TEXT NOT NULL DEFAULT 'pendiente',
    intentos INTEGER NOT NULL DEFAULT 0, ultimo_error TEXT, referencias_sap TEXT, encolado_en TEXT NOT NULL);
CREATE TABLE salida_excel (id INTEGER PRIMARY KEY CHECK (id = 1), ruta TEXT, ultima_escritura TEXT, registros INTEGER,
    ultimo_error TEXT, pendiente INTEGER NOT NULL DEFAULT 0);
INSERT INTO equipos VALUES ('EQ-994K-01', '1000000001', '101', 'CAT 994-K', 'Cargador frontal');
INSERT INTO compartimientos VALUES ('MOTOR', 'MOTOR', 'MOTOR', 1);
INSERT INTO eventos VALUES ('ev-v2', '2026-10-30T08:00', 'dia', 'Técnico v2', 'EQ-994K-01', 19000, 'mantenimiento_correctivo',
    NULL, NULL, NULL, 'Cambio de manguera', 'detenido', 3.5, NULL, 'guardado', '2026-10-30T08:00', '2026-10-30T09:00', '2026-10-30T09:00');
INSERT INTO detalles VALUES ('d-v2', 'ev-v2', 1, 'MOTOR', 'con_hallazgo', 'Fuga', NULL, NULL, '2026-10-30T08:30');
INSERT INTO grabaciones VALUES ('g-v2', 'ev-v2', NULL, 'descripcion_trabajo', 'audio/ev-v2/g-v2.wav', 4.0, 'Cambio de manguera',
    'es', 'openai/whisper-large-v3', 'faster-whisper', 9.1, '2026-10-30T08:10');
INSERT INTO cola_sap VALUES ('ev-v2', 'pendiente', 0, NULL, NULL, '2026-10-30T09:00');
PRAGMA user_version = 2;
"""
print("-- migration of a v2 database")
mig = pathlib.Path(tempfile.mkdtemp(prefix="voice_mig_"))
with sqlite3.connect(mig / "registro_mantencion.db") as con:
    con.executescript(V2_SCHEMA)
out = subprocess.run([sys.executable, "-c",
                      "import json, sys; sys.path.insert(0, r'%s'); from app import db; db.init(); e = db.get_event('ev-v2'); "
                      "c = db.connect(); print(json.dumps({'v': c.execute('PRAGMA user_version').fetchone()[0], "
                      "'ev': e['evento'], 'n_det': len(e['detalles']), 'n_grab': len(e['grabaciones']), "
                      "'fk': c.execute('PRAGMA foreign_key_check').fetchall(), 'sap': db.pending_sap()}))" % ROOT],
                     env={**os.environ, "VOICE_RUNTIME": str(mig), "VOICE_MACHINES": str(MACHINES)},
                     capture_output=True, text=True, encoding="utf-8")
m = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
if out.returncode:
    print(out.stderr[-2000:])
check(m.get("v") == 3 and m["ev"]["estado_equipo"] == "fuera_servicio_emergencia" and m["ev"]["horas_fuera_servicio"] == 3.5,
      "v2 record migrated: 'detenido' -> grounded (emergency), downtime column renamed")
check(m.get("n_det") == 1 and m.get("n_grab") == 1 and m.get("sap") == 1 and m.get("fk") == [],
      "lines, recordings and SAP queue kept; foreign keys intact")
check((mig / "registro_mantencion_v2_respaldo.db").exists(), "backup copy of the v2 database kept")
shutil.rmtree(mig, ignore_errors=True)

runtime = pathlib.Path(tempfile.mkdtemp(prefix="voice_test_"))
env = {**os.environ, "VOICE_RUNTIME": str(runtime), "VOICE_PORT": str(PORT), "VOICE_WISPR_DRYRUN": "1",
       "VOICE_MACHINES": str(MACHINES)}
server = subprocess.Popen([sys.executable, str(ROOT / "app" / "server.py"), "--no-browser"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(120):
        try:
            st, s = call("GET", "/api/estado")
            if s["modelo"]["state"] != "cargando":
                break
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(1)
    print("\nestado:", s["modelo"])
    check(s["modelo"]["state"] == "listo", "speech model loaded")

    st, page = call("GET", "/")
    check(st == 200 and b"/static/i18n.js" in page and b"card-lista" in page, "UI page served")
    st, cat = call("GET", "/api/catalogo")
    check(len(cat["equipos"]) == 6, f"6 example machines in catalog ({len(cat['equipos'])})")
    check(any(e["equipo_id"] == "EQ-994K-01" and e["equnr"] == "1000000001" for e in cat["equipos"]), "machine with its SAP equipment no.")
    comps = [c["compartimiento"] for c in cat["compartimientos"]]
    check(len(comps) == 22 and "MANDO FINAL TRASERO IZQUIERDO" in comps and "PERFORACION" not in comps,
          f"22 DB compartments, excluded ones hidden ({len(comps)})")
    lista = cat["lista"]
    items = [it for sec in lista["secciones"] for it in sec["items"]]
    check([s["id"] for s in lista["secciones"]] == ["suelo", "motor", "exterior", "cabina"] and len(items) == 41,
          "checklist: the 4 CAT sections in order, 41 items")
    check(cat["estados_equipo"]["fuera_servicio_emergencia"]["en"] == "Grounded – emergency (breakdown)"
          and "detenido" not in cat["estados_equipo"], "machine status 'Grounded' replaces 'Detenido'")

    print("\n-- local-only protection and Wispr Flow bridge (keys simulated)")
    st, _ = call("GET", "/api/catalogo", headers={"Origin": "http://evil.example"})
    check(st == 403, "request from another web site refused")
    st, _ = call("GET", "/api/catalogo", headers={"Host": f"evil.example:{PORT}"})
    check(st == 403, "request through a foreign host name refused (DNS rebinding)")
    st, _ = call("POST", "/api/wispr/presionar")
    check(st == 403, "Wispr shortcut cannot be triggered without the app's header")
    st, w = call("POST", "/api/wispr/presionar", headers={"X-Registro": "1"})
    check(st == 200 and w["grabando"] and w["atajo"], f"Wispr shortcut held ({w.get('atajo')})")
    st, w = call("POST", "/api/wispr/soltar", headers={"X-Registro": "1"})
    check(st == 200 and not w["grabando"], "Wispr shortcut released")

    print("\n-- record 1: corrective work on EQ-994K-01")
    st, r = call("POST", "/api/eventos", {})
    eid = r["evento_id"]
    check(st == 200, "draft created")
    for bad, msg in (({"equipo_id": "XX1"}, "unknown machine"), ({"actividad": "x"}, "unknown activity"),
                     ({"horometro": "abc"}, "non-numeric hour meter"), ({"estado_equipo": "detenido"}, "old status code"),
                     ({"horas_detencion": 2}, "old downtime field")):
        st, r = call("PATCH", f"/api/eventos/{eid}", bad)
        check(st == 422, f"rejected: {msg}")
    st, r = call("POST", f"/api/eventos/{eid}/confirmar")
    check(st == 422 and len(r["errores"]) >= 5, f"empty draft cannot be saved ({len(r['errores'])} messages)")

    work_date = (dt.datetime.now() - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    st, _ = call("PATCH", f"/api/eventos/{eid}", {
        "fecha_trabajo": work_date, "turno": "dia", "tecnico": "Prueba automática", "equipo_id": "EQ-994K-01",
        "horometro": 41250.5, "actividad": "mantenimiento_correctivo", "clase_orden": "Z003",
        "orden_trabajo": "4000004258A", "aviso": "10012345", "estado_equipo": "fuera_servicio_emergencia",
        "horas_fuera_servicio": 4})
    check(st == 200, "header fields saved (grounded – emergency, 4 h downtime)")

    print("     dictation:")
    st, r = dictate(eid, "descripcion_trabajo", "s06.wav")
    check(st == 200 and "convertidor" in r["text"].lower(), "dictated into job description")
    st, r = dictate(eid, "hallazgo", "s01.wav")
    check(st == 400, "line field without a line is refused")

    _, a = call("POST", f"/api/eventos/{eid}/detalles", {"compartimiento": "SISTEMA HIDRAULICO"})
    _, b = call("POST", f"/api/eventos/{eid}/detalles", {"compartimiento": "TRANSMISION"})
    _, c = call("POST", f"/api/eventos/{eid}/detalles", {"compartimiento": "FRENOS"})
    _, e = call("POST", f"/api/eventos/{eid}/detalles", {})
    a, b, c, e = a["detalle_id"], b["detalle_id"], c["detalle_id"], e["detalle_id"]
    st, r1 = dictate(eid, "hallazgo", "s01.wav", a)
    st, r2 = dictate(eid, "accion_realizada", "s03.wav", a)
    st, r3 = dictate(eid, "hallazgo", "s04.wav", b)
    st, r4 = dictate(eid, "observaciones", "s07_noise.wav")
    check(r4["text"] == "", "noise-only clip gives no text")
    check("izquierdo" in r1["text"].lower() and "no se cambió" in r2["text"].lower() and "15 bar" in r3["text"],
          "line dictations keep side, negation and numbers")
    st, t1 = call("POST", f"/api/eventos/{eid}/rellenos", {"detalle_id": a})
    check(st == 200, "top-up added to a compartment line")
    call("PATCH", f"/api/eventos/{eid}/rellenos/{t1['relleno_id']}", {"cantidad": 45})
    st, _ = call("PUT", f"/api/eventos/{eid}/inspeccion/neumaticos", {"estado": "ok"})   # leftover from another form

    call("PATCH", f"/api/eventos/{eid}/detalles/{a}", {"resultado": "sin_registrar", "hallazgo": r1["text"],
                                                      "accion_realizada": r2["text"]})
    call("PATCH", f"/api/eventos/{eid}/detalles/{b}", {"resultado": "con_hallazgo", "hallazgo": ""})
    call("PATCH", f"/api/eventos/{eid}/detalles/{e}", {"hallazgo": "texto sin compartimiento"})
    future = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    call("PATCH", f"/api/eventos/{eid}", {"fecha_trabajo": future})
    st, r = call("POST", f"/api/eventos/{eid}/confirmar")
    errs = " | ".join(r.get("errores", []))
    check(st == 422 and "futura" in errs, "future date blocked")
    check("solo números" in errs, "work-order number with letters blocked")
    check("falta elegir el resultado" in errs, "text without result blocked")
    check("falta describir" in errs, "'con hallazgo' without text blocked")
    check("elija el compartimiento" in errs, "line without compartment blocked")
    st, r = call("POST", f"/api/eventos/{eid}/confirmar", headers={"X-Lang": "en"})
    errs_en = " | ".join(r.get("errores", []))
    check("cannot be in the future" in errs_en and "digits only" in errs_en, "the same rules answer in English")

    call("PATCH", f"/api/eventos/{eid}", {"fecha_trabajo": work_date, "orden_trabajo": "400000425808"})
    call("PATCH", f"/api/eventos/{eid}/detalles/{a}", {"resultado": "con_hallazgo"})
    call("PATCH", f"/api/eventos/{eid}/detalles/{b}", {"hallazgo": r3["text"]})
    call("PATCH", f"/api/eventos/{eid}/detalles/{c}", {"resultado": "sin_hallazgos"})
    call("DELETE", f"/api/eventos/{eid}/detalles/{e}")
    st, r = call("POST", f"/api/eventos/{eid}/confirmar")
    check(st == 200 and r["evento"]["estado"] == "guardado" and r["evento"]["estado_sap"] == "pendiente",
          "saved locally and queued 'Pendiente SAP'")
    check(not r["excel"]["pendiente"] and pathlib.Path(r["excel"]["ruta"]).exists(), "Excel written on save")
    check(len(r["detalles"]) == 3 and len(r["grabaciones"]) == 5 and len(r["rellenos"]) == 1,
          "3 compartment lines, 5 recordings and the line top-up kept")
    check(r["inspeccion"] == [], "checklist answers from the hidden form part are not saved with a work order")
    st, _ = call("PATCH", f"/api/eventos/{eid}", {"tecnico": "otro"})
    check(st == 422, "saved record is read-only")
    st, audio = call("GET", f"/api/grabaciones/{r['grabaciones'][0]['grabacion_id']}/audio")
    check(st == 200 and audio[:4] == b"RIFF", "recorded audio plays back")

    print("\n-- record 2: daily CAT 994K inspection on EQ-994K-02")
    _, r = call("POST", "/api/eventos", {})
    eid2 = r["evento_id"]
    call("PATCH", f"/api/eventos/{eid2}", {"fecha_trabajo": work_date, "tecnico": "Prueba automática",
                                           "equipo_id": "EQ-994K-02", "actividad": "inspeccion_visual_diaria"})
    for path, body, msg in (("inspeccion/no_existe", {"estado": "ok"}, "unknown item"),
                            ("inspeccion/neumaticos", {"estado": "roto"}, "unknown status"),
                            ("inspeccion/neumaticos", {"defectos": ["fuga_inventada"]}, "defect of another item"),
                            ("inspeccion/neumaticos", {"posicion": "CEN"}, "position not offered for the item")):
        st, _ = call("PUT", f"/api/eventos/{eid2}/{path}", body)
        check(st == 422, f"rejected: {msg}")
    st, _ = call("PUT", f"/api/eventos/{eid2}/inspeccion/neumaticos", {"estado": "falla"})
    check(st == 200, "item marked 'Falla'")
    st, r = call("POST", f"/api/eventos/{eid2}/inspeccion/ok-pendientes", {"seccion": "motor"})
    check(st == 200 and len(r["items"]) == 9, f"'mark pending as OK' marks the 9 engine items ({len(r.get('items', []))})")
    st, top = call("POST", f"/api/eventos/{eid2}/rellenos", {"item_id": "aceite_motor"})
    call("PATCH", f"/api/eventos/{eid2}/rellenos/{top['relleno_id']}", {"cantidad": "5"})
    st, top2 = call("POST", f"/api/eventos/{eid2}/rellenos", {"item_id": "ejes"})
    st, _ = call("POST", f"/api/eventos/{eid2}/rellenos", {"item_id": "rops"})
    check(st == 422, "no top-up on an item without fluid")
    st, _ = call("PATCH", f"/api/eventos/{eid2}/rellenos/{top2['relleno_id']}", {"compartimiento": "MOTOR"})
    check(st == 422, "top-up compartment must belong to the item (axles cannot take engine oil)")

    st, r = call("POST", f"/api/eventos/{eid2}/confirmar")
    errs = " | ".join(r.get("errores", []))
    check(st == 422 and "Faltan 31 ítem(s)" in errs, "items not inspected block the save (31 missing)")
    check("Neumáticos" in errs and "marque el defecto" in errs, "a fault needs a defect or a comment")
    check("Relleno en «Ejes" in errs and "cantidad" in errs and "elija el compartimiento" in errs,
          "top-up without quantity or compartment blocked")
    check("horómetro" in errs and "estado del equipo" in errs, "hour meter and machine status required")
    st, r = call("POST", f"/api/eventos/{eid2}/confirmar", headers={"X-Lang": "en"})
    check("31 item(s) not inspected yet" in " | ".join(r.get("errores", [])), "checklist messages in English too")

    call("PUT", f"/api/eventos/{eid2}/inspeccion/neumaticos", {"defectos": ["corte"], "posicion": "DI"})
    st, rd = dictate(eid2, "comentario", "s01.wav", item_id="neumaticos")
    check(st == 200 and rd["text"], "dictated into a checklist item comment")
    st, _ = call("PUT", f"/api/eventos/{eid2}/inspeccion/vims", {"estado": "observacion", "defectos": ["nivel2"]})
    st, wd = call("POST", f"/api/eventos/{eid2}/dictados", {"campo": "comentario", "item_id": "vims",
                                                           "texto": "Alarma nivel 2, MID 81 CID 1404 FMI 6", "duracion_s": 4.2})
    check(st == 200 and wd["grabacion_id"], "Wispr Flow dictation logged (text, no audio)")
    call("PUT", f"/api/eventos/{eid2}/inspeccion/vims", {"comentario": "Alarma nivel 2, MID 81 CID 1404 FMI 6"})
    for sec in ("suelo", "exterior", "cabina"):
        call("POST", f"/api/eventos/{eid2}/inspeccion/ok-pendientes", {"seccion": sec})
    call("PATCH", f"/api/eventos/{eid2}/rellenos/{top2['relleno_id']}", {"compartimiento": "DIFERENCIAL TRASERO", "cantidad": "12.5"})
    call("PATCH", f"/api/eventos/{eid2}", {"horometro": 21877, "estado_equipo": "operativo_con_restriccion"})
    from openpyxl import load_workbook
    xlsx = runtime / "Registros_Mantencion.xlsx"
    with open(xlsx, "rb"):  # an open handle blocks replacing the file, like Excel does
        st, r = call("POST", f"/api/eventos/{eid2}/confirmar")
    check(st == 200 and r["evento"]["estado"] == "guardado", "inspection saved, even though Excel is locked")
    check(len(r["inspeccion"]) == 41 and sum(x["estado"] == "ok" for x in r["inspeccion"]) == 39, "41 answers kept (39 OK)")
    check(r["excel"]["pendiente"] == 1 and "abierto" in (r["excel"]["ultimo_error"] or ""), "Excel marked pending with a clear message")
    st, x = call("POST", "/api/excel/escribir")
    check(not x["pendiente"] and x["registros"] == 2, "after closing the file, Excel catches up (2 records)")

    wb = load_workbook(xlsx)
    check(wb.sheetnames == ["Registros", "Inspeccion", "Detalle", "Rellenos", "Grabaciones", "Diccionario", "Léame"],
          f"sheets {wb.sheetnames}")
    reg = list(wb["Registros"].values)
    rows = {r_[0]: dict(zip(reg[0], r_)) for r_ in reg[1:]}
    r1x, r2x = rows[eid], rows[eid2]
    check(r1x["equnr"] == "1000000001" and isinstance(r1x["fecha_trabajo"], dt.datetime)
          and r1x["actividad"] == "Mantenimiento correctivo" and r1x["horometro"] == 41250.5, "Registros: work order row")
    check(r1x["estado_equipo"] == "Fuera de servicio – emergencia (falla)" and r1x["horas_fuera_servicio"] == 4,
          "Registros: grounded status and downtime hours")
    check(r2x["n_items_ok"] == 39 and r2x["n_items_falla"] == 1 and r2x["n_items_observar"] == 1
          and "Neumáticos" in (r2x["items_con_falla"] or ""), "Registros: inspection counts and faulty item names")
    check(r2x["rellenos"] == "MOTOR 5 L; DIFERENCIAL TRASERO 12.5 L" and r2x["lista_id"].startswith("inspeccion_diaria_994k@"),
          f"Registros: top-ups summary and checklist version ({r2x['rellenos']})")
    insp = [dict(zip(list(wb["Inspeccion"].values)[0], v)) for v in list(wb["Inspeccion"].values)[1:]]
    neu = next(i for i in insp if i["item_id"] == "neumaticos")
    check(len(insp) == 41 and neu["estado_item"] == "Falla" and neu["defectos"] == "Corte o daño"
          and neu["posicion"] == "Delantera izquierda" and neu["critico"] == "Sí", "Inspeccion: one row per item, labels in Spanish")
    check([i["n_item"] for i in insp] == list(range(1, 42)), "Inspeccion: items in the CAT form order")
    tops = [dict(zip(list(wb["Rellenos"].values)[0], v)) for v in list(wb["Rellenos"].values)[1:]]
    check(sorted((t_["compartimiento"], t_["cantidad"], t_["unidad"]) for t_ in tops)
          == [("DIFERENCIAL TRASERO", 12.5, "L"), ("MOTOR", 5, "L"), ("SISTEMA HIDRAULICO", 45, "L")],
          "Rellenos: line top-up takes the line's compartment; item top-ups their own")
    grab = [dict(zip(list(wb["Grabaciones"].values)[0], v)) for v in list(wb["Grabaciones"].values)[1:]]
    wispr = [g for g in grab if g["modelo"] == "Wispr Flow"]
    check(len(grab) == 7 and len(wispr) == 1 and wispr[0]["archivo_audio"] is None and wispr[0]["item"].startswith("VIMS"),
          "Grabaciones: Whisper rows with audio, Wispr row without, item names shown")
    det = list(wb["Detalle"].values)
    check([d[det[0].index("compartimiento")] for d in det[1:]] == ["SISTEMA HIDRAULICO", "TRANSMISION", "FRENOS"],
          "Detalle: the 3 compartments of the work order in order")

    print("\n-- drafts and microphone test")
    _, r = call("POST", "/api/eventos", {})
    eid3 = r["evento_id"]
    call("PATCH", f"/api/eventos/{eid3}", {"actividad": "inspeccion_visual_diaria"})
    call("PUT", f"/api/eventos/{eid3}/inspeccion/frenos", {"estado": "ok"})
    st, _ = call("DELETE", f"/api/eventos/{eid3}/inspeccion")
    st2, r = call("GET", f"/api/eventos/{eid3}")
    check(st == 200 and r["inspeccion"] == [], "switching away from the inspection clears its answers")
    dictate(eid3, "observaciones", "s05.wav")
    st, _ = call("DELETE", f"/api/eventos/{eid3}")
    st2, _ = call("GET", f"/api/eventos/{eid3}")
    check(st == 200 and st2 == 404 and not (runtime / "audio" / eid3).exists(), "discarded draft and its audio are deleted")
    st, r = call("POST", "/api/prueba-microfono", raw=wav("s01.wav"), ctype="audio/wav")
    check(st == 200 and "manguera" in r["text"].lower() and "grabacion_id" not in r, "microphone test transcribes without storing")
    st, hist = call("GET", "/api/eventos")
    check(len(hist) == 2 and all(h["estado"] == "guardado" for h in hist)
          and next(h for h in hist if h["evento_id"] == eid2)["n_items"] == 41, "history lists the 2 saved records")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    err = server.stderr.read().decode(errors="replace").strip()
    if failures and err:
        print("\nserver stderr:\n" + err[-3000:])
    shutil.rmtree(runtime, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
sys.exit(1 if failures else 0)
