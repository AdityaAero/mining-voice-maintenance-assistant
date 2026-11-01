"""Registro de mantención por voz — local web app (FastAPI) around openai/whisper-large-v3.

Run:  double-click the desktop shortcut (launcher.pyw), or  .venv\\Scripts\\python.exe app\\server.py
Then open http://127.0.0.1:8765 — everything runs on this computer, no internet needed
(except optional dictation through Wispr Flow).
"""
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import ctypes  # noqa: E402
import io  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
import wave  # noqa: E402
import webbrowser  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from ctypes import wintypes  # noqa: E402

import numpy as np  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import db, excel  # noqa: E402
from app.asr import SAMPLE_RATE, SpeechRecognizer  # noqa: E402
from app.wispr import bridge  # noqa: E402

HOST, PORT = "127.0.0.1", int(os.environ.get("VOICE_PORT", "8765"))
STATIC = pathlib.Path(__file__).resolve().parent / "static"
MAX_AUDIO_S = 120
# only this computer's own page may use the API (blocks other web sites and DNS-rebinding tricks)
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
LOCAL_ORIGINS = {f"http://{h}" for h in LOCAL_HOSTS}

recognizer = SpeechRecognizer()
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    db.init()
    recognizer.start_loading()
    if db.excel_status()["pendiente"] or not excel.EXCEL_PATH.exists():
        threading.Thread(target=excel.write_workbook, daemon=True).start()
    threading.Thread(target=excel.retry_loop, args=(_stop,), daemon=True).start()
    yield
    bridge.release()
    _stop.set()


