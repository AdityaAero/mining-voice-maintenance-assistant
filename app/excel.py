"""Excel output: every saved record is written to an .xlsx workbook (works offline).

The workbook is regenerated from the local database on each save, so a failed write (e.g. the file is
open in Excel, which locks it) loses nothing: the next successful write contains every record. Set the
environment variable VOICE_EXCEL to write it elsewhere, e.g. into a OneDrive or Google Drive folder that
syncs when internet is available.
"""
import datetime as dt
import os
import pathlib
import threading

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app import db

EXCEL_PATH = pathlib.Path(os.environ.get("VOICE_EXCEL", db.RUNTIME / "Registros_Mantencion.xlsx"))
_lock = threading.Lock()

LABELS = {"actividad": db.ACTIVIDADES, "resultado": db.RESULTADOS, "turno": db.TURNOS,
          "estado_equipo": db.ESTADOS_EQUIPO}
WIDE = {"descripcion_trabajo", "observaciones", "hallazgo", "accion_realizada", "accion_recomendada",
        "transcripcion_original", "item", "comentario", "items_con_falla", "items_observar", "rellenos", "defectos"}

DICTIONARY = [
    ("evento_id", "Record ID", "Identificador único del registro (también en la base local)"),
    ("fecha_trabajo", "Work date and time", "Fecha y hora en que se realizó el trabajo o la inspección"),
    ("turno", "Shift", "Día / Noche"),
    ("tecnico", "Technician", "Nombre o credencial del técnico"),
    ("equipo_id", "Machine serial", "N° de serie del equipo (registro de activos)"),
    ("equnr", "SAP equipment no.", "N° de equipo SAP"),
    ("flota", "Fleet number", "N° de flota"),
    ("modelo", "Model", "Modelo del equipo o del reconocimiento de voz, según la hoja"),
    ("horometro", "Hour meter (h)", "Horómetro del equipo al momento del trabajo"),
    ("actividad", "Activity", "Inspección diaria (lista CAT 994K) / preventivo / correctivo / reparación mayor"),
    ("lista_id", "Checklist version", "Lista de inspección usada (id@versión)"),
    ("clase_orden", "SAP order class", "Z001…Z012 (significado inferido, no oficial T003O)"),
    ("orden_trabajo", "Work order no.", "N° de orden SAP (AUFNR)"),
    ("aviso", "Notification no.", "N° de aviso SAP (QMNUM)"),
    ("descripcion_trabajo", "Job description", "Descripción del trabajo realizado"),
    ("estado_equipo", "Machine status", "Operativo / con restricción / fuera de servicio – mantención programada / "
                                         "fuera de servicio – emergencia (falla)"),
    ("horas_fuera_servicio", "Downtime (h)", "Horas que el equipo estuvo fuera de servicio (grounded)"),
    ("observaciones", "General remarks", "Observaciones generales del registro"),
    ("n_items_ok", "Items OK", "Inspección diaria: ítems en buen estado"),
    ("n_items_observar", "Items to monitor", "Inspección diaria: ítems con defecto menor (observar)"),
    ("n_items_falla", "Items with a fault", "Inspección diaria: ítems con falla (requiere acción)"),
    ("n_items_na", "Items not applicable", "Inspección diaria: ítems que no aplican"),
    ("items_con_falla", "Faulty items", "Nombres de los ítems con falla"),
    ("items_observar", "Items to monitor (names)", "Nombres de los ítems a observar"),
    ("rellenos", "Top-ups", "Rellenos del registro: compartimiento, cantidad y unidad"),
    ("n_compartimientos", "Compartment lines", "Cantidad de líneas de compartimiento (mantenimiento)"),
    ("n_hallazgos", "Findings", "Líneas con resultado «Con hallazgo»"),
    ("confirmado_en", "Saved at", "Fecha y hora en que se guardó en el equipo"),
    ("estado_sap", "SAP status", "pendiente / sincronizado / error"),
    ("seccion", "Checklist section", "Sección del formulario CAT (desde el suelo, motor, fuera de cabina, cabina)"),
    ("n_item", "Item no.", "N° del ítem en la lista"),
    ("item_id", "Item ID", "Identificador estable del ítem de la lista"),
    ("item", "Checklist item", "Ítem de la lista de inspección"),
    ("critico", "Safety-critical", "Sí = una falla normalmente deja el equipo fuera de servicio"),
    ("estado_item", "Item result", "OK / Observar / Falla / N/A"),
    ("defectos", "Defects", "Defectos marcados en el ítem"),
    ("posicion", "Position", "Posición o lado (p. ej. rueda delantera izquierda)"),
    ("comentario", "Comment", "Comentario del ítem (escrito o dictado)"),
    ("marcado_en", "Marked at", "Hora en que se marcó el resultado del ítem"),
    ("origen", "Source", "Ítem de la lista o línea de compartimiento donde se registró el relleno"),
    ("cantidad", "Quantity", "Cantidad rellenada"),
    ("unidad", "Unit", "L = litros, kg = kilogramos (grasa)"),
    ("linea", "Line no.", "N° de línea dentro del registro"),
    ("compartimiento", "Compartment", "Compartimiento (nombres de la base de datos SAP / laboratorio)"),
    ("conjunto_sap", "SAP assembly", "Conjunto SAP equivalente del compartimiento"),
    ("resultado", "Result", "Sin registrar / No inspeccionado / Sin hallazgos / Con hallazgo"),
    ("hallazgo", "Finding", "Lo que se observó"),
    ("accion_realizada", "Action taken", "Acción realizada"),
    ("accion_recomendada", "Recommended action", "Acción recomendada o pendiente"),
    ("campo", "Dictated field", "Campo en que se dictó"),
    ("transcripcion_original", "Raw transcript", "Texto original del dictado (Whisper o Wispr Flow), antes de correcciones"),
    ("duracion_s", "Audio length (s)", "Duración de la grabación"),
    ("procesamiento_s", "Processing time (s)", "Tiempo de transcripción"),
    ("motor", "Speech engine", "Motor de transcripción"),
    ("archivo_audio", "Audio file", "Ruta del audio dentro de la carpeta data de la app (vacío con Wispr Flow)"),
    ("creado_en", "Recorded at", "Fecha y hora de la grabación"),
]


