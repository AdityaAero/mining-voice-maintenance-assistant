"""Local SQLite store: catalogs, maintenance records (header + daily-inspection checklist or compartment lines),
top-ups, recordings, SAP queue."""
import csv
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"                                    # catalogs (CSV), checklist and vocabulary
RUNTIME = pathlib.Path(os.environ.get("VOICE_RUNTIME", DATA))  # database, audio, Excel
DB_PATH = RUNTIME / "registro_mantencion.db"
SCHEMA_VERSION = 3

# code -> (Spanish label, used in the Excel output; English label)
ACTIVIDADES = {
    "inspeccion_visual_diaria": ("Inspección diaria (lista CAT 994K)", "Daily inspection (CAT 994K checklist)"),
    "mantenimiento_preventivo": ("Mantenimiento preventivo", "Preventive maintenance"),
    "mantenimiento_correctivo": ("Mantenimiento correctivo", "Corrective maintenance"),
    "reparacion_mayor": ("Reparación mayor", "Major repair"),
}
INSPECCION = "inspeccion_visual_diaria"
RESULTADOS = {
    "sin_registrar": ("Sin registrar", "Not recorded"),
    "no_inspeccionado": ("No inspeccionado", "Not inspected"),
    "sin_hallazgos": ("Sin hallazgos", "No findings"),
    "con_hallazgo": ("Con hallazgo", "Finding"),
}
TURNOS = {"dia": ("Día", "Day"), "noche": ("Noche", "Night")}
ESTADOS_EQUIPO = {
    "operativo": ("Operativo", "Operational"),
    "operativo_con_restriccion": ("Operativo con restricción", "Operational with restriction"),
    "fuera_servicio_programado": ("Fuera de servicio – mantención programada", "Grounded – scheduled maintenance"),
    "fuera_servicio_emergencia": ("Fuera de servicio – emergencia (falla)", "Grounded – emergency (breakdown)"),
}
UNIDADES = ("L", "kg")
# fields a technician can fill (and dictate into); numbers are stored as REAL
CAMPOS_EVENTO = ("fecha_trabajo", "turno", "tecnico", "equipo_id", "horometro", "actividad", "clase_orden",
                 "orden_trabajo", "aviso", "descripcion_trabajo", "estado_equipo", "horas_fuera_servicio", "observaciones")
CAMPOS_DETALLE = ("compartimiento", "resultado", "hallazgo", "accion_realizada", "accion_recomendada")
CAMPOS_ITEM = ("estado", "defectos", "posicion", "comentario")
CAMPOS_NUMERICOS = ("horometro", "horas_fuera_servicio", "cantidad")
CAMPOS_DICTADO_EVENTO = ("tecnico", "horometro", "orden_trabajo", "aviso", "descripcion_trabajo", "observaciones")
CAMPOS_DICTADO_DETALLE = ("hallazgo", "accion_realizada", "accion_recomendada")
CAMPOS_DICTADO_ITEM = ("comentario",)