app = FastAPI(title="Registro de mantención por voz", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def solo_este_computador(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.headers.get("host") not in LOCAL_HOSTS or (origin and origin not in LOCAL_ORIGINS):
        return JSONResponse({"errores": ["Solo se permite el acceso desde este computador."]}, status_code=403)
    return await call_next(request)


def lang(request: Request):
    return "en" if request.headers.get("x-lang") == "en" else "es"


def t(request: Request, es: str, en: str):
    return en if lang(request) == "en" else es


@app.exception_handler(db.ValidationError)
async def validation_error(_req, exc: db.ValidationError):
    return JSONResponse({"errores": exc.errors}, status_code=422)


@app.exception_handler(db.NotFound)
async def not_found(req, exc: db.NotFound):
    return JSONResponse({"errores": [db.msg(lang(req), "no_encontrado", x=exc)]}, status_code=404)


def decode_wav(body: bytes) -> np.ndarray:
    try:
        with wave.open(io.BytesIO(body)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise HTTPException(400, f"Audio WAV no válido / invalid WAV audio: {exc}")
    if width != 2:
        raise HTTPException(400, "Se espera WAV PCM de 16 bits / 16-bit PCM WAV expected.")
    audio = np.frombuffer(frames, "<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:  # the browser already sends 16 kHz; this is a fallback
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    if len(audio) > MAX_AUDIO_S * SAMPLE_RATE:
        raise HTTPException(400, f"Grabación demasiado larga (máximo {MAX_AUDIO_S} s) / recording too long.")
    return audio


async def _transcribe(request: Request, idioma: str):
    body = await request.body()
    audio = decode_wav(body)
    if recognizer.status()["state"] == "error":
        raise HTTPException(503, recognizer.status()["error"])
    return body, await run_in_threadpool(recognizer.transcribe, audio, "en" if idioma == "en" else "es")


def _excel_info():
    s = db.excel_status()
    s["ruta"] = str(excel.EXCEL_PATH)
    return s


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/estado")
def estado():
    return {"modelo": recognizer.status(), "pendientes_sap": db.pending_sap(), "excel": _excel_info(),
            "wispr": bridge.status()}


@app.get("/api/catalogo")
def catalogo():
    return db.catalog()


@app.post("/api/eventos")
def crear_borrador(request: Request, fields: dict | None = None):
    return {"evento_id": db.create_draft(fields or {}, lang(request))}


@app.get("/api/eventos")
def listar():
    return db.list_events()


@app.get("/api/eventos/{evento_id}")
def ver(evento_id: str):
    return db.get_event(evento_id)


@app.patch("/api/eventos/{evento_id}")
def actualizar(evento_id: str, fields: dict, request: Request):
    db.update_event(evento_id, fields, lang(request))
    return {"ok": True}


@app.delete("/api/eventos/{evento_id}")
def descartar(evento_id: str, request: Request):
    db.delete_draft(evento_id, lang(request))
    return {"ok": True}


@app.post("/api/eventos/{evento_id}/detalles")
def agregar_detalle(evento_id: str, request: Request, fields: dict | None = None):
    comp = (fields or {}).get("compartimiento") or None
    return {"detalle_id": db.add_details(evento_id, [comp], lang(request))[0]}


@app.post("/api/eventos/{evento_id}/detalles/todos")
def agregar_todos(evento_id: str, request: Request):
    db.add_all_compartments(evento_id, lang(request))
    return db.get_event(evento_id)


@app.patch("/api/eventos/{evento_id}/detalles/{detalle_id}")
def actualizar_detalle(evento_id: str, detalle_id: str, fields: dict, request: Request):
    db.update_detail(evento_id, detalle_id, fields, lang(request))
    return {"ok": True}


@app.delete("/api/eventos/{evento_id}/detalles/{detalle_id}")
def quitar_detalle(evento_id: str, detalle_id: str, request: Request):
    db.delete_detail(evento_id, detalle_id, lang(request))
    return {"ok": True}


@app.put("/api/eventos/{evento_id}/inspeccion/{item_id}")
def marcar_item(evento_id: str, item_id: str, fields: dict, request: Request):
    db.set_item(evento_id, item_id, fields, lang(request))
    return {"ok": True}


@app.post("/api/eventos/{evento_id}/inspeccion/ok-pendientes")
def marcar_pendientes_ok(evento_id: str, fields: dict, request: Request):
    return {"items": db.ok_pending(evento_id, fields.get("seccion"), lang(request))}


@app.delete("/api/eventos/{evento_id}/inspeccion")
def borrar_inspeccion(evento_id: str, request: Request):
    db.clear_checklist(evento_id, lang(request))
    return {"ok": True}


@app.post("/api/eventos/{evento_id}/rellenos")
def agregar_relleno(evento_id: str, fields: dict, request: Request):
    return {"relleno_id": db.add_topup(evento_id, fields, lang(request))}


@app.patch("/api/eventos/{evento_id}/rellenos/{relleno_id}")
def actualizar_relleno(evento_id: str, relleno_id: str, fields: dict, request: Request):
    db.update_topup(evento_id, relleno_id, fields, lang(request))
    return {"ok": True}


@app.delete("/api/eventos/{evento_id}/rellenos/{relleno_id}")
def quitar_relleno(evento_id: str, relleno_id: str, request: Request):
    db.delete_topup(evento_id, relleno_id, lang(request))
    return {"ok": True}


def _check_dictation_target(request, evento_id, campo, detalle_id, item_id):
    allowed = (db.CAMPOS_DICTADO_ITEM if item_id else db.CAMPOS_DICTADO_DETALLE if detalle_id
               else db.CAMPOS_DICTADO_EVENTO)
    if campo not in allowed:
        raise HTTPException(400, t(request, "Campo no válido para dictado.", "Field cannot be dictated."))
    if db.get_event(evento_id)["evento"]["estado"] != "borrador":
        raise HTTPException(409, db.msg(lang(request), "guardado"))


@app.post("/api/eventos/{evento_id}/grabaciones")
async def grabar(evento_id: str, campo: str, request: Request, detalle_id: str | None = None,
                 item_id: str | None = None, idioma: str = "es"):
    _check_dictation_target(request, evento_id, campo, detalle_id, item_id)
    body, result = await _transcribe(request, idioma)
    # keep the original audio as evidence, next to its raw transcript
    rel = pathlib.Path("audio") / evento_id / f"{uuid.uuid4()}.wav"
    (db.RUNTIME / rel).parent.mkdir(parents=True, exist_ok=True)
    (db.RUNTIME / rel).write_bytes(body)
    result["grabacion_id"] = db.add_recording(evento_id, campo, rel.as_posix(), result, detalle_id, item_id,
                                              lang=lang(request))
    return result


@app.post("/api/eventos/{evento_id}/dictados")
def dictado_externo(evento_id: str, fields: dict, request: Request):
    """Text typed into a field by Wispr Flow: kept as the raw transcript (Wispr keeps the audio, not this app)."""
    campo, detalle_id, item_id = fields.get("campo"), fields.get("detalle_id"), fields.get("item_id")
    _check_dictation_target(request, evento_id, campo, detalle_id, item_id)
    texto = str(fields.get("texto") or "").strip()
    if not texto:
        raise HTTPException(400, t(request, "Dictado vacío.", "Empty dictation."))
    result = {"text": texto, "audio_s": fields.get("duracion_s"), "engine": "Wispr Flow (escritorio)",
              "language": None, "processing_s": fields.get("espera_s")}
    gid = db.add_recording(evento_id, campo, None, result, detalle_id, item_id, modelo="Wispr Flow", lang=lang(request))
    return {"grabacion_id": gid}


@app.post("/api/prueba-microfono")
async def prueba_microfono(request: Request, idioma: str = "es"):
    """Microphone test: transcribe without storing anything."""
    _body, result = await _transcribe(request, idioma)
    return result


@app.get("/api/grabaciones/{grabacion_id}/audio")
def audio(grabacion_id: str, request: Request):
    path = db.recording_path(grabacion_id)
    if not path or not path.exists():
        raise HTTPException(404, t(request, "Audio no encontrado.", "Audio not found."))
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/wispr/{accion}")
def wispr(accion: str, request: Request):
    """Hold / release Wispr Flow's push-to-talk shortcut for the field the technician is dictating into."""
    if request.headers.get("x-registro") != "1":  # same-page fetch only; never a plain form or link
        raise HTTPException(403, "Solo desde la app / app only.")
    try:
        if accion == "presionar":
            if not bridge.running():
                raise HTTPException(409, t(request, "Wispr Flow no está abierto en este computador. Ábralo e intente de nuevo.",
                                           "Wispr Flow is not running on this computer. Open it and try again."))
            bridge.press()
        elif accion == "soltar":
            bridge.release()
        elif accion == "cancelar":
            bridge.cancel()
        else:
            raise HTTPException(404, accion)
    except OSError as exc:
        bridge.release()
        raise HTTPException(500, t(request, f"No se pudo usar el atajo de Wispr Flow: {exc}",
                                   f"Could not use the Wispr Flow shortcut: {exc}"))
    return bridge.status()


@app.post("/api/eventos/{evento_id}/confirmar")
def confirmar(evento_id: str, request: Request):
    db.confirm_event(evento_id, lang(request))
    excel.write_workbook()
    return {**db.get_event(evento_id), "excel": _excel_info()}


@app.post("/api/excel/escribir")
def excel_escribir():
    excel.write_workbook()
    return _excel_info()


@app.post("/api/excel/abrir")
def excel_abrir():
    if not excel.EXCEL_PATH.exists():
        excel.write_workbook()
    os.startfile(excel.EXCEL_PATH)  # this server only listens on 127.0.0.1, so it opens on this computer
    return {"ok": True}


@app.get("/api/excel/descargar")
def excel_descargar():
    if not excel.EXCEL_PATH.exists():
        excel.write_workbook()
    return FileResponse(excel.EXCEL_PATH, filename=excel.EXCEL_PATH.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _exit_with_parent(server):
    """When started by the desktop launcher, stop together with it (closing the app window stops the server)."""
    pid = int(os.environ.get("VOICE_PARENT_PID") or 0)
    if not pid:
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)
    server.should_exit = True


if __name__ == "__main__":
    import uvicorn

    url = f"http://{HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(2.0, webbrowser.open, [url]).start()
    print(f"Registro de mantención por voz -> {url}  (Ctrl+C para detener)", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    threading.Thread(target=_exit_with_parent, args=(server,), daemon=True).start()
    server.run()
