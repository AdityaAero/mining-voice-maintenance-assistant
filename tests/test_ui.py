"""Browser test of the real screen: headless Microsoft Edge with a fake microphone that plays a test WAV.

Starts a private server (port 8798, temporary data folder, Wispr Flow keys simulated), fills a daily CAT 994K
inspection (statuses, defects, position, top-up, "mark pending as OK", dictation by click / hold / F2, a Wispr
Flow dictation), saves it, records a corrective job, and checks the English screen, the Excel output and phone width.

usage: python tests/test_ui.py [screenshot_dir]
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
SHOTS = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "screenshots"
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


SHOTS.mkdir(parents=True, exist_ok=True)
runtime = pathlib.Path(tempfile.mkdtemp(prefix="voice_ui_"))
env = {**os.environ, "VOICE_RUNTIME": str(runtime), "VOICE_PORT": str(PORT), "VOICE_WISPR_DRYRUN": "1",
       "VOICE_MACHINES": str(ROOT / "data" / "machines.example.csv")}
server = subprocess.Popen([sys.executable, str(ROOT / "app" / "server.py"), "--no-browser"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/estado", timeout=2)
            break
        except OSError:
            time.sleep(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True, args=[
            "--use-fake-ui-for-media-stream",          # auto-allow the microphone
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={SAMPLES / 's01.wav'}",  # the 'microphone' plays this clip in a loop
        ])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto(BASE)
        page.evaluate("localStorage.setItem('lang', 'es')")
        page.reload()
        page.wait_for_function("document.querySelector('#chip-model').textContent.includes('listo')", timeout=60000)
        page.screenshot(path=str(SHOTS / "1_inicio.png"), full_page=True)
        check(page.locator("#f-actividad .tile").count() == 4, "4 activity tiles: what is being recorded")
        check(page.locator("#f-equipo option").count() == 7, "the 6 example machines in the dropdown")
        check(page.input_value("#f-fecha") != "" and page.is_visible("#f-horometro"), "date/time pre-filled, hour meter next to it")
        check(page.is_hidden("#card-lista") and page.is_visible("#pick-first"), "form parts appear only after choosing the activity")

        page.fill("#f-tecnico", "Técnico UI")
        page.select_option("#f-equipo", "EQ-994K-01")
        page.fill("#f-horometro", "41.250")
        page.click("#f-actividad .tile[data-value='inspeccion_visual_diaria']")
        page.wait_for_selector("#card-lista:not([hidden])")
        page.locator("#f-horometro").blur()
        check(page.input_value("#f-horometro") == "41250", "typed hour meter '41.250' tidied to 41250")
        check(page.locator("#checklist .item").count() == 41 and page.locator("#checklist .sec").count() == 4,
              "checklist shown: 41 items in 4 sections")
        check(page.inner_text("#lista-progress .count") == "0 de 41", "progress 0 de 41")
        check(page.is_visible("#btn-next") and "41" in page.inner_text("#btn-next"), "'next pending' button in the save bar")
        n_mics = page.locator("#record-form .mic:visible").count()
        print(f"       visible dictation buttons: {n_mics}")

        # tires: fault -> defect chip, position, dictated comment
        tires = page.locator("#item-neumaticos")
        tires.locator(".status button[data-v='falla']").click()
        check(tires.locator(".item-body").is_visible() and tires.locator(".row-warn").is_visible(),
              "'Falla' opens the item and asks for the defect")
        tires.locator(".defects button[data-d='corte']").click()
        tires.locator("select.pos").select_option("DI")
        check(tires.locator(".row-warn").is_hidden(), "one tap on a defect is enough detail")
        mic = tires.locator(".vfield .mic")
        mic.click()
        page.wait_for_timeout(1500)
        check("Detener" in mic.inner_text(), "dictation button turns into 'Detener' while recording")
        page.wait_for_timeout(5000)
        mic.click()
        page.wait_for_function("document.querySelector('#item-neumaticos textarea').value.length > 0", timeout=90000)
        txt = tires.locator("textarea").input_value()
        print(f"       click-dictation -> {txt!r}")
        check("manguera" in txt.lower(), "click start/stop dictation fills the item comment")
        check(page.is_visible("#critical-warn"), "fault on a safety-critical item raises a warning")

        # replay: play -> pause (position kept) -> continue
        play = tires.locator(".raw .player .play")
        play.click()
        page.wait_for_timeout(1500)
        check(play.inner_text() == "⏸ Pausar", "replay: 'Pausar' while playing")
        play.click()
        page.wait_for_timeout(300)
        pos = tires.locator(".raw .player .seek").input_value()
        page.wait_for_timeout(800)
        check(play.inner_text() == "▶ Continuar" and float(pos) > 0.5 and tires.locator(".raw .player .seek").input_value() == pos,
              f"replay: pause keeps the position ({pos} s)")
        play.click()
        page.wait_for_timeout(600)
        check(play.inner_text() == "⏸ Pausar", "replay: 'Continuar' resumes")
        play.click()

        # VIMS: monitor + defect, F2 dictation into its comment
        vims = page.locator("#item-vims")
        vims.locator(".status button[data-v='observacion']").click()
        vims.locator(".defects button[data-d='nivel2']").click()
        vims.locator("textarea").focus()
        page.keyboard.press("F2")
        page.wait_for_timeout(6500)
        page.keyboard.press("F2")
        page.wait_for_function("document.querySelector('#item-vims textarea').value.length > 0", timeout=90000)
        print(f"       F2-dictation    -> {vims.locator('textarea').input_value()!r}")
        check(len(vims.locator("textarea").input_value()) > 10, "F2 dictates (Whisper) into the selected item")

        # engine oil top-up
        oil = page.locator("#item-aceite_motor")
        oil.locator(".add-topup").click()
        page.wait_for_selector("#item-aceite_motor .topup-row")
        check(oil.locator(".topup-comp").inner_text() == "MOTOR", "engine-oil top-up preset to compartment MOTOR")
        oil.locator(".topup-row .qty").fill("5")

        # Wispr Flow engine: the app holds Wispr's shortcut (simulated here), Wispr pastes into the field
        page.click("#dictbar button[data-engine='wispr']")
        radio = page.locator("#item-radio")
        radio.locator(".item-more").click()
        wbtn = radio.locator(".vfield .mic")
        check(wbtn.inner_text() == "Wispr" and page.is_visible("#dict-help kbd"), "Wispr mode: buttons say 'Wispr', help shows Ctrl+Win")
        wbtn.click()
        page.wait_for_timeout(700)
        held = page.evaluate("fetch('/api/estado').then(r => r.json()).then(s => s.wispr.grabando)")
        focused = page.evaluate("document.activeElement === document.querySelector('#item-radio textarea')")
        check(held and focused, "Wispr shortcut held while the item's text box has the focus")
        wbtn.click()
        page.wait_for_timeout(300)
        page.evaluate("""() => { const ta = document.querySelector('#item-radio textarea');
            const dt = new DataTransfer(); dt.setData('text/plain', 'Radio sin comunicación con despacho');
            ta.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
        page.wait_for_function("document.querySelector('#item-radio textarea').value.includes('despacho')", timeout=5000)
        check("Wispr" in radio.locator(".raw").inner_text(), "Wispr text lands in the field, marked 'Dictado con Wispr Flow'")
        page.click("#dictbar button[data-engine='whisper']")

        # everything else OK with one tap per section
        for sec in ("suelo", "motor", "exterior", "cabina"):
            page.click(f"#checklist .sec[data-sec='{sec}'] .ok-rest")
            page.wait_for_timeout(300)
        page.wait_for_function("document.querySelector('#lista-progress .count').textContent === '41 de 41'", timeout=10000)
        check(page.is_hidden("#btn-next"), "all 41 inspected: 'next pending' disappears")
        check(page.locator("#item-neumaticos").get_attribute("data-estado") == "falla", "'mark pending as OK' keeps the fault")

        # result: hold-to-talk dictation into the remarks, grounded status shows downtime
        page.click("#f-estado button[data-value='operativo_con_restriccion']")
        check(page.is_visible("#critical-warn"), "warning stays while the machine is not grounded")
        page.click("#f-estado button[data-value='fuera_servicio_emergencia']")
        check(page.is_hidden("#critical-warn") and page.is_visible("#downtime-fld"), "grounded: warning cleared, downtime field shown")
        page.fill("#f-downtime", "6")
        hmic = page.locator(".vfield[data-field='observaciones'] .mic")
        hmic.scroll_into_view_if_needed()
        box = hmic.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(7000)
        page.mouse.up()
        page.wait_for_function("document.querySelector('#f-obs').value.length > 0", timeout=90000)
        print(f"       hold-dictation  -> {page.input_value('#f-obs')!r}")
        check("manguera" in page.input_value("#f-obs").lower(), "press-and-hold dictation fills the remarks")
        page.wait_for_timeout(1200)  # let the autosave run
        page.screenshot(path=str(SHOTS / "2_inspeccion.png"), full_page=True)

        page.click("#btn-confirm")
        page.wait_for_selector("#done:not([hidden])", timeout=30000)
        done = page.inner_text("#done")
        print("       " + page.inner_text("#done-summary")[:160])
        check("Guardado en Excel" in done and "41 ítems: 39 OK, 1 a observar, 1 con falla, 0 N/A" in done and "MOTOR 5 L" in done,
              "saved: Excel + counts + top-up shown")
        page.screenshot(path=str(SHOTS / "3_guardado.png"), full_page=True)

        xlsx = runtime / "Registros_Mantencion.xlsx"
        wb = load_workbook(xlsx)
        reg = list(wb["Registros"].values)
        rec = dict(zip(reg[0], reg[1])) if len(reg) > 1 else {}
        check(rec.get("tecnico") == "Técnico UI" and rec.get("horometro") == 41250 and rec.get("horas_fuera_servicio") == 6
              and rec.get("estado_equipo") == "Fuera de servicio – emergencia (falla)", "Excel: record with grounded status and downtime")
        insp = list(wb["Inspeccion"].values)
        check(len(insp) == 42 and any(r_[insp[0].index("item_id")] == "neumaticos" and r_[insp[0].index("posicion")] == "Delantera izquierda"
                                      for r_ in insp[1:]), "Excel: 41 checklist rows with position")
        grab = [dict(zip(list(wb["Grabaciones"].values)[0], v)) for v in list(wb["Grabaciones"].values)[1:]]
        check(any(g["modelo"] == "Wispr Flow" and "despacho" in g["transcripcion_original"] for g in grab),
              "Excel: the Wispr dictation is logged")

        # second record: corrective job with a compartment line and a line top-up
        page.click("#btn-new")
        page.select_option("#f-equipo", "EQ-994K-02")
        page.fill("#f-horometro", "21900")
        page.click("#f-actividad .tile[data-value='inspeccion_visual_diaria']")
        page.click("#item-frenos .status button[data-v='ok']")
        page.wait_for_timeout(400)
        page.once("dialog", lambda d: d.accept())
        page.click("#f-actividad .tile[data-value='mantenimiento_correctivo']")
        page.wait_for_selector("#card-orden:not([hidden])")
        check(page.is_hidden("#card-lista") and page.is_visible("#card-detalle") and page.input_value("#f-clase") == "Z003",
              "switching to corrective shows the work order (Z003 suggested) and compartment lines")
        page.click("#btn-add-line")
        line = page.locator("#lines .line").last
        line.locator(".line-comp").select_option("SISTEMA HIDRAULICO")
        line.locator(".result button[data-value='sin_hallazgos']").click()
        line.locator(".add-topup").click()
        page.wait_for_selector("#lines .topup-row")
        line.locator(".topup-row .qty").fill("45")
        page.click("#f-estado button[data-value='operativo']")
        page.wait_for_timeout(1000)
        page.click("#btn-confirm")
        page.wait_for_selector("#done:not([hidden])", timeout=30000)
        check("SISTEMA HIDRAULICO 45 L" in page.inner_text("#done-summary"), "corrective job saved with its line top-up")

        page.click("#chip-mic")
        page.wait_for_selector("#mic-dialog[open]")
        page.wait_for_timeout(800)
        check(page.locator("#mic-device option").count() >= 1, "microphone dialog lists input devices")
        page.click("#mic-dialog button[value='close']")

        page.click(".tab[data-view='view-history']")
        page.wait_for_selector("#history-body tr td")
        check(page.locator("#history-body tr").count() == 2, "history shows the 2 saved records")
        page.screenshot(path=str(SHOTS / "4_historial.png"))

        # English screen (the app's own translation, not the browser's)
        page.click(".lang button[data-lang='en']")
        page.wait_for_selector("#f-actividad .tile")
        page.wait_for_timeout(500)
        page.click("#f-actividad .tile[data-value='inspeccion_visual_diaria']")
        page.wait_for_timeout(300)
        estados = page.inner_text("#f-estado")
        check("Grounded – emergency (breakdown)" in estados and "Grounded – scheduled maintenance" in estados,
              "English: machine status 'Grounded – emergency / scheduled'")
        page.click("#f-estado button[data-value='fuera_servicio_programado']")
        check("Downtime" in page.inner_text("#downtime-fld") and "Dictate" in page.inner_text(".vfield[data-field='tecnico'] .mic"),
              "English: 'Downtime (hours grounded)' and 'Dictate'")
        body = page.inner_text("body").lower()
        check("arrested" not in body and "detention" not in body and "detenido" not in body, "no 'arrested' / 'detention' wording left")
        page.screenshot(path=str(SHOTS / "5_english.png"), full_page=True)

        phone = browser.new_page(viewport={"width": 390, "height": 844})
        phone.goto(BASE)
        phone.wait_for_selector("#f-actividad .tile")
        phone.click("#f-actividad .tile[data-value='inspeccion_visual_diaria']")
        phone.wait_for_timeout(1000)
        overflow = phone.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        check(not overflow, "no horizontal scrolling at phone width")
        phone.screenshot(path=str(SHOTS / "6_telefono.png"), full_page=False)

        check(not errors, f"no JavaScript errors ({errors[:3]})")
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(runtime, ignore_errors=True)

print(f"\nscreenshots: {SHOTS}")
print(f"{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
sys.exit(1 if failures else 0)