def load_checklist(path=DATA / "checklist_994k.json"):
    """Daily-inspection checklist; shared defects are expanded so every item lists its own."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    common = raw["defectos_comunes"]
    n = 0
    for sec in raw["secciones"]:
        for it in sec["items"]:
            n += 1
            it["n"] = n
            it["seccion"] = sec["id"]
            it["defectos"] = [{"id": d, **common[d]} if isinstance(d, str) else d for d in it.get("defectos", [])]
            for key, default in (("critico", False), ("posiciones", None), ("relleno", None), ("compartimiento", None)):
                it.setdefault(key, default)
            assert len({d["id"] for d in it["defectos"]}) == len(it["defectos"]), it["id"]
            assert not it["posiciones"] or it["posiciones"] in raw["posiciones"], it["id"]
    ids = [it["id"] for sec in raw["secciones"] for it in sec["items"]]
    assert len(ids) == len(set(ids)), "duplicate checklist item id"
    return raw


CHECKLIST = load_checklist()
ITEMS = {it["id"]: it for sec in CHECKLIST["secciones"] for it in sec["items"]}
SECCIONES = {sec["id"]: sec for sec in CHECKLIST["secciones"]}
ESTADOS_ITEM = {k: (v["es"], v["en"]) for k, v in CHECKLIST["estados"].items()}
LISTA_ID = f"{CHECKLIST['id']}@{CHECKLIST['version']}"

MSG = {
    "campo_desconocido": ("Campo desconocido: {x}", "Unknown field: {x}"),
    "numero": ("«{x}» debe ser un número.", "“{x}” must be a number."),
    "guardado": ("Este registro ya fue guardado y no se puede modificar.", "This record was already saved and cannot be changed."),
    "no_encontrado": ("Registro no encontrado: {x}", "Record not found: {x}"),
    "lista_no_valida": ("Valor no válido para la lista seleccionada.", "Invalid value for the selected list."),
    "comp_no_valido": ("Compartimiento no válido: {x}", "Invalid compartment: {x}"),
    "resultado_no_valido": ("Resultado no válido.", "Invalid result."),
    "item_no_valido": ("Ítem de inspección no válido: {x}", "Invalid inspection item: {x}"),
    "estado_no_valido": ("Estado no válido: {x}", "Invalid status: {x}"),
    "defecto_no_valido": ("Defecto no válido para «{item}»: {x}", "Invalid defect for “{item}”: {x}"),
    "posicion_no_valida": ("Posición no válida para «{item}»: {x}", "Invalid position for “{item}”: {x}"),
    "seccion_no_valida": ("Sección no válida: {x}", "Invalid section: {x}"),
    "relleno_destino": ("Indique un ítem con relleno o una línea de compartimiento.", "Give a checklist item with a top-up or a compartment line."),
    "relleno_comp": ("Compartimiento de relleno no válido para «{item}»: {x}", "Invalid top-up compartment for “{item}”: {x}"),
    "fecha_falta": ("Ingrese la fecha y hora del trabajo.", "Enter the work date and time."),
    "fecha_futura": ("La fecha del trabajo no puede ser futura.", "The work date cannot be in the future."),
    "fecha_mala": ("La fecha del trabajo no es válida.", "The work date is not valid."),
    "tecnico": ("Ingrese el nombre del técnico.", "Enter the technician's name."),
    "equipo": ("Seleccione el equipo.", "Select the machine."),
    "actividad": ("Elija qué va a registrar (inspección diaria o mantenimiento).", "Choose what you are recording (daily inspection or maintenance)."),
    "horometro": ("Ingrese el horómetro del equipo.", "Enter the machine hour meter."),
    "negativo": ("{x} no puede ser negativo.", "{x} cannot be negative."),
    "solo_numeros": ("El {x} debe tener solo números (se recibió «{v}»).", "The {x} must contain digits only (got “{v}”)."),
    "estado_equipo": ("Indique el estado del equipo al terminar.", "Select the machine status at the end."),
    "faltan_items": ("Faltan {n} ítem(s) por inspeccionar: {lista}", "{n} item(s) not inspected yet: {lista}"),
    "item_sin_detalle": ("«{item}»: marque el defecto o escriba un comentario.", "“{item}”: tick the defect or write a comment."),
    "relleno_cantidad": ("Relleno en «{x}»: ingrese la cantidad (mayor que 0).", "Top-up in “{x}”: enter the quantity (greater than 0)."),
    "relleno_compartimiento": ("Relleno en «{x}»: elija el compartimiento.", "Top-up in “{x}”: choose the compartment."),
    "linea": ("Línea {n}", "Line {n}"),
    "linea_comp": ("{x}: elija el compartimiento.", "{x}: choose the compartment."),
    "linea_hallazgo": ("{x}: marcado «Con hallazgo» pero falta describir el hallazgo.", "{x}: marked “Finding” but the finding is not described."),
    "linea_resultado": ("{x}: tiene texto pero falta elegir el resultado.", "{x}: has text but no result."),
    "sin_lineas": ("Agregue al menos un compartimiento con su resultado.", "Add at least one compartment with its result."),
}
NOMBRES = {"horometro": ("El horómetro", "The hour meter"),
           "horas_fuera_servicio": ("Las horas fuera de servicio", "Downtime hours"),
           "orden_trabajo": ("N° de orden", "work order no."), "aviso": ("N° de aviso", "notification no.")}


def msg(lang, key, **kw):
    es, en = MSG[key]
    return (en if lang == "en" else es).format(**kw)


def label(options, code, lang="es"):
    pair = options.get(code)
    return (pair[1] if lang == "en" else pair[0]) if pair else code


def _in(values):
    return ",".join(f"'{v}'" for v in values)


EVENTOS_SQL = f"""
CREATE TABLE IF NOT EXISTS eventos (
    evento_id            TEXT PRIMARY KEY,
    fecha_trabajo        TEXT,
    turno                TEXT CHECK (turno IS NULL OR turno IN ({_in(TURNOS)})),
    tecnico              TEXT,
    equipo_id            TEXT REFERENCES equipos(equipo_id),
    horometro            REAL,
    actividad            TEXT CHECK (actividad IS NULL OR actividad IN ({_in(ACTIVIDADES)})),
    clase_orden          TEXT REFERENCES clases_orden(clase_orden),
    orden_trabajo        TEXT,
    aviso                TEXT,
    descripcion_trabajo  TEXT,
    estado_equipo        TEXT CHECK (estado_equipo IS NULL OR estado_equipo IN ({_in(ESTADOS_EQUIPO)})),
    horas_fuera_servicio REAL,
    observaciones        TEXT,
    lista_id             TEXT,
    estado               TEXT NOT NULL DEFAULT 'borrador' CHECK (estado IN ('borrador', 'guardado')),
    creado_en            TEXT NOT NULL,
    actualizado_en       TEXT NOT NULL,
    confirmado_en        TEXT
);"""
GRABACIONES_SQL = """
CREATE TABLE IF NOT EXISTS grabaciones (
    grabacion_id           TEXT PRIMARY KEY,
    evento_id              TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    detalle_id             TEXT,
    item_id                TEXT,
    campo                  TEXT NOT NULL,
    archivo_audio          TEXT,
    duracion_s             REAL,
    transcripcion_original TEXT,
    idioma                 TEXT,
    modelo                 TEXT,
    motor                  TEXT,
    procesamiento_s        REAL,
    creado_en              TEXT NOT NULL
);"""
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS equipos (
    equipo_id   TEXT PRIMARY KEY,
    equnr       TEXT,
    flota       TEXT,
    modelo      TEXT,
    descripcion TEXT
);
CREATE TABLE IF NOT EXISTS compartimientos (
    compartimiento TEXT PRIMARY KEY,
    conjunto_sap   TEXT,
    grupo          TEXT,
    orden          INTEGER
);
CREATE TABLE IF NOT EXISTS clases_orden (
    clase_orden        TEXT PRIMARY KEY,
    descripcion        TEXT,
    actividad_sugerida TEXT
);
{EVENTOS_SQL}
CREATE TABLE IF NOT EXISTS detalles (
    detalle_id         TEXT PRIMARY KEY,
    evento_id          TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    linea              INTEGER NOT NULL,
    compartimiento     TEXT REFERENCES compartimientos(compartimiento),
    resultado          TEXT NOT NULL DEFAULT 'sin_registrar' CHECK (resultado IN ({_in(RESULTADOS)})),
    hallazgo           TEXT,
    accion_realizada   TEXT,
    accion_recomendada TEXT,
    actualizado_en     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspeccion (
    evento_id      TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    item_id        TEXT NOT NULL,
    estado         TEXT CHECK (estado IS NULL OR estado IN ({_in(ESTADOS_ITEM)})),
    defectos       TEXT,
    posicion       TEXT,
    comentario     TEXT,
    marcado_en     TEXT,
    actualizado_en TEXT NOT NULL,
    PRIMARY KEY (evento_id, item_id)
);
CREATE TABLE IF NOT EXISTS rellenos (
    relleno_id     TEXT PRIMARY KEY,
    evento_id      TEXT NOT NULL REFERENCES eventos(evento_id) ON DELETE CASCADE,
    item_id        TEXT,
    detalle_id     TEXT REFERENCES detalles(detalle_id) ON DELETE CASCADE,
    compartimiento TEXT,
    cantidad       REAL,
    unidad         TEXT NOT NULL CHECK (unidad IN ({_in(UNIDADES)})),
    actualizado_en TEXT NOT NULL
);
{GRABACIONES_SQL}
CREATE TABLE IF NOT EXISTS cola_sap (
    evento_id       TEXT PRIMARY KEY REFERENCES eventos(evento_id),
    estado          TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'sincronizado', 'error')),
    intentos        INTEGER NOT NULL DEFAULT 0,
    ultimo_error    TEXT,
    referencias_sap TEXT,
    encolado_en     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS salida_excel (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    ruta             TEXT,
    ultima_escritura TEXT,
    registros        INTEGER,
    ultimo_error     TEXT,
    pendiente        INTEGER NOT NULL DEFAULT 0
);
"""