def _value(col, v):
    if v is None:
        return None
    if col in LABELS:
        return db.label(LABELS[col], v)
    if col in ("fecha_trabajo", "confirmado_en", "creado_en", "marcado_en"):
        try:
            return dt.datetime.fromisoformat(v).replace(tzinfo=None)
        except ValueError:
            return v
    return v


def _sheet(ws, header, rows):
    ws.append(header)
    for r in rows:
        ws.append([_value(c, v) for c, v in zip(header, r)])
    head_fill = PatternFill("solid", fgColor="0B1F4D")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    for i, col in enumerate(header, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = 48 if col in WIDE else max(12, min(28, len(col) + 4))
        for cell in ws[letter][1:]:
            if isinstance(cell.value, dt.datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
            if col in WIDE:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_workbook():
    """Regenerate the workbook. Returns the status dict; never raises for a locked file."""
    with _lock:
        tables = db.export_tables()
        wb = Workbook()
        wb.remove(wb.active)
        for name, header, rows in tables:
            _sheet(wb.create_sheet(name), header, rows)
        _sheet(wb.create_sheet("Diccionario"), ["columna", "english", "descripcion"], DICTIONARY)
        info = wb.create_sheet("Léame")
        for line in ("Registro de mantención por voz — archivo generado automáticamente por la app.",
                     "Hojas: Registros (uno por registro), Inspeccion (un ítem de la lista diaria por fila), "
                     "Detalle (compartimientos de mantenimiento), Rellenos, Grabaciones (dictados).",
                     "No edite este archivo: se reescribe completo cada vez que se guarda un registro.",
                     "Para corregir un dato, hágalo en la app. La base original es data/registro_mantencion.db.",
                     f"Última actualización: {dt.datetime.now():%Y-%m-%d %H:%M}"):
            info.append([line])
        info.column_dimensions["A"].width = 100

        EXCEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = EXCEL_PATH.with_name(f"~tmp_{EXCEL_PATH.name}")
        n = len(tables[0][2])
        try:
            wb.save(tmp)
            os.replace(tmp, EXCEL_PATH)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            db.set_excel_status(EXCEL_PATH, False,
                                error="El archivo Excel está abierto. Ciérrelo; se actualizará automáticamente.")
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            db.set_excel_status(EXCEL_PATH, False, error=f"No se pudo escribir el Excel: {exc}")
        else:
            db.set_excel_status(EXCEL_PATH, True, registros=n)
        return db.excel_status()


def retry_loop(stop: threading.Event, every_s=30):
    """Background retry while a write is pending (e.g. the user had the file open)."""
    while not stop.wait(every_s):
        if db.excel_status()["pendiente"]:
            write_workbook()