class NotFound(Exception):
    """Raised when a record id does not exist."""


class ValidationError(Exception):
    """Raised with a list of messages the technician must fix before saving."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def machines_csv():
    """The fleet list: VOICE_MACHINES, else data/machines.csv (your own fleet, kept out of git), else the example."""
    if os.environ.get("VOICE_MACHINES"):
        return pathlib.Path(os.environ["VOICE_MACHINES"])
    own = DATA / "machines.csv"
    return own if own.exists() else DATA / "machines.example.csv"


def _migrate_v2():
    """v2 -> v3 in place, after a backup copy: machine-status codes, downtime column name, recordings without
    audio (Wispr Flow) and per checklist item. The new tables are created by init()."""
    backup = DB_PATH.with_name("registro_mantencion_v2_respaldo.db")
    if not backup.exists():
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("BEGIN")
    try:
        con.execute(EVENTOS_SQL.replace("IF NOT EXISTS eventos", "eventos_v3"))
        con.execute(
            """INSERT INTO eventos_v3 (evento_id, fecha_trabajo, turno, tecnico, equipo_id, horometro, actividad,
                   clase_orden, orden_trabajo, aviso, descripcion_trabajo, estado_equipo, horas_fuera_servicio,
                   observaciones, estado, creado_en, actualizado_en, confirmado_en)
               SELECT evento_id, fecha_trabajo, turno, tecnico, equipo_id, horometro, actividad, clase_orden,
                   orden_trabajo, aviso, descripcion_trabajo,
                   CASE estado_equipo WHEN 'detenido' THEN 'fuera_servicio_emergencia' ELSE estado_equipo END,
                   horas_detencion, observaciones, estado, creado_en, actualizado_en, confirmado_en
               FROM eventos""")
        con.execute("DROP TABLE eventos")
        con.execute("ALTER TABLE eventos_v3 RENAME TO eventos")
        cols = ("grabacion_id, evento_id, detalle_id, campo, archivo_audio, duracion_s, transcripcion_original, "
                "idioma, modelo, motor, procesamiento_s, creado_en")
        con.execute(GRABACIONES_SQL.replace("IF NOT EXISTS grabaciones", "grabaciones_v3"))
        con.execute(f"INSERT INTO grabaciones_v3 ({cols}) SELECT {cols} FROM grabaciones")
        con.execute("DROP TABLE grabaciones")
        con.execute("ALTER TABLE grabaciones_v3 RENAME TO grabaciones")
        problems = con.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key problems after migration: {problems[:5]}")
        con.execute("UPDATE salida_excel SET pendiente = 1")   # rewrite the workbook in the new layout at start
        con.execute("PRAGMA user_version = 3")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")
        con.close()


def init():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "audio").mkdir(exist_ok=True)
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            _migrate_v2()
        elif version != SCHEMA_VERSION:  # first-version database: keep it aside, never delete it
            backup = DB_PATH.with_name(f"registro_mantencion_v{version}_respaldo.db")
            for suffix in ("", "-wal", "-shm"):
                src = pathlib.Path(str(DB_PATH) + suffix)
                if src.exists():
                    shutil.move(src, str(backup) + suffix)
    with connect() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.executemany(
            "INSERT INTO equipos VALUES (:equipo_id, :equnr, :flota, :modelo, :descripcion) "
            "ON CONFLICT(equipo_id) DO UPDATE SET equnr=excluded.equnr, flota=excluded.flota, "
            "modelo=excluded.modelo, descripcion=excluded.descripcion",
            _read_csv(machines_csv()),
        )
        shown = [r for r in _read_csv(DATA / "compartments.csv") if r["mostrar"] == "1"]
        con.executemany(
            "INSERT INTO compartimientos VALUES (:compartimiento, :conjunto_sap, :grupo, :orden) "
            "ON CONFLICT(compartimiento) DO UPDATE SET conjunto_sap=excluded.conjunto_sap, grupo=excluded.grupo, "
            "orden=excluded.orden",
            shown,
        )
        # compartments removed from the CSV stay for old records but leave the dropdown (orden = NULL)
        con.execute(f"UPDATE compartimientos SET orden = NULL WHERE compartimiento NOT IN "
                    f"({','.join('?' * len(shown))})", [r["compartimiento"] for r in shown])
        con.executemany(
            "INSERT INTO clases_orden VALUES (:clase_orden, :descripcion, :actividad_sugerida) "
            "ON CONFLICT(clase_orden) DO UPDATE SET descripcion=excluded.descripcion, "
            "actividad_sugerida=excluded.actividad_sugerida",
            _read_csv(DATA / "order_classes.csv"),
        )
        con.execute("INSERT OR IGNORE INTO salida_excel (id) VALUES (1)")


def _labels(options):
    return {k: {"es": es, "en": en} for k, (es, en) in options.items()}


def catalog():
    with connect() as con:
        equipos = [dict(r) for r in con.execute(
            """SELECT q.*, (SELECT e.horometro FROM eventos e WHERE e.equipo_id = q.equipo_id
                              AND e.estado = 'guardado' AND e.horometro IS NOT NULL
                              ORDER BY e.fecha_trabajo DESC LIMIT 1) AS ultimo_horometro
               FROM equipos q ORDER BY CAST(q.flota AS INTEGER)""")]
        compartimientos = [dict(r) for r in con.execute(
            "SELECT * FROM compartimientos WHERE orden IS NOT NULL ORDER BY orden, compartimiento")]
        clases = [dict(r) for r in con.execute("SELECT * FROM clases_orden ORDER BY clase_orden")]
        tecnicos = [r[0] for r in con.execute(
            "SELECT tecnico FROM eventos WHERE tecnico IS NOT NULL GROUP BY tecnico ORDER BY MAX(actualizado_en) DESC LIMIT 50")]
    return {"equipos": equipos, "compartimientos": compartimientos, "clases_orden": clases, "tecnicos": tecnicos,
            "actividades": _labels(ACTIVIDADES), "resultados": _labels(RESULTADOS), "turnos": _labels(TURNOS),
            "estados_equipo": _labels(ESTADOS_EQUIPO), "inspeccion": INSPECCION, "lista": CHECKLIST}


def _clean(field, value, lang="es"):
    if value is None:
        return None
    if field in CAMPOS_NUMERICOS:
        if value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValidationError([msg(lang, "numero", x=field)])
    value = str(value).strip()
    return value or None


def _require_draft(con, evento_id, lang="es"):
    row = con.execute("SELECT estado FROM eventos WHERE evento_id = ?", (evento_id,)).fetchone()
    if not row:
        raise NotFound(evento_id)
    if row["estado"] != "borrador":
        raise ValidationError([msg(lang, "guardado")])


def create_draft(fields, lang="es"):
    evento_id = str(uuid.uuid4())
    ts = now()
    with connect() as con:
        con.execute("INSERT INTO eventos (evento_id, fecha_trabajo, creado_en, actualizado_en) VALUES (?, ?, ?, ?)",
                    (evento_id, ts[:16], ts, ts))
    if fields:
        update_event(evento_id, fields, lang)
    return evento_id


def update_event(evento_id, fields, lang="es"):
    unknown = set(fields) - set(CAMPOS_EVENTO)
    if unknown:
        raise ValidationError([msg(lang, "campo_desconocido", x=", ".join(sorted(unknown)))])
    values = {k: _clean(k, v, lang) for k, v in fields.items()}
    with connect() as con:
        _require_draft(con, evento_id, lang)
        try:
            sets = ", ".join(f"{k} = :{k}" for k in values)
            con.execute(f"UPDATE eventos SET {sets}, actualizado_en = :ts WHERE evento_id = :id",
                        {**values, "ts": now(), "id": evento_id})
        except sqlite3.IntegrityError:
            raise ValidationError([msg(lang, "lista_no_valida")])


def delete_draft(evento_id, lang="es"):
    with connect() as con:
        _require_draft(con, evento_id, lang)
        con.execute("DELETE FROM eventos WHERE evento_id = ?", (evento_id,))
    shutil.rmtree(RUNTIME / "audio" / evento_id, ignore_errors=True)


# ---------------- compartment lines (maintenance work) ----------------
def add_details(evento_id, compartimientos, lang="es"):
    """Add one line per compartment (None = an empty line). Returns the new detalle_ids."""
    ids = []
    with connect() as con:
        _require_draft(con, evento_id, lang)
        linea = con.execute("SELECT COALESCE(MAX(linea), 0) FROM detalles WHERE evento_id = ?", (evento_id,)).fetchone()[0]
        for comp in compartimientos:
            linea += 1
            detalle_id = str(uuid.uuid4())
            try:
                con.execute("INSERT INTO detalles (detalle_id, evento_id, linea, compartimiento, actualizado_en) "
                            "VALUES (?, ?, ?, ?, ?)", (detalle_id, evento_id, linea, comp, now()))
            except sqlite3.IntegrityError:
                raise ValidationError([msg(lang, "comp_no_valido", x=comp)])
            ids.append(detalle_id)
    return ids


def add_all_compartments(evento_id, lang="es"):
    with connect() as con:
        present = {r[0] for r in con.execute(
            "SELECT compartimiento FROM detalles WHERE evento_id = ? AND compartimiento IS NOT NULL", (evento_id,))}
        all_comps = [r[0] for r in con.execute(
            "SELECT compartimiento FROM compartimientos WHERE orden IS NOT NULL ORDER BY orden")]
    return add_details(evento_id, [c for c in all_comps if c not in present], lang)


def update_detail(evento_id, detalle_id, fields, lang="es"):
    unknown = set(fields) - set(CAMPOS_DETALLE)
    if unknown:
        raise ValidationError([msg(lang, "campo_desconocido", x=", ".join(sorted(unknown)))])
    values = {k: _clean(k, v, lang) for k, v in fields.items()}
    if "resultado" in values and values["resultado"] not in RESULTADOS:
        raise ValidationError([msg(lang, "resultado_no_valido")])
    with connect() as con:
        _require_draft(con, evento_id, lang)
        try:
            sets = ", ".join(f"{k} = :{k}" for k in values)
            cur = con.execute(f"UPDATE detalles SET {sets}, actualizado_en = :ts WHERE detalle_id = :d AND evento_id = :e",
                              {**values, "ts": now(), "d": detalle_id, "e": evento_id})
        except sqlite3.IntegrityError:
            raise ValidationError([msg(lang, "comp_no_valido", x=values.get("compartimiento"))])
        if not cur.rowcount:
            raise NotFound(detalle_id)


def delete_detail(evento_id, detalle_id, lang="es"):
    with connect() as con:
        _require_draft(con, evento_id, lang)
        con.execute("DELETE FROM detalles WHERE detalle_id = ? AND evento_id = ?", (detalle_id, evento_id))


# ---------------- daily-inspection checklist ----------------
def _item(item_id, lang):
    if item_id not in ITEMS:
        raise ValidationError([msg(lang, "item_no_valido", x=item_id)])
    return ITEMS[item_id]


def set_item(evento_id, item_id, fields, lang="es"):
    """Save part of one checklist answer (status, defects, position, comment)."""
    it = _item(item_id, lang)
    unknown = set(fields) - set(CAMPOS_ITEM)
    if unknown:
        raise ValidationError([msg(lang, "campo_desconocido", x=", ".join(sorted(unknown)))])
    name = it["nombre"]["en" if lang == "en" else "es"]
    values = {}
    if "estado" in fields:
        values["estado"] = fields["estado"] or None
        if values["estado"] and values["estado"] not in ESTADOS_ITEM:
            raise ValidationError([msg(lang, "estado_no_valido", x=values["estado"])])
    if "defectos" in fields:
        chosen = list(dict.fromkeys(fields["defectos"] or []))
        bad = [d for d in chosen if d not in {x["id"] for x in it["defectos"]}]
        if bad:
            raise ValidationError([msg(lang, "defecto_no_valido", item=name, x=", ".join(map(str, bad)))])
        values["defectos"] = json.dumps(chosen) if chosen else None
    if "posicion" in fields:
        values["posicion"] = fields["posicion"] or None
        allowed = {p[0] for p in CHECKLIST["posiciones"].get(it["posiciones"] or "", [])}
        if values["posicion"] and values["posicion"] not in allowed:
            raise ValidationError([msg(lang, "posicion_no_valida", item=name, x=values["posicion"])])
    if "comentario" in fields:
        values["comentario"] = _clean("comentario", fields["comentario"], lang)
    ts = now()
    with connect() as con:
        _require_draft(con, evento_id, lang)
        con.execute("INSERT OR IGNORE INTO inspeccion (evento_id, item_id, actualizado_en) VALUES (?, ?, ?)",
                    (evento_id, item_id, ts))
        if values:
            sets = ", ".join(f"{k} = :{k}" for k in values) + (", marcado_en = :ts" if "estado" in values else "")
            con.execute(f"UPDATE inspeccion SET {sets}, actualizado_en = :ts WHERE evento_id = :e AND item_id = :i",
                        {**values, "ts": ts, "e": evento_id, "i": item_id})
        con.execute("UPDATE eventos SET lista_id = COALESCE(lista_id, ?), actualizado_en = ? WHERE evento_id = ?",
                    (LISTA_ID, ts, evento_id))


def ok_pending(evento_id, seccion, lang="es"):
    """Mark every not-yet-answered item of one section as OK. Returns the item ids marked."""
    if seccion not in SECCIONES:
        raise ValidationError([msg(lang, "seccion_no_valida", x=seccion)])
    ts = now()
    with connect() as con:
        _require_draft(con, evento_id, lang)
        done = {r[0] for r in con.execute(
            "SELECT item_id FROM inspeccion WHERE evento_id = ? AND estado IS NOT NULL", (evento_id,))}
        todo = [it["id"] for it in SECCIONES[seccion]["items"] if it["id"] not in done]
        for item_id in todo:
            con.execute("INSERT OR IGNORE INTO inspeccion (evento_id, item_id, actualizado_en) VALUES (?, ?, ?)",
                        (evento_id, item_id, ts))
            con.execute("UPDATE inspeccion SET estado = 'ok', marcado_en = ?, actualizado_en = ? "
                        "WHERE evento_id = ? AND item_id = ?", (ts, ts, evento_id, item_id))
        con.execute("UPDATE eventos SET lista_id = COALESCE(lista_id, ?), actualizado_en = ? WHERE evento_id = ?",
                    (LISTA_ID, ts, evento_id))
    return todo


def clear_checklist(evento_id, lang="es"):
    with connect() as con:
        _require_draft(con, evento_id, lang)
        con.execute("DELETE FROM rellenos WHERE evento_id = ? AND item_id IS NOT NULL", (evento_id,))
        con.execute("DELETE FROM inspeccion WHERE evento_id = ?", (evento_id,))


# ---------------- top-ups (oil, coolant, grease) ----------------
def add_topup(evento_id, fields, lang="es"):
    item_id, detalle_id = fields.get("item_id") or None, fields.get("detalle_id") or None
    if bool(item_id) == bool(detalle_id):
        raise ValidationError([msg(lang, "relleno_destino")])
    comp, unidad = fields.get("compartimiento") or None, "L"
    if item_id:
        it = _item(item_id, lang)
        if not it["relleno"]:
            raise ValidationError([msg(lang, "relleno_destino")])
        allowed, unidad = it["relleno"]["compartimientos"], it["relleno"]["unidad"]
        comp = comp or (allowed[0] if len(allowed) == 1 else None)
        if comp and comp not in allowed:
            raise ValidationError([msg(lang, "relleno_comp", item=it["nombre"][lang], x=comp)])
    cantidad = _clean("cantidad", fields.get("cantidad"), lang)
    relleno_id = str(uuid.uuid4())
    with connect() as con:
        _require_draft(con, evento_id, lang)
        if detalle_id and not con.execute("SELECT 1 FROM detalles WHERE detalle_id = ? AND evento_id = ?",
                                          (detalle_id, evento_id)).fetchone():
            raise NotFound(detalle_id)
        con.execute("INSERT INTO rellenos VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (relleno_id, evento_id, item_id, detalle_id, None if detalle_id else comp, cantidad, unidad, now()))
    return relleno_id


def update_topup(evento_id, relleno_id, fields, lang="es"):
    unknown = set(fields) - {"compartimiento", "cantidad"}
    if unknown:
        raise ValidationError([msg(lang, "campo_desconocido", x=", ".join(sorted(unknown)))])
    with connect() as con:
        _require_draft(con, evento_id, lang)
        row = con.execute("SELECT item_id FROM rellenos WHERE relleno_id = ? AND evento_id = ?",
                          (relleno_id, evento_id)).fetchone()
        if not row:
            raise NotFound(relleno_id)
        values = {}
        if "cantidad" in fields:
            values["cantidad"] = _clean("cantidad", fields["cantidad"], lang)
        if "compartimiento" in fields and row["item_id"]:
            it = ITEMS[row["item_id"]]
            values["compartimiento"] = fields["compartimiento"] or None
            if values["compartimiento"] and values["compartimiento"] not in it["relleno"]["compartimientos"]:
                raise ValidationError([msg(lang, "relleno_comp", item=it["nombre"][lang], x=values["compartimiento"])])
        if values:
            sets = ", ".join(f"{k} = :{k}" for k in values)
            con.execute(f"UPDATE rellenos SET {sets}, actualizado_en = :ts WHERE relleno_id = :r",
                        {**values, "ts": now(), "r": relleno_id})


def delete_topup(evento_id, relleno_id, lang="es"):
    with connect() as con:
        _require_draft(con, evento_id, lang)
        con.execute("DELETE FROM rellenos WHERE relleno_id = ? AND evento_id = ?", (relleno_id, evento_id))


# ---------------- recordings ----------------
def add_recording(evento_id, campo, archivo_audio, result, detalle_id=None, item_id=None,
                  modelo="openai/whisper-large-v3", lang="es"):
    """One dictation: the audio file (None when another app, e.g. Wispr Flow, did the listening) and its raw text."""
    if item_id:
        _item(item_id, lang)
    with connect() as con:
        _require_draft(con, evento_id, lang)
        if detalle_id and not con.execute("SELECT 1 FROM detalles WHERE detalle_id = ? AND evento_id = ?",
                                          (detalle_id, evento_id)).fetchone():
            raise NotFound(detalle_id)
        grabacion_id = pathlib.Path(archivo_audio).stem if archivo_audio else str(uuid.uuid4())
        con.execute(
            "INSERT INTO grabaciones (grabacion_id, evento_id, detalle_id, item_id, campo, archivo_audio, duracion_s, "
            "transcripcion_original, idioma, modelo, motor, procesamiento_s, creado_en) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (grabacion_id, evento_id, detalle_id, item_id, campo, str(archivo_audio) if archivo_audio else None,
             result.get("audio_s"), result["text"], result.get("language"), modelo, result.get("engine"),
             result.get("processing_s"), now()),
        )
    return grabacion_id


def recording_path(grabacion_id):
    with connect() as con:
        row = con.execute("SELECT archivo_audio FROM grabaciones WHERE grabacion_id = ?", (grabacion_id,)).fetchone()
    return RUNTIME / row["archivo_audio"] if row and row["archivo_audio"] else None


# ---------------- records ----------------
def _items_rows(con, evento_id):
    rows = [dict(r) for r in con.execute("SELECT * FROM inspeccion WHERE evento_id = ?", (evento_id,))]
    for r in rows:
        r["defectos"] = json.loads(r["defectos"]) if r["defectos"] else []
    return rows


def get_event(evento_id):
    with connect() as con:
        ev = con.execute("SELECT e.*, q.estado AS estado_sap FROM eventos e LEFT JOIN cola_sap q USING (evento_id) "
                         "WHERE e.evento_id = ?", (evento_id,)).fetchone()
        if not ev:
            raise NotFound(evento_id)
        detalles = [dict(r) for r in con.execute(
            "SELECT * FROM detalles WHERE evento_id = ? ORDER BY linea", (evento_id,))]
        inspeccion = _items_rows(con, evento_id)
        rellenos = [dict(r) for r in con.execute(
            "SELECT * FROM rellenos WHERE evento_id = ? ORDER BY actualizado_en", (evento_id,))]
        grabaciones = [dict(r) for r in con.execute(
            "SELECT grabacion_id, detalle_id, item_id, campo, archivo_audio IS NOT NULL AS con_audio, duracion_s, "
            "transcripcion_original, modelo, motor, procesamiento_s, creado_en "
            "FROM grabaciones WHERE evento_id = ? ORDER BY creado_en", (evento_id,))]
    return {"evento": dict(ev), "detalles": detalles, "inspeccion": inspeccion, "rellenos": rellenos,
            "grabaciones": grabaciones}


def _validate(ev, detalles, items, rellenos, lang):
    errors = []
    L = 1 if lang == "en" else 0
    fecha = ev["fecha_trabajo"]
    if not fecha:
        errors.append(msg(lang, "fecha_falta"))
    else:
        try:
            if dt.datetime.fromisoformat(fecha) > dt.datetime.now() + dt.timedelta(minutes=15):
                errors.append(msg(lang, "fecha_futura"))
        except ValueError:
            errors.append(msg(lang, "fecha_mala"))
    if not ev["tecnico"]:
        errors.append(msg(lang, "tecnico"))
    if not ev["equipo_id"]:
        errors.append(msg(lang, "equipo"))
    if ev["horometro"] is None:
        errors.append(msg(lang, "horometro"))
    if not ev["actividad"]:
        errors.append(msg(lang, "actividad"))
    for field in ("horometro", "horas_fuera_servicio"):
        if ev[field] is not None and ev[field] < 0:
            errors.append(msg(lang, "negativo", x=NOMBRES[field][L]))
    for field in ("orden_trabajo", "aviso"):
        if ev[field] and not ev[field].isdigit():
            errors.append(msg(lang, "solo_numeros", x=NOMBRES[field][L], v=ev[field]))
    if ev["actividad"] == INSPECCION:
        answers = {r["item_id"]: r for r in items}
        missing = [it for it in ITEMS.values() if not (answers.get(it["id"]) or {}).get("estado")]
        if missing:
            names = [it["nombre"][lang] for it in missing]
            errors.append(msg(lang, "faltan_items", n=len(missing),
                              lista="; ".join(names[:6]) + (" …" if len(names) > 6 else "")))
        for it in ITEMS.values():
            r = answers.get(it["id"])
            if r and r["estado"] in ("observacion", "falla") and not r["defectos"] and not r["comentario"]:
                errors.append(msg(lang, "item_sin_detalle", item=it["nombre"][lang]))
    elif ev["actividad"]:
        for d in detalles:
            n = msg(lang, "linea", n=d["linea"]) + (f" ({d['compartimiento']})" if d["compartimiento"] else "")
            has_text = d["hallazgo"] or d["accion_realizada"] or d["accion_recomendada"]
            if not d["compartimiento"] and (has_text or d["resultado"] != "sin_registrar"):
                errors.append(msg(lang, "linea_comp", x=n))
            if d["resultado"] == "con_hallazgo" and not d["hallazgo"]:
                errors.append(msg(lang, "linea_hallazgo", x=n))
            if d["resultado"] == "sin_registrar" and has_text:
                errors.append(msg(lang, "linea_resultado", x=n))
        if not any(d["compartimiento"] and d["resultado"] != "sin_registrar" for d in detalles):
            errors.append(msg(lang, "sin_lineas"))
    lines = {d["detalle_id"]: d for d in detalles}
    for t in rellenos:
        if t["item_id"]:
            where = ITEMS[t["item_id"]]["nombre"][lang]
        else:
            d = lines.get(t["detalle_id"]) or {}
            where = d.get("compartimiento") or msg(lang, "linea", n=d.get("linea", "?"))
        if not t["cantidad"] or t["cantidad"] <= 0:
            errors.append(msg(lang, "relleno_cantidad", x=where))
        if t["item_id"] and not t["compartimiento"]:
            errors.append(msg(lang, "relleno_compartimiento", x=where))
    if not ev["estado_equipo"]:
        errors.append(msg(lang, "estado_equipo"))
    return errors


def confirm_event(evento_id, lang="es"):
    """Validate the whole record, then save it and queue it for SAP in one transaction."""
    with connect() as con:
        _require_draft(con, evento_id, lang)
        ev = con.execute("SELECT * FROM eventos WHERE evento_id = ?", (evento_id,)).fetchone()
        detalles = [dict(r) for r in con.execute("SELECT * FROM detalles WHERE evento_id = ? ORDER BY linea", (evento_id,))]
        items = _items_rows(con, evento_id)
        rellenos = [dict(r) for r in con.execute("SELECT * FROM rellenos WHERE evento_id = ?", (evento_id,))]
        if ev["actividad"] == INSPECCION:   # only the part of the form that was on screen is saved
            detalles = []
        else:
            items = []
            rellenos = [t for t in rellenos if not t["item_id"]]
        errors = _validate(ev, detalles, items, rellenos, lang)
        if errors:
            raise ValidationError(errors)
        ts = now()
        if ev["actividad"] == INSPECCION:
            con.execute("DELETE FROM detalles WHERE evento_id = ?", (evento_id,))
        else:
            con.execute("DELETE FROM rellenos WHERE evento_id = ? AND item_id IS NOT NULL", (evento_id,))
            con.execute("DELETE FROM inspeccion WHERE evento_id = ?", (evento_id,))
            # completely empty lines carry no information; lines with a compartment stay, even if 'sin_registrar'
            con.execute("DELETE FROM detalles WHERE evento_id = ? AND compartimiento IS NULL", (evento_id,))
        con.execute("UPDATE eventos SET estado = 'guardado', confirmado_en = ?, actualizado_en = ? WHERE evento_id = ?",
                    (ts, ts, evento_id))
        con.execute("INSERT INTO cola_sap (evento_id, encolado_en) VALUES (?, ?)", (evento_id, ts))
        con.execute("UPDATE salida_excel SET pendiente = 1 WHERE id = 1")


def list_events(limit=100):
    with connect() as con:
        rows = con.execute(
            """SELECT e.evento_id, e.fecha_trabajo, e.equipo_id, q.flota, e.tecnico, e.actividad, e.orden_trabajo,
                      e.horometro, e.estado_equipo, e.estado, e.confirmado_en, c.estado AS estado_sap,
                      (SELECT COUNT(*) FROM detalles d WHERE d.evento_id = e.evento_id) AS n_lineas,
                      (SELECT COUNT(*) FROM detalles d WHERE d.evento_id = e.evento_id
                         AND d.resultado = 'con_hallazgo') AS n_hallazgos,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id
                         AND i.estado IS NOT NULL) AS n_items,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id
                         AND i.estado IN ('observacion', 'falla')) AS n_defectos
               FROM eventos e
               LEFT JOIN equipos q USING (equipo_id)
               LEFT JOIN cola_sap c USING (evento_id)
               ORDER BY e.actualizado_en DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def pending_sap():
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM cola_sap WHERE estado = 'pendiente'").fetchone()[0]


def _defect_labels(item, codes):
    names = {d["id"]: d["es"] for d in item["defectos"]}
    return "; ".join(names.get(c, c) for c in codes)


def _position_label(item, code):
    for c, es, _en in CHECKLIST["posiciones"].get(item["posiciones"] or "", []):
        if c == code:
            return es
    return code


def export_tables():
    """Saved records as (sheet name, header, rows) for the Excel output. Labels are in Spanish."""
    with connect() as con:
        def q(sql):
            cur = con.execute(sql)
            return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]

        registros = q(
            """SELECT e.evento_id, e.fecha_trabajo, e.turno, e.tecnico, e.equipo_id, q.equnr, q.flota, q.modelo,
                      e.horometro, e.actividad, e.lista_id, e.clase_orden, e.orden_trabajo, e.aviso,
                      e.descripcion_trabajo, e.estado_equipo, e.horas_fuera_servicio, e.observaciones,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id AND i.estado = 'ok') AS n_items_ok,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id AND i.estado = 'observacion') AS n_items_observar,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id AND i.estado = 'falla') AS n_items_falla,
                      (SELECT COUNT(*) FROM inspeccion i WHERE i.evento_id = e.evento_id AND i.estado = 'na') AS n_items_na,
                      NULL AS items_con_falla, NULL AS items_observar, NULL AS rellenos,
                      (SELECT COUNT(*) FROM detalles d WHERE d.evento_id = e.evento_id) AS n_compartimientos,
                      (SELECT COUNT(*) FROM detalles d WHERE d.evento_id = e.evento_id
                         AND d.resultado = 'con_hallazgo') AS n_hallazgos,
                      e.confirmado_en, c.estado AS estado_sap
               FROM eventos e JOIN equipos q USING (equipo_id) LEFT JOIN cola_sap c USING (evento_id)
               WHERE e.estado = 'guardado' ORDER BY e.fecha_trabajo, e.confirmado_en""")
        detalle = q(
            """SELECT e.evento_id, e.fecha_trabajo, e.equipo_id, q.flota, e.actividad, e.orden_trabajo, e.tecnico,
                      d.linea, d.compartimiento, k.conjunto_sap, d.resultado, d.hallazgo, d.accion_realizada,
                      d.accion_recomendada
               FROM detalles d JOIN eventos e USING (evento_id) JOIN equipos q USING (equipo_id)
               LEFT JOIN compartimientos k USING (compartimiento)
               WHERE e.estado = 'guardado' ORDER BY e.fecha_trabajo, e.confirmado_en, d.linea""")
        answers = con.execute(
            """SELECT e.evento_id, e.fecha_trabajo, e.equipo_id, q.flota, e.horometro, e.tecnico, i.item_id,
                      i.estado, i.defectos, i.posicion, i.comentario, i.marcado_en
               FROM inspeccion i JOIN eventos e USING (evento_id) JOIN equipos q USING (equipo_id)
               WHERE e.estado = 'guardado' ORDER BY e.fecha_trabajo, e.confirmado_en""").fetchall()
        tops = con.execute(
            """SELECT e.evento_id, e.fecha_trabajo, e.equipo_id, q.flota, e.horometro, e.tecnico, e.actividad,
                      t.item_id, d.linea, COALESCE(t.compartimiento, d.compartimiento) AS compartimiento,
                      t.cantidad, t.unidad
               FROM rellenos t JOIN eventos e USING (evento_id) JOIN equipos q USING (equipo_id)
               LEFT JOIN detalles d USING (detalle_id)
               WHERE e.estado = 'guardado' ORDER BY e.fecha_trabajo, e.confirmado_en, t.rowid""").fetchall()
        grabaciones = q(
            """SELECT g.evento_id, e.equipo_id, g.campo, d.compartimiento, g.item_id AS item, g.transcripcion_original,
                      g.duracion_s, g.procesamiento_s, g.modelo, g.motor, g.archivo_audio, g.creado_en
               FROM grabaciones g JOIN eventos e USING (evento_id) LEFT JOIN detalles d USING (detalle_id)
               WHERE e.estado = 'guardado' ORDER BY g.creado_en""")

    order = {it["id"]: it["n"] for it in ITEMS.values()}
    insp_header = ["evento_id", "fecha_trabajo", "equipo_id", "flota", "horometro", "tecnico", "seccion", "n_item",
                   "item_id", "item", "critico", "estado_item", "defectos", "posicion", "comentario", "marcado_en"]
    insp_rows, falla, obs = [], {}, {}
    for r in sorted(answers, key=lambda r: (r["fecha_trabajo"], r["evento_id"], order.get(r["item_id"], 999))):
        it = ITEMS.get(r["item_id"])
        if not it:
            continue
        codes = json.loads(r["defectos"]) if r["defectos"] else []
        insp_rows.append((r["evento_id"], r["fecha_trabajo"], r["equipo_id"], r["flota"], r["horometro"], r["tecnico"],
                          SECCIONES[it["seccion"]]["nombre"]["es"], it["n"], it["id"], it["nombre"]["es"],
                          "Sí" if it["critico"] else "No", label(ESTADOS_ITEM, r["estado"]),
                          _defect_labels(it, codes), _position_label(it, r["posicion"]) if r["posicion"] else None,
                          r["comentario"], r["marcado_en"]))
        if r["estado"] == "falla":
            falla.setdefault(r["evento_id"], []).append(it["nombre"]["es"])
        elif r["estado"] == "observacion":
            obs.setdefault(r["evento_id"], []).append(it["nombre"]["es"])
    top_header = ["evento_id", "fecha_trabajo", "equipo_id", "flota", "horometro", "tecnico", "actividad", "origen",
                  "compartimiento", "cantidad", "unidad"]
    top_rows, top_sum = [], {}
    for t in tops:
        origen = ITEMS[t["item_id"]]["nombre"]["es"] if t["item_id"] in ITEMS else f"Línea {t['linea']}"
        top_rows.append((t["evento_id"], t["fecha_trabajo"], t["equipo_id"], t["flota"], t["horometro"], t["tecnico"],
                         t["actividad"], origen, t["compartimiento"], t["cantidad"], t["unidad"]))
        top_sum.setdefault(t["evento_id"], []).append(f"{t['compartimiento']} {t['cantidad']:g} {t['unidad']}")

    reg_header, reg_rows = registros
    col = {c: i for i, c in enumerate(reg_header)}
    filled = []
    for r in reg_rows:
        r = list(r)
        r[col["items_con_falla"]] = "; ".join(falla.get(r[0], [])) or None
        r[col["items_observar"]] = "; ".join(obs.get(r[0], [])) or None
        r[col["rellenos"]] = "; ".join(top_sum.get(r[0], [])) or None
        filled.append(tuple(r))
    graba_header, graba_rows = grabaciones
    gi = graba_header.index("item")
    graba_rows = [tuple(ITEMS[v]["nombre"]["es"] if i == gi and v in ITEMS else v for i, v in enumerate(r))
                  for r in graba_rows]
    return [("Registros", reg_header, filled), ("Inspeccion", insp_header, insp_rows), ("Detalle", *detalle),
            ("Rellenos", top_header, top_rows), ("Grabaciones", graba_header, graba_rows)]


def excel_status():
    with connect() as con:
        return dict(con.execute("SELECT ruta, ultima_escritura, registros, ultimo_error, pendiente FROM salida_excel").fetchone())


def set_excel_status(ruta, ok, registros=None, error=None):
    with connect() as con:
        if ok:
            con.execute("UPDATE salida_excel SET ruta = ?, ultima_escritura = ?, registros = ?, ultimo_error = NULL, "
                        "pendiente = 0 WHERE id = 1", (str(ruta), now(), registros))
        else:
            con.execute("UPDATE salida_excel SET ruta = ?, ultimo_error = ?, pendiente = 1 WHERE id = 1",
                        (str(ruta), error))
