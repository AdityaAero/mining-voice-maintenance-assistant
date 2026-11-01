"use strict";
// Registro de mantención por voz — client. Talks only to the local server (127.0.0.1). Texts: i18n.js.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  catalog: null,
  eventId: null,
  event: null,            // evento row as last loaded
  readOnly: false,
  modelReady: false,
  wisprOpen: false,
  engine: "whisper",      // dictation engine of the Dictar buttons: "whisper" (local) or "wispr" (Wispr Flow)
  creating: null,         // promise while the draft is being created
  pendingEvent: {},       // event fields waiting to be saved
  eventTimer: null,
  lines: new Map(),       // detalle_id -> {el, pending, timer}
  items: new Map(),       // checklist item_id -> {el, it, a: answer, pending, timer}
  topups: new Map(),      // relleno_id -> {el, pending, timer}
  saving: new Set(),
  lastTarget: null,       // last focused dictation field
  activeMic: null,        // MicField currently dictating
  wisprKeys: null,        // Ctrl+Win pressed by hand on a field: {mic, t0, up}
  mic: { stream: null, ctx: null, deviceId: null, label: "" },
};
const STATUS_CODES = ["ok", "observacion", "falla", "na"];
const NUMERIC = new Set(["horometro", "horas_fuera_servicio"]);

// ---------------- helpers ----------------
async function api(path, opts = {}) {
  const headers = { "X-Lang": LANG, ...(opts.headers || {}) };
  let body = opts.body;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.json); }
  const res = await fetch(path, { method: opts.method || "GET", headers, body, keepalive: !!opts.keepalive });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data?.detail;
    const errores = data?.errores || [typeof detail === "string" ? detail : `Error ${res.status}`];
    throw Object.assign(new Error(errores.join("\n")), { errores, status: res.status });
  }
  return data;
}
const wisprCall = (accion, keepalive = false) => api(`/api/wispr/${accion}`, { method: "POST", headers: { "X-Registro": "1" }, keepalive });

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null && c !== false) n.append(c);
  return n;
}
function icon(id) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

const storage = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const pad = (n) => String(n).padStart(2, "0");
const mmss = (s) => `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`;
const locale = () => (LANG === "en" ? "en-GB" : "es-CL");
const localNow = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const fmtDate = (s) => (s ? new Date(s).toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" }) : "");
const fmtNum = (n) => Number(n).toLocaleString(locale());
const setDraftState = (text) => ($("#draft-state").textContent = text);
const savedNow = () => setDraftState(t("draft_saved", { t: new Date().toLocaleTimeString(locale()) }));

function highlight(text) {
  const esc = text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  return esc
    .replace(/\b(no|sin|ni|nunca|ning[uú]n[oa]?|tampoco|not|without|none|never)\b/gi, '<mark class="neg">$1</mark>')
    .replace(/\b(\d+(?:[.,]\d+)?)\b/g, '<mark class="num">$1</mark>')
    .replace(/\b(izquierd[oa]s?|derech[oa]s?|delanter[oa]s?|traser[oa]s?|left|right|front|rear)\b/gi, '<mark class="pos">$1</mark>');
}

// Spoken or typed numbers: Spanish uses "." for thousands and "," for decimals ("41.200 horas" -> 41200).
function parseSpokenNumber(text) {
  const m = String(text).match(/\d+(?:[.,\s]\d+)*/);
  if (!m) return null;
  let s = m[0].replace(/\s/g, "");
  if (/^\d{1,3}([.,]\d{3})+$/.test(s)) s = s.replace(/[.,]/g, "");
  else s = s.replace(",", ".");
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}
const numberOrRaw = (v) => (String(v).trim() === "" ? "" : parseSpokenNumber(v) ?? v);

function makeSeg(container, options, value, onChange) {
  container.replaceChildren();
  for (const [val, lab] of Object.entries(options)) {
    const b = el("button", { type: "button", role: "radio", "data-value": val, "aria-checked": String(val === value) }, L(lab));
    b.addEventListener("click", () => {
      if (state.readOnly) return;
      $$("button", container).forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      onChange(val);
    });
    container.append(b);
  }
}
const segValue = (container) => $('button[aria-checked="true"]', container)?.dataset.value || null;
const setSeg = (container, value) => $$("button", container).forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === value)));

// ---------------- status chips ----------------
async function pollStatus() {
  try {
    renderStatus(await api("/api/estado"));
    setTimeout(pollStatus, state.modelReady ? 8000 : 1500);
  } catch {
    const chip = $("#chip-model");
    chip.className = "chip err";
    chip.querySelector("span").textContent = t("chip_server_down");
    setTimeout(pollStatus, 3000);
  }
}

function renderStatus(s) {
  const m = s.modelo;
  state.modelReady = m.state === "listo";
  const chip = $("#chip-model");
  chip.className = "chip " + (m.state === "listo" ? "ok" : m.state === "error" ? "err" : "");
  chip.querySelector("span").textContent = t(m.state === "listo" ? "chip_model_ready" : m.state === "error" ? "chip_model_error" : "chip_model_loading");
  chip.title = m.state === "error" ? m.error : `${m.model} — ${m.engine}`;
  const x = s.excel;
  const ex = $("#chip-excel");
  ex.className = "chip " + (x.pendiente && x.ultimo_error ? "warn" : x.ultima_escritura ? "ok" : "");
  ex.querySelector("span").textContent = x.pendiente && x.ultimo_error ? t("chip_excel_pending")
    : x.ultima_escritura ? t("chip_excel_ok", { n: x.registros ?? 0 }) : t("chip_excel_empty");
  ex.title = `${x.ultimo_error ? x.ultimo_error + "\n" : ""}${x.ruta}\n${t("chip_excel_title")}`;
  $("#excel-path").textContent = t("workbook", { x: x.ruta });
  const sap = $("#chip-sap");
  sap.textContent = t("chip_sap", { n: s.pendientes_sap });
  sap.className = "chip " + (s.pendientes_sap ? "warn" : "");
  if (state.wisprOpen !== s.wispr.abierto) { state.wisprOpen = s.wispr.abierto; renderEngine(); }
}
const refreshStatus = () => api("/api/estado").then(renderStatus).catch(() => {});

async function openExcel() {
  try { await api("/api/excel/abrir", { method: "POST" }); } catch (e) { alert(e.message); }
}

// ---------------- dictation engine ----------------
function setEngine(engine) {
  if (state.activeMic) return;
  state.engine = engine === "wispr" ? "wispr" : "whisper";
  storage.set("engine", state.engine);
  renderEngine();
}

function renderEngine() {
  $$("#dictbar .engine button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.engine === state.engine)));
  $("#wispr-sub").textContent = state.wisprOpen ? t("eng_wispr_sub") : t("eng_wispr_closed");
  $('#dictbar button[data-engine="wispr"]').classList.toggle("off", !state.wisprOpen);
  $("#dictbar").classList.toggle("wispr", state.engine === "wispr");
  $("#dict-help").innerHTML = t(state.engine === "wispr" ? "dict_help_wispr" : "dict_help");
  $$(".vfield").forEach((v) => { if (v.mic?.status === "idle") v.mic.idle(); });
}

// ---------------- microphone (local Whisper) ----------------
function micErrorText(e) {
  if (e.name === "NotAllowedError") return t("mic_blocked");
  if (e.name === "NotFoundError" || e.name === "OverconstrainedError") return t("mic_notfound");
  if (e.name === "NotReadableError") return t("mic_busy");
  return t("mic_other", { x: e.message });
}

async function ensureMic() {
  const want = state.mic.deviceId || null;
  const track = state.mic.stream?.getAudioTracks()[0];
  if (track && track.readyState === "live" && (!want || track.getSettings().deviceId === want)) return state.mic.stream;
  state.mic.stream?.getTracks().forEach((tr) => tr.stop());
  state.mic.stream = await navigator.mediaDevices.getUserMedia({
    audio: { deviceId: want ? { exact: want } : undefined, channelCount: 1,
             echoCancellation: false, noiseSuppression: false, autoGainControl: true },
  });
  state.mic.label = state.mic.stream.getAudioTracks()[0]?.label || "";
  if (!state.mic.ctx) state.mic.ctx = new AudioContext();
  if (state.mic.ctx.state === "suspended") await state.mic.ctx.resume();
  const chip = $("#chip-mic");
  chip.className = "chip ok";
  chip.querySelector("span").textContent = t("chip_mic_named", { x: state.mic.label.replace(/\s*\(.*\)$/, "") });
  return state.mic.stream;
}

function micFailed(e) {
  const chip = $("#chip-mic");
  chip.className = "chip err";
  chip.querySelector("span").textContent = t("chip_mic_none");
  return micErrorText(e);
}

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const x = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

// Browser recording (webm/opus) -> 16 kHz mono 16-bit WAV, which is what Whisper expects.
async function toWav16k(blob) {
  const decoded = await state.mic.ctx.decodeAudioData(await blob.arrayBuffer());
  const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * 16000)), 16000);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  return encodeWav((await off.startRendering()).getChannelData(0), 16000);
}

// Records from the shared stream; calls onLevel(0..1) while recording.
class Recorder {
  async start(onLevel) {
    const stream = await ensureMic();
    this.chunks = [];
    this.rec = new MediaRecorder(stream);
    this.rec.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.rec.start();
    this.t0 = performance.now();
    const analyser = state.mic.ctx.createAnalyser();
    analyser.fftSize = 1024;
    this.src = state.mic.ctx.createMediaStreamSource(stream);
    this.src.connect(analyser);
    const data = new Float32Array(analyser.fftSize);
    const tick = () => {
      analyser.getFloatTimeDomainData(data);
      onLevel(Math.min(1, Math.sqrt(data.reduce((a, x) => a + x * x, 0) / data.length) * 4));
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }
  elapsed() { return (performance.now() - this.t0) / 1000; }
  async stop() {
    cancelAnimationFrame(this.raf);
    this.src?.disconnect();
    await new Promise((res) => { this.rec.onstop = res; this.rec.stop(); });
    return new Blob(this.chunks, { type: this.rec.mimeType });
  }
}

// Replay of a saved dictation: play/pause button, seek bar and time. Only one recording plays at a time.
class Playback {
  static current = null;
  static pauseAll() { Playback.current?.audio.pause(); }

  constructor(src) {
    this.audio = new Audio(src);
    this.audio.preload = "metadata";
    this.btn = el("button", { type: "button", class: "play", onclick: () => this.toggle() });
    this.seek = el("input", { type: "range", class: "seek", min: "0", max: "0", step: "0.1", value: "0", "aria-label": t("audio_pos") });
    this.time = el("span", { class: "time" });
    this.node = el("span", { class: "player" }, this.btn, this.seek, this.time);
    this.seek.addEventListener("input", () => { this.audio.currentTime = Number(this.seek.value); this.render(); });
    this.audio.addEventListener("ended", () => { this.audio.currentTime = 0; });   // next press starts from the beginning
    for (const ev of ["loadedmetadata", "timeupdate", "play", "pause", "ended", "error"]) this.audio.addEventListener(ev, () => this.render());
    this.render();
  }

  toggle() {
    if (!this.audio.paused) { this.audio.pause(); return; }
    if (Playback.current !== this) Playback.pauseAll();
    Playback.current = this;
    this.audio.play().catch(() => (this.time.textContent = t("audio_play_error")));
  }

  render() {
    const a = this.audio;
    const dur = Number.isFinite(a.duration) ? a.duration : 0;
    this.btn.textContent = !a.paused ? t("pause") : a.currentTime > 0 ? t("resume") : t("listen");
    this.btn.setAttribute("aria-pressed", String(!a.paused));
    this.seek.max = String(dur);
    this.seek.value = String(a.currentTime);
    this.seek.disabled = !dur;
    if (a.error) this.time.textContent = t("audio_missing");
    else this.time.textContent = dur ? `${mmss(a.currentTime)} / ${mmss(dur)}` : "";
  }

  dispose() {
    this.audio.pause();
    if (Playback.current === this) Playback.current = null;
  }
}

// A text/number field with its own dictation button. Click = start/stop; holding works as push-to-talk.
// The button uses the engine chosen at the top: local Whisper (records here, transcribes on this computer)
// or Wispr Flow (the app holds Wispr's Ctrl+Win shortcut; Wispr types into the focused field).
class MicField {
  constructor(vfield, target = {}) {
    this.vfield = vfield;
    this.detalleId = target.detalleId || null;
    this.itemId = target.itemId || null;
    this.labelText = target.label || null;
    this.field = vfield.dataset.field;
    this.kind = vfield.dataset.kind || "text";
    this.input = $("input, textarea", vfield);
    this.status = "idle";
    this.btn = el("button", { type: "button", class: "mic", title: t("dictar_title") });
    this.level = el("i", { class: "level" });
    this.label = el("span", {});
    this.btn.append(icon("i-mic"), this.label, this.level);
    $(".vrow", vfield).append(this.btn);
    this.raw = el("div", { class: "raw", hidden: "" });
    this.msgBox = el("p", { class: "field-msg", hidden: "" });
    vfield.append(this.raw, this.msgBox);
    vfield.mic = this;
    this.idle();

    this.btn.addEventListener("pointerdown", (e) => { e.preventDefault(); this.press(); });
    this.btn.addEventListener("pointerup", () => this.release());
    this.btn.addEventListener("pointercancel", () => this.release());
    this.btn.addEventListener("contextmenu", (e) => e.preventDefault());
    this.btn.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); this.press(); } });
    this.btn.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); this.release(); } });
    this.input.addEventListener("focus", () => setTarget(vfield));
    this.input.addEventListener("paste", (e) => this.onPaste(e));
  }

  get name() { return this.labelText || t(this.vfield.dataset.labelKey) || this.field; }

  setButton(status, text, iconId) {
    this.status = status;
    this.btn.classList.toggle("recording", status === "recording");
    this.btn.classList.toggle("busy", status === "busy");
    this.btn.firstChild.replaceWith(icon(iconId));
    this.label.textContent = text;
  }

  idle() {
    const wispr = state.engine === "wispr";
    this.btn.classList.toggle("wispr", wispr);
    this.setButton("idle", wispr ? t("wispr_btn") : t("dictar"), wispr ? "i-wave" : "i-mic");
  }

  msg(text) { this.msgBox.textContent = text || ""; this.msgBox.hidden = !text; }

  focusInput() {
    this.input.focus({ preventScroll: true });
    const n = this.input.value.length;
    try { this.input.setSelectionRange(n, n); } catch { /* not a text field */ }
  }

  async press(engine = state.engine) {
    if (this.status === "recording") { if (this.mode === "toggle") this.stop(); return; }
    if (this.status !== "idle" || state.readOnly) return;
    if (state.activeMic && state.activeMic !== this) { this.msg(t("already_rec", { x: state.activeMic.name })); return; }
    this.engine = engine;
    if (engine === "whisper" && !state.modelReady) { this.msg(t("model_loading")); return; }
    if (engine === "wispr" && !state.wisprOpen) { this.msg(t("wispr_closed")); return; }
    this.msg("");
    Playback.pauseAll();   // the speakers would be picked up by the microphone
    setTarget(this.vfield);
    this.held = true;
    this.started = false;
    this.mode = "pending";
    this.pressAt = performance.now();
    state.activeMic = this;
    this.btn.classList.toggle("wispr", engine === "wispr");
    this.setButton("recording", t("starting"), "i-stop");
    try {
      if (engine === "wispr") await this.wisprStart(); else await this.whisperStart();
    } catch (e) {
      state.activeMic = null;
      this.idle();
      this.msg(engine === "wispr" ? e.message : micFailed(e));
      return;
    }
    if (!this.held) this.mode = "toggle";   // it was a click, not a hold
  }

  release() {
    this.held = false;
    if (this.status === "recording" && this.mode === "pending" && this.started) {
      if (performance.now() - this.pressAt > 600) this.stop(); else this.mode = "toggle";
    }
  }

  async whisperStart() {
    this.recorder = new Recorder();
    await this.recorder.start((lv) => {
      this.level.style.width = `${lv * 100}%`;
      const s = this.recorder.elapsed();
      this.label.textContent = t("stop_t", { t: mmss(s) });
      if (s > 90) this.stop();
    });
    this.started = true;
  }

  async wisprStart() {
    this.focusInput();   // Wispr types into the field that has the keyboard focus
    await wisprCall("presionar");
    this.t0 = performance.now();
    this.started = true;
    this.timer = setInterval(() => {
      const s = (performance.now() - this.t0) / 1000;
      this.label.textContent = t("wispr_listening", { t: mmss(s) });
      if (s > 90) this.stop();
    }, 250);
  }

  async cancel() {
    if (this.status !== "recording") return;
    if (this.engine === "wispr") { clearInterval(this.timer); wisprCall("cancelar").catch(() => {}); }
    else await this.recorder.stop();
    state.activeMic = null;
    this.level.style.width = "0";
    this.idle();
    this.msg(t("rec_cancelled"));
  }

  async stop() {
    if (this.status !== "recording") return;
    this.status = "stopping";
    if (this.engine === "wispr") return this.wisprStop();
    const seconds = this.recorder.elapsed();
    const blob = await this.recorder.stop();
    state.activeMic = null;
    this.level.style.width = "0";
    if (seconds < 0.5) { this.idle(); this.msg(t("rec_short")); return; }
    this.setButton("busy", t("transcribing"), "i-mic");
    try {
      const wav = await toWav16k(blob);
      const id = await ensureDraft();
      const q = new URLSearchParams({ campo: this.field, idioma: LANG });
      if (this.detalleId) q.set("detalle_id", this.detalleId);
      if (this.itemId) q.set("item_id", this.itemId);
      const res = await api(`/api/eventos/${id}/grabaciones?${q}`, { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
      if (!res.text) this.msg(t("no_speech"));
      else this.apply(res);
    } catch (e) {
      this.msg(t("transcribe_error", { x: e.message }));
    } finally {
      this.idle();
    }
  }

  async wisprStop() {
    clearInterval(this.timer);
    const seconds = (performance.now() - this.t0) / 1000;
    this.setButton("busy", t("wispr_typing"), "i-wave");
    this.focusInput();
    const text = new Promise((resolve) => {
      this.gotPaste = resolve;
      setTimeout(() => resolve(null), 15000);
    });
    try {
      await wisprCall("soltar");
    } catch (e) {
      this.gotPaste(null);
      this.msg(e.message);
    }
    const typed = await text;
    this.gotPaste = null;
    state.activeMic = null;
    this.idle();
    if (typed) this.applyWispr(typed, seconds);
    else if (!this.msgBox.textContent) this.msg(t("wispr_nothing"));
  }

  // Wispr Flow delivers its text as a paste (Ctrl+V) into the focused field.
  onPaste(e) {
    const text = e.clipboardData?.getData("text") ?? "";
    const k = state.wisprKeys;
    const byHand = k && k.mic === this && performance.now() - (k.up ?? performance.now()) < 20000;
    if (!text.trim() || (!this.gotPaste && !byHand)) return;   // an ordinary paste
    e.preventDefault();
    if (this.gotPaste) { this.gotPaste(text); return; }
    state.wisprKeys = null;
    this.applyWispr(text, ((k.up ?? performance.now()) - k.t0) / 1000);
  }

  async applyWispr(text, seconds) {
    if (state.readOnly) return;
    this.apply({ text, wispr: true });
    try {
      const id = await ensureDraft();
      await api(`/api/eventos/${id}/dictados`, { method: "POST", json: {
        campo: this.field, detalle_id: this.detalleId, item_id: this.itemId, texto: text,
        duracion_s: Math.round(seconds * 10) / 10 } });
    } catch { /* the text is already in the field and saved with it; only the dictation log failed */ }
  }

  apply(res) {
    const text = res.text.trim();
    if (this.kind === "number") {
      const n = parseSpokenNumber(text);
      if (n == null) { this.msg(t("no_number", { x: text })); this.showRaw(res); return; }
      this.input.value = String(n);
    } else if (this.kind === "digits") {
      const digits = text.replace(/\D/g, "");
      if (!digits) { this.msg(t("no_digits", { x: text })); this.showRaw(res); return; }
      this.input.value = digits;
    } else {
      this.input.value = this.input.value.trim() ? `${this.input.value.trim()} ${text}` : text;
    }
    this.showRaw(res);
    this.input.dispatchEvent(new Event("input", { bubbles: true }));
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  showRaw(res) {
    const q = el("q");
    q.innerHTML = highlight(res.text ?? res.transcripcion_original ?? "");
    const wispr = res.wispr || (res.modelo || "").startsWith("Wispr");
    this.player?.dispose();
    this.player = !wispr && res.grabacion_id && res.con_audio !== 0 ? new Playback(`/api/grabaciones/${res.grabacion_id}/audio`) : null;
    this.raw.replaceChildren(
      el("span", {}, t("raw_label"), q),
      this.player?.node,
      el("span", {}, wispr ? t("raw_wispr")
        : t("raw_stats", { a: res.audio_s ?? res.duracion_s ?? "?", p: res.processing_s ?? res.procesamiento_s ?? "?" })),
    );
    this.raw.hidden = false;
  }
}

function setTarget(vfield) {
  if (state.lastTarget && state.lastTarget !== vfield) state.lastTarget.classList.remove("target");
  state.lastTarget = vfield;
  vfield.classList.add("target");
}

// F2 = local Whisper into the selected field (hold = push-to-talk, tap = start/stop). Ctrl+Win = Wispr Flow
// (its own shortcut; the app only notes which field the text is for). Esc cancels.
document.addEventListener("keydown", (e) => {
  if (e.key === "F2" && !e.repeat) {
    e.preventDefault();
    const target = document.activeElement?.closest?.(".vfield") || state.lastTarget;
    if (target?.mic) target.mic.press("whisper");
  } else if (e.key === "Escape" && state.activeMic) {
    state.activeMic.cancel();
  } else if (!e.repeat && ((e.key === "Meta" && e.ctrlKey) || (e.key === "Control" && e.metaKey)) && !state.activeMic) {
    const v = document.activeElement?.closest?.(".vfield");
    state.wisprKeys = v?.mic ? { mic: v.mic, t0: performance.now(), up: null } : null;
  }
});
document.addEventListener("keyup", (e) => {
  if (e.key === "F2" && state.activeMic?.engine === "whisper") state.activeMic.release();
  if ((e.key === "Meta" || e.key === "Control") && state.wisprKeys && !state.wisprKeys.up) state.wisprKeys.up = performance.now();
});
// never leave Wispr's shortcut held if the window is closed mid-dictation
window.addEventListener("pagehide", () => { if (state.activeMic?.engine === "wispr") wisprCall("soltar", true).catch(() => {}); });

// ---------------- microphone dialog ----------------
let meterRaf = 0;
async function openMicDialog() {
  const dlg = $("#mic-dialog");
  dlg.showModal();
  $("#mic-test-result").textContent = "";
  try {
    await ensureMic();
    $("#mic-help").textContent = t("mic_help");
  } catch (e) {
    $("#mic-help").textContent = micFailed(e);
    return;
  }
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  const sel = $("#mic-device");
  const current = state.mic.stream.getAudioTracks()[0]?.getSettings().deviceId;
  sel.replaceChildren(...devices.map((d) => el("option", { value: d.deviceId }, d.label || t("mic_title"))));
  sel.value = current || "";
  runMeter();
}

function runMeter() {
  cancelAnimationFrame(meterRaf);
  const analyser = state.mic.ctx.createAnalyser();
  analyser.fftSize = 1024;
  const src = state.mic.ctx.createMediaStreamSource(state.mic.stream);
  src.connect(analyser);
  const data = new Float32Array(analyser.fftSize);
  const tick = () => {
    if (!$("#mic-dialog").open) { src.disconnect(); return; }
    analyser.getFloatTimeDomainData(data);
    const lv = Math.min(1, Math.sqrt(data.reduce((a, x) => a + x * x, 0) / data.length) * 4);
    $("#mic-meter").style.width = `${lv * 100}%`;
    meterRaf = requestAnimationFrame(tick);
  };
  tick();
}

async function changeMicDevice() {
  state.mic.deviceId = $("#mic-device").value || null;
  storage.set("micDevice", state.mic.deviceId);
  try { await ensureMic(); runMeter(); $("#mic-help").textContent = t("mic_changed"); }
  catch (e) { $("#mic-help").textContent = micFailed(e); }
}

let testRec = null;
async function toggleMicTest() {
  const btn = $("#mic-test");
  const out = $("#mic-test-result");
  if (!testRec) {
    if (!state.modelReady) { out.textContent = t("model_loading"); return; }
    testRec = new Recorder();
    try { await testRec.start(() => {}); } catch (e) { testRec = null; out.textContent = micFailed(e); return; }
    btn.textContent = t("mic_test_stop");
    btn.classList.add("mic", "recording");
    out.textContent = t("mic_test_rec");
    return;
  }
  const r = testRec;
  testRec = null;
  btn.textContent = t("mic_test");
  btn.classList.remove("mic", "recording");
  out.textContent = t("transcribing");
  try {
    const wav = await toWav16k(await r.stop());
    const res = await api(`/api/prueba-microfono?idioma=${LANG}`, { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    out.textContent = res.text ? `«${res.text}» (${res.processing_s} s)` : t("mic_test_none");
  } catch (e) {
    out.textContent = t("err_x", { x: e.message });
  }
}

// ---------------- record form ----------------
const FIELD_INPUTS = {
  fecha_trabajo: "#f-fecha", equipo_id: "#f-equipo", tecnico: "#f-tecnico", horometro: "#f-horometro",
  clase_orden: "#f-clase", orden_trabajo: "#f-ot", aviso: "#f-aviso", descripcion_trabajo: "#f-desc",
  horas_fuera_servicio: "#f-downtime", observaciones: "#f-obs",
};
const FIELD_SEGS = { turno: "#f-turno", actividad: "#f-actividad", estado_equipo: "#f-estado" };
const isInspection = (act) => act === state.catalog.inspeccion;
const nItems = () => state.catalog.lista.secciones.reduce((n, s) => n + s.items.length, 0);

function initForm() {
  const c = state.catalog;
  $("#f-equipo").replaceChildren(el("option", { value: "" }, t("sel_equipo")),
    ...c.equipos.map((e) => el("option", { value: e.equipo_id }, t("equipo_opt", { f: e.flota, id: e.equipo_id }))));
  $("#f-clase").replaceChildren(el("option", { value: "" }, t("no_clase")),
    ...c.clases_orden.map((k) => el("option", { value: k.clase_orden, title: t("clase_title") }, `${k.clase_orden} · ${k.descripcion}`)));
  $("#tecnicos").replaceChildren(...c.tecnicos.map((x) => el("option", { value: x })));
  makeSeg($("#f-turno"), c.turnos, null, (v) => eventChanged("turno", v));
  makeSeg($("#f-estado"), c.estados_equipo, null, (v) => { eventChanged("estado_equipo", v); updateResultCard(); });
  renderActivityTiles();

  for (const [field, sel] of Object.entries(FIELD_INPUTS)) {
    const input = $(sel);
    input.addEventListener(input.tagName === "SELECT" ? "change" : "input", () => {
      if (field === "equipo_id") return onEquipo();
      eventChanged(field, NUMERIC.has(field) ? numberOrRaw(input.value) : input.value);
      if (field === "horometro") renderHorometroInfo();
    });
    if (NUMERIC.has(field)) input.addEventListener("change", () => {   // tidy typed numbers: "41.250" -> 41250
      const n = parseSpokenNumber(input.value);
      if (n != null && String(n) !== input.value) input.value = String(n);
    });
  }
  $$("#record-form .card .vfield").forEach((v) => { if (!v.closest("#lines, #checklist")) new MicField(v); });
  $("#btn-ahora").addEventListener("click", () => { $("#f-fecha").value = localNow(); eventChanged("fecha_trabajo", $("#f-fecha").value); });
  $("#btn-add-line").addEventListener("click", () => addLine());
  $("#btn-add-all").addEventListener("click", addAllLines);
  $("#btn-confirm").addEventListener("click", confirmRecord);
  $("#btn-discard").addEventListener("click", discardDraft);
  $("#btn-new").addEventListener("click", newRecord);
  $("#btn-next").addEventListener("click", goNextPending);
  $("#lista-title").textContent = L(c.lista.titulo);
}

function renderActivityTiles() {
  const icons = { inspeccion_visual_diaria: "i-clip", mantenimiento_preventivo: "i-cal", mantenimiento_correctivo: "i-wrench", reparacion_mayor: "i-gear" };
  const box = $("#f-actividad");
  box.replaceChildren();
  for (const [code, lab] of Object.entries(state.catalog.actividades)) {
    const b = el("button", { type: "button", role: "radio", class: "tile", "data-value": code, "aria-checked": "false" },
      icon(icons[code] || "i-clip"), el("span", { class: "tile-text" }, el("b", {}, L(lab)), el("small", {}, t(`act_sub_${code}`, { n: nItems() }))));
    b.addEventListener("click", () => onActividad(code));
    box.append(b);
  }
}

function readForm() {
  const v = {};
  for (const [f, sel] of Object.entries(FIELD_INPUTS)) v[f] = NUMERIC.has(f) ? numberOrRaw($(sel).value) : $(sel).value;
  for (const [f, sel] of Object.entries(FIELD_SEGS)) v[f] = segValue($(sel));
  return v;
}

function setForm(ev) {
  for (const [f, sel] of Object.entries(FIELD_INPUTS)) $(sel).value = ev[f] ?? "";
  for (const [f, sel] of Object.entries(FIELD_SEGS)) setSeg($(sel), ev[f]);
  state.prevEquipo = ev.equipo_id || "";
  renderEquipoInfo();
}

function resetFormView() {
  Playback.pauseAll();
  $$(".raw, .field-msg").forEach((n) => (n.hidden = true));
  $("#confirm-errors").hidden = true;
  $("#done").hidden = true;
  $("#record-form").hidden = false;
  state.topups.clear();
}

function newRecord() {
  state.eventId = null;
  state.event = null;
  state.readOnly = false;
  state.pendingEvent = {};
  storage.set("borrador", null);
  const hour = new Date().getHours();
  setForm({ fecha_trabajo: localNow(), turno: hour >= 8 && hour < 20 ? "dia" : "noche", tecnico: storage.get("tecnico") || "" });
  // defaults count as changes so they are saved with the draft
  state.pendingEvent = { fecha_trabajo: $("#f-fecha").value, turno: segValue($("#f-turno")), tecnico: $("#f-tecnico").value };
  resetFormView();
  renderLines([]);
  renderChecklist([]);
  applyReadOnly();
  setDraftState(t("draft_hint"));
  showView("view-record");
  window.scrollTo(0, 0);
}

async function loadRecord(id) {
  const data = await api(`/api/eventos/${id}`);
  state.eventId = id;
  state.event = data.evento;
  state.readOnly = data.evento.estado !== "borrador";
  state.pendingEvent = {};
  storage.set("borrador", state.readOnly ? storage.get("borrador") : id);
  setForm(data.evento);
  resetFormView();
  renderLines(data.detalles);
  renderChecklist(data.inspeccion);
  for (const r of data.rellenos) {
    const owner = r.item_id ? state.items.get(r.item_id)?.el : state.lines.get(r.detalle_id)?.el;
    const rows = owner && $(".topup-rows", owner);
    if (rows) renderTopupRow(rows, r);
  }
  // latest raw transcript per field
  for (const g of data.grabaciones) {
    const scope = g.item_id ? state.items.get(g.item_id)?.el : g.detalle_id ? state.lines.get(g.detalle_id)?.el : $("#record-form");
    const v = scope && $(`.vfield[data-field="${g.campo}"]`, scope);
    if (v?.mic && g.transcripcion_original) v.mic.showRaw(g);
  }
  applyReadOnly();
  setDraftState(state.readOnly ? t("record_ro") : t("draft_restored"));
  showView("view-record");
  window.scrollTo(0, 0);
}

function applyReadOnly() {
  const ro = state.readOnly;
  $$("#record-form input, #record-form select, #record-form textarea").forEach((n) => (n.disabled = ro));
  $$("#record-form .mic, #record-form .line-del, #record-form .add-topup, #record-form .topup-del, #record-form .ok-rest, #record-form .item-more, " +
     "#btn-ahora, #btn-add-line, #btn-add-all, #btn-discard, #btn-confirm, #dictbar").forEach((n) => (n.hidden = ro));
  $$("#record-form .status button, #record-form .defects button, #f-actividad button").forEach((b) => (b.disabled = ro));
  showSections();
}

// which cards are on screen depends on what is being recorded
function showSections() {
  const act = segValue($("#f-actividad"));
  const insp = isInspection(act);
  const answered = [...state.items.values()].some((x) => x.a.estado);
  // a saved record shows what it holds (records from before the checklist have compartment lines)
  const showList = state.readOnly ? answered : insp;
  const showLines = state.readOnly ? state.lines.size > 0 && !answered : !!act && !insp;
  $("#card-lista").hidden = !showList;
  $("#card-orden").hidden = !act || insp;
  $("#card-detalle").hidden = !showLines;
  $("#card-resultado").hidden = !act;
  $("#pick-first").hidden = !!act || state.readOnly;
  $$("#record-form > .card:not([hidden]) .step").forEach((s, i) => (s.textContent = String(i + 1)));
  updateResultCard();
  updateProgress();
}

// ---- event field changes / autosave ----
function ensureDraft() {
  if (state.eventId) return Promise.resolve(state.eventId);
  if (!state.creating) {
    state.creating = api("/api/eventos", { method: "POST", json: {} }).then((r) => {
      state.eventId = r.evento_id;
      storage.set("borrador", r.evento_id);
      state.pendingEvent = { ...readForm(), ...state.pendingEvent };
      clearTimeout(state.eventTimer);
      state.eventTimer = setTimeout(saveEvent, 0);   // store the defaults (date, shift, technician) right away
      return r.evento_id;
    }).finally(() => { state.creating = null; });
  }
  return state.creating;
}

function eventChanged(field, value) {
  if (state.readOnly) return;
  state.pendingEvent[field] = value;
  if (field === "tecnico") storage.set("tecnico", value);
  clearTimeout(state.eventTimer);
  state.eventTimer = setTimeout(saveEvent, 500);
  setDraftState(t("draft_saving"));
  renderEquipoInfo();
}

function track(p) {
  state.saving.add(p);
  p.finally(() => state.saving.delete(p));
  return p;
}

async function saveEvent() {
  state.eventTimer = null;
  return track((async () => {
    const id = await ensureDraft();
    const fields = state.pendingEvent;
    state.pendingEvent = {};
    if (!Object.keys(fields).length) return;
    try {
      await api(`/api/eventos/${id}`, { method: "PATCH", json: fields });
      savedNow();
    } catch (e) {
      setDraftState(t("draft_error", { x: e.message }));
    }
  })());
}

async function flushSaves() {
  if (state.eventTimer || Object.keys(state.pendingEvent).length) { clearTimeout(state.eventTimer); saveEvent(); }
  for (const [id, line] of state.lines) if (line.timer) { clearTimeout(line.timer); saveLine(id); }
  for (const [id, x] of state.items) if (x.timer) { clearTimeout(x.timer); saveItem(id); }
  for (const [id, x] of state.topups) if (x.timer) { clearTimeout(x.timer); saveTopup(id); }
  await Promise.allSettled([...state.saving]);
}

function equipoName(id) {
  const e = state.catalog.equipos.find((q) => q.equipo_id === id);
  return e ? t("equipo_opt", { f: e.flota, id: e.equipo_id }) : id;
}

function onEquipo() {
  const sel = $("#f-equipo");
  const prev = state.prevEquipo || "";
  const hasData = state.lines.size || [...state.items.values()].some((x) => x.a.estado);
  if (prev && sel.value !== prev && hasData && !window.confirm(t("equipo_change", { a: equipoName(prev), b: equipoName(sel.value) }))) {
    sel.value = prev;
    return;
  }
  state.prevEquipo = sel.value;
  eventChanged("equipo_id", sel.value);
  renderHorometroInfo();
}

async function onActividad(code) {
  if (state.readOnly) return;
  const prev = segValue($("#f-actividad"));
  if (prev === code) return;
  const label = L(state.catalog.actividades[code]);
  if (prev && isInspection(prev) !== isInspection(code)) {
    // the form part that disappears is deleted, so nothing hidden is saved by mistake
    if (isInspection(prev)) {
      const n = [...state.items.values()].filter((x) => x.a.estado).length;
      if (n && !window.confirm(t("switch_lose_items", { x: label, n }))) return;
      if (n || state.topups.size) {
        try { await flushSaves(); await api(`/api/eventos/${await ensureDraft()}/inspeccion`, { method: "DELETE" }); }
        catch (e) { setDraftState(t("remove_failed", { x: e.message })); return; }
      }
      state.topups.clear();
      renderChecklist([]);
    } else if (state.lines.size) {
      if (!window.confirm(t("switch_lose_lines", { x: label, n: state.lines.size }))) return;
      try {
        await flushSaves();
        for (const id of [...state.lines.keys()]) await api(`/api/eventos/${state.eventId}/detalles/${id}`, { method: "DELETE" });
      } catch (e) { setDraftState(t("remove_failed", { x: e.message })); return; }
      renderLines([]);
    }
  }
  setSeg($("#f-actividad"), code);
  eventChanged("actividad", code);
  const clase = $("#f-clase");
  const suggested = state.catalog.clases_orden.find((k) => k.actividad_sugerida === code);
  if (!clase.value && suggested) { clase.value = suggested.clase_orden; eventChanged("clase_orden", clase.value); }
  showSections();
}

function renderEquipoInfo() {
  const e = state.catalog.equipos.find((q) => q.equipo_id === $("#f-equipo").value);
  $("#equipo-info").textContent = e ? `SAP ${e.equnr} · ${e.modelo} · ${e.descripcion}` : "";
  const banner = $("#banner");
  if (!e) { banner.hidden = true; return; }
  const act = state.catalog.actividades[segValue($("#f-actividad"))];
  const tec = $("#f-tecnico").value.trim();
  const hm = parseSpokenNumber($("#f-horometro").value);
  banner.replaceChildren(
    el("span", { class: "equipo" }, t("banner_equipo", { f: e.flota, id: e.equipo_id })),
    el("span", { class: "meta" }, [e.modelo, act && L(act), hm != null && t("banner_hm", { h: fmtNum(hm) }),
      tec && t("banner_tecnico", { x: tec }), state.readOnly && t("banner_ro")].filter(Boolean).join(" · ")),
  );
  banner.hidden = false;
  renderHorometroInfo();
}

function renderHorometroInfo() {
  const e = state.catalog.equipos.find((q) => q.equipo_id === $("#f-equipo").value);
  const info = $("#horometro-info");
  if (!e || e.ultimo_horometro == null) { info.textContent = ""; return; }
  const v = parseSpokenNumber($("#f-horometro").value);
  info.textContent = t("horometro_last", { h: fmtNum(e.ultimo_horometro) }) +
    (v != null && v < e.ultimo_horometro ? t("horometro_lower") : "");
}

// ---------------- daily inspection checklist ----------------
function renderChecklist(answers) {
  const lista = state.catalog.lista;
  const byId = new Map((answers || []).map((a) => [a.item_id, a]));
  const root = $("#checklist");
  root.replaceChildren();
  state.items.clear();
  for (const sec of lista.secciones) {
    const okRest = el("button", { type: "button", class: "small ok-rest" });
    okRest.addEventListener("click", () => okPending(sec.id));
    const box = el("section", { class: "sec", "data-sec": sec.id },
      el("header", { class: "sec-head" }, el("h3", {}, L(sec.nombre)), el("span", { class: "sec-count" }), okRest));
    for (const it of sec.items) box.append(renderItem(it, byId.get(it.id)));
    root.append(box);
  }
  updateProgress();
}

function renderItem(it, ans = {}) {
  const a = { estado: ans.estado || null, defectos: [...(ans.defectos || [])], posicion: ans.posicion || "", comentario: ans.comentario || "" };
  const node = el("article", { class: "item", id: `item-${it.id}`, "data-item": it.id });
  const tags = [
    it.critico && el("span", { class: "tag crit" }, t("tag_critico")),
    it.origen === "nuevo" && el("span", { class: "tag new" }, t("tag_nuevo")),
    it.origen === "fusion" && el("span", { class: "tag merged", title: L(it.nota) }, t("tag_fusion")),
  ];
  const status = el("div", { class: "status", role: "radiogroup", "aria-label": L(it.nombre) });
  for (const code of STATUS_CODES) {
    const help = state.catalog.lista.estados[code];
    status.append(el("button", { type: "button", role: "radio", "data-v": code, "aria-checked": "false",
                                 title: LANG === "en" ? help.ayuda_en : help.ayuda_es }, t(`st_${code}`)));
  }
  status.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setItemStatus(it.id, b.dataset.v); });
  const more = el("button", { type: "button", class: "small ghost item-more", title: t("add_note"), "aria-label": t("add_note") }, "✎");
  more.addEventListener("click", () => { node.classList.toggle("open"); if (node.classList.contains("open")) $("textarea", node).focus(); });
  const head = el("div", { class: "item-head" },
    el("span", { class: "item-no" }, String(it.n)),
    el("div", { class: "item-text" }, el("div", { class: "item-name" }, L(it.nombre), ...tags), el("div", { class: "item-look" }, L(it.revisar))),
    status, more);

  const body = el("div", { class: "item-body" });
  const chips = el("div", { class: "defects" });
  for (const d of it.defectos) {
    const c = el("button", { type: "button", class: "chipbtn", "data-d": d.id, "aria-pressed": String(a.defectos.includes(d.id)) }, L(d));
    c.addEventListener("click", () => toggleDefect(it.id, d.id));
    chips.append(c);
  }
  body.append(chips);
  if (it.posiciones) {
    const sel = el("select", { class: "pos", "aria-label": t("f_posicion") }, el("option", { value: "" }, t("choose")),
      ...state.catalog.lista.posiciones[it.posiciones].map(([c, es, en]) => el("option", { value: c }, LANG === "en" ? en : es)));
    sel.value = a.posicion;
    sel.addEventListener("change", () => { a.posicion = sel.value; itemChanged(it.id, { posicion: sel.value }); });
    body.append(el("label", { class: "fld pos-fld" }, el("span", {}, t("f_posicion")), sel));
  }
  const ta = el("textarea", { rows: "2", placeholder: t("ph_comentario") });
  ta.value = a.comentario;
  ta.addEventListener("input", () => { a.comentario = ta.value; itemChanged(it.id, { comentario: ta.value }, 600); });
  const vf = el("div", { class: "vfield", "data-field": "comentario" }, el("label", {}, t("f_comentario")), el("div", { class: "vrow" }, ta));
  body.append(vf);
  node.append(head, body);
  if (it.relleno) node.append(topupBox({ itemId: it.id, relleno: it.relleno }));
  node.append(el("p", { class: "row-warn", hidden: "" }));
  new MicField(vf, { itemId: it.id, label: L(it.nombre) });
  state.items.set(it.id, { el: node, it, a, pending: {}, timer: null });
  if (a.defectos.length || a.posicion || a.comentario) node.classList.add("open");
  syncItem(it.id);
  return node;
}

function syncItem(id) {
  const x = state.items.get(id);
  const { el: node, a } = x;
  node.dataset.estado = a.estado || "";
  $$(".status button", node).forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === a.estado)));
  $$(".defects button", node).forEach((b) => b.setAttribute("aria-pressed", String(a.defectos.includes(b.dataset.d))));
  const defect = a.estado === "observacion" || a.estado === "falla";
  node.classList.toggle("defect", defect);
  const warn = $(".row-warn", node);
  const need = defect && !a.defectos.length && !a.comentario.trim();
  warn.textContent = need ? t("item_need_detail") : "";
  warn.hidden = !need;
}

function setItemStatus(id, v) {
  if (state.readOnly) return;
  const x = state.items.get(id);
  x.a.estado = v;
  if (v === "observacion" || v === "falla") x.el.classList.add("open");
  itemChanged(id, { estado: v }, 150);
  syncItem(id);
  updateProgress();
  updateResultCard();
}

function toggleDefect(id, d) {
  if (state.readOnly) return;
  const x = state.items.get(id);
  const i = x.a.defectos.indexOf(d);
  if (i >= 0) x.a.defectos.splice(i, 1); else x.a.defectos.push(d);
  const fields = { defectos: [...x.a.defectos] };
  // ticking a defect means the item is not OK; start at the milder level, the technician can raise it
  if (x.a.defectos.length && (!x.a.estado || x.a.estado === "ok")) { x.a.estado = "observacion"; fields.estado = "observacion"; }
  itemChanged(id, fields, 150);
  syncItem(id);
  updateProgress();
  updateResultCard();
}

function itemChanged(id, fields, delay = 400) {
  if (state.readOnly) return;
  const x = state.items.get(id);
  Object.assign(x.pending, fields);
  syncItem(id);
  clearTimeout(x.timer);
  x.timer = setTimeout(() => saveItem(id), delay);
  setDraftState(t("draft_saving"));
}

function saveItem(id) {
  const x = state.items.get(id);
  if (!x) return;
  x.timer = null;
  const fields = x.pending;
  x.pending = {};
  if (!Object.keys(fields).length) return;
  return track((async () => {
    try {
      const eid = await ensureDraft();
      await api(`/api/eventos/${eid}/inspeccion/${id}`, { method: "PUT", json: fields });
      savedNow();
    } catch (e) { setDraftState(t("draft_error", { x: e.message })); }
  })());
}

async function okPending(secId) {
  if (state.readOnly) return;
  try {
    await flushSaves();
    const eid = await ensureDraft();
    const r = await api(`/api/eventos/${eid}/inspeccion/ok-pendientes`, { method: "POST", json: { seccion: secId } });
    for (const id of r.items) { const x = state.items.get(id); if (x) { x.a.estado = "ok"; syncItem(id); } }
    savedNow();
  } catch (e) { setDraftState(t("draft_error", { x: e.message })); }
  updateProgress();
  updateResultCard();
}

function updateProgress() {
  const all = [...state.items.values()];
  const done = all.filter((x) => x.a.estado).length;
  const insp = !$("#card-lista").hidden;
  $("#lista-progress .count").textContent = t("progress", { a: done, b: all.length });
  $("#lista-progress .bar i").style.width = `${all.length ? (100 * done) / all.length : 0}%`;
  for (const sec of $$("#checklist .sec")) {
    const xs = all.filter((x) => x.it.seccion === sec.dataset.sec);
    const n = xs.filter((x) => !x.a.estado).length;
    $(".sec-count", sec).textContent = t("progress", { a: xs.length - n, b: xs.length });
    const btn = $(".ok-rest", sec);
    btn.textContent = n ? t("ok_pending", { n }) : t("section_done");
    btn.disabled = !n || state.readOnly;
    sec.classList.toggle("complete", !n);
  }
  const pending = all.length - done;
  const next = $("#btn-next");
  next.hidden = !insp || !pending || state.readOnly;
  next.textContent = t("next_pending", { n: pending });
}

function goNextPending() {
  const x = [...state.items.values()].find((i) => !i.a.estado);
  if (!x) return;
  x.el.scrollIntoView({ block: "center", behavior: "smooth" });
  x.el.classList.remove("flash");
  void x.el.offsetWidth;   // restart the highlight animation
  x.el.classList.add("flash");
  $(".status button", x.el)?.focus({ preventScroll: true });
}

function updateResultCard() {
  const act = segValue($("#f-actividad"));
  const estado = segValue($("#f-estado")) || "";
  const grounded = estado.startsWith("fuera_servicio");
  $("#downtime-fld").hidden = !act || (isInspection(act) && !grounded && !$("#f-downtime").value);
  const critical = [...state.items.values()].filter((x) => x.it.critico && x.a.estado === "falla").map((x) => L(x.it.nombre));
  const warn = $("#critical-warn");
  warn.hidden = !critical.length || grounded || !isInspection(act);
  warn.textContent = critical.length ? t("critical_warn", { x: critical.join("; ") }) : "";
}

// ---------------- top-ups (oil, coolant, grease) ----------------
function topupBox(target) {
  const unit = target.relleno?.unidad || "L";
  const rows = el("div", { class: "topup-rows" });
  rows.target = target;
  const add = el("button", { type: "button", class: "small ghost add-topup" }, t("topup_add", { u: unit }));
  add.addEventListener("click", () => addTopup(rows));
  return el("div", { class: "topups" }, rows, add);
}

function renderTopupRow(rows, r) {
  const comps = rows.target.relleno?.compartimientos || null;
  const row = el("div", { class: "topup-row", "data-id": r.relleno_id }, el("span", { class: "topup-label" }, t("topup")));
  if (comps && comps.length > 1) {
    const sel = el("select", { "aria-label": t("topup_comp") }, el("option", { value: "" }, t("choose")), ...comps.map((c) => el("option", { value: c }, c)));
    sel.value = r.compartimiento || "";
    sel.addEventListener("change", () => topupChanged(r.relleno_id, { compartimiento: sel.value }));
    row.append(sel);
  } else if (comps) {
    row.append(el("span", { class: "topup-comp" }, comps[0]));
  }
  const qty = el("input", { class: "qty", inputmode: "decimal", autocomplete: "off", "aria-label": t("topup_qty"), placeholder: t("topup_qty") });
  qty.value = r.cantidad ?? "";
  qty.addEventListener("input", () => topupChanged(r.relleno_id, { cantidad: numberOrRaw(qty.value) }));
  const del = el("button", { type: "button", class: "small ghost topup-del", title: t("remove"), "aria-label": t("remove") }, "✕");
  del.addEventListener("click", () => removeTopup(r.relleno_id));
  row.append(qty, el("span", { class: "unit" }, r.unidad), del);
  state.topups.set(r.relleno_id, { el: row, pending: {}, timer: null });
  rows.append(row);
  return row;
}

async function addTopup(rows) {
  if (state.readOnly) return;
  try {
    const eid = await ensureDraft();
    const tg = rows.target;
    const body = tg.itemId ? { item_id: tg.itemId } : { detalle_id: tg.detalleId };
    const r = await api(`/api/eventos/${eid}/rellenos`, { method: "POST", json: body });
    const comps = tg.relleno?.compartimientos;
    const row = renderTopupRow(rows, { relleno_id: r.relleno_id, unidad: tg.relleno?.unidad || "L",
                                       compartimiento: comps?.length === 1 ? comps[0] : null, cantidad: null });
    $(comps?.length > 1 ? "select" : "input", row).focus();
  } catch (e) { setDraftState(t("add_failed", { x: e.message })); }
}

function topupChanged(id, fields) {
  if (state.readOnly) return;
  const x = state.topups.get(id);
  Object.assign(x.pending, fields);
  clearTimeout(x.timer);
  x.timer = setTimeout(() => saveTopup(id), 500);
  setDraftState(t("draft_saving"));
}

function saveTopup(id) {
  const x = state.topups.get(id);
  if (!x) return;
  x.timer = null;
  const fields = x.pending;
  x.pending = {};
  if (!Object.keys(fields).length) return;
  return track(api(`/api/eventos/${state.eventId}/rellenos/${id}`, { method: "PATCH", json: fields })
    .then(savedNow).catch((e) => setDraftState(t("draft_error", { x: e.message }))));
}

async function removeTopup(id) {
  try {
    clearTimeout(state.topups.get(id)?.timer);
    await api(`/api/eventos/${state.eventId}/rellenos/${id}`, { method: "DELETE" });
    state.topups.get(id)?.el.remove();
    state.topups.delete(id);
  } catch (e) { setDraftState(t("remove_failed", { x: e.message })); }
}

// ---------------- compartment lines (maintenance) ----------------
function compartmentSelect(sel, value) {
  const groups = new Map();
  for (const c of state.catalog.compartimientos) {
    if (!groups.has(c.grupo)) groups.set(c.grupo, el("optgroup", { label: c.grupo || t("otros") }));
    groups.get(c.grupo).append(el("option", { value: c.compartimiento }, c.compartimiento));
  }
  sel.replaceChildren(el("option", { value: "" }, t("sel_comp")), ...groups.values());
  sel.value = value || "";
}

function renderLines(detalles) {
  $("#lines").replaceChildren();
  state.lines.clear();
  for (const d of detalles) renderLine(d, false);
  renumber();
}

function renderLine(d, expanded) {
  const node = $("#line-template").content.firstElementChild.cloneNode(true);
  applyI18n(node);
  const line = { el: node, pending: {}, timer: null };
  state.lines.set(d.detalle_id, line);
  node.dataset.state = d.resultado;
  const sel = $(".line-comp", node);
  compartmentSelect(sel, d.compartimiento);
  sel.addEventListener("change", () => lineChanged(d.detalle_id, "compartimiento", sel.value));
  makeSeg($(".result", node), state.catalog.resultados, d.resultado, (v) => {
    node.dataset.state = v;
    if (v === "con_hallazgo") node.classList.remove("collapsed");
    lineChanged(d.detalle_id, "resultado", v);
  });
  for (const f of ["hallazgo", "accion_realizada", "accion_recomendada"]) {
    const v = $(`.vfield[data-field="${f}"]`, node);
    const ta = $("textarea", v);
    ta.value = d[f] || "";
    ta.addEventListener("input", () => lineChanged(d.detalle_id, f, ta.value));
    new MicField(v, { detalleId: d.detalle_id });
  }
  $(".topups", node).replaceWith(topupBox({ detalleId: d.detalle_id }));
  const hasText = d.hallazgo || d.accion_realizada || d.accion_recomendada;
  node.classList.toggle("collapsed", !expanded && !hasText && d.resultado !== "con_hallazgo");
  const toggle = $(".line-toggle", node);
  const syncToggle = () => (toggle.textContent = node.classList.contains("collapsed") ? t("line_details") : t("line_hide"));
  toggle.addEventListener("click", () => { node.classList.toggle("collapsed"); syncToggle(); });
  syncToggle();
  $(".line-del", node).addEventListener("click", () => removeLine(d.detalle_id));
  $("#lines").append(node);
  updateLineWarn(d.detalle_id);
  return node;
}

function renumber() {
  [...state.lines.values()].forEach((l, i) => ($(".line-no", l.el).textContent = `${i + 1}.`));
  $("#lines-empty").hidden = state.lines.size > 0;
}

function lineValues(id) {
  const node = state.lines.get(id).el;
  return {
    compartimiento: $(".line-comp", node).value,
    resultado: segValue($(".result", node)) || "sin_registrar",
    hallazgo: $('.vfield[data-field="hallazgo"] textarea', node).value,
    accion_realizada: $('.vfield[data-field="accion_realizada"] textarea', node).value,
    accion_recomendada: $('.vfield[data-field="accion_recomendada"] textarea', node).value,
  };
}

function updateLineWarn(id) {
  const v = lineValues(id);
  const warn = $(".row-warn", state.lines.get(id).el);
  const hasText = (v.hallazgo + v.accion_realizada + v.accion_recomendada).trim();
  let m = "";
  if (!v.compartimiento && (hasText || v.resultado !== "sin_registrar")) m = t("line_need_comp");
  else if (v.resultado === "sin_registrar" && hasText) m = t("line_need_result");
  else if (v.resultado === "con_hallazgo" && !v.hallazgo.trim()) m = t("line_need_finding");
  warn.textContent = m;
  warn.hidden = !m;
}

function lineChanged(id, field, value) {
  if (state.readOnly) return;
  const line = state.lines.get(id);
  line.pending[field] = value;
  updateLineWarn(id);
  clearTimeout(line.timer);
  line.timer = setTimeout(() => saveLine(id), 500);
  setDraftState(t("draft_saving"));
}

function saveLine(id) {
  const line = state.lines.get(id);
  if (!line) return;
  line.timer = null;
  const fields = line.pending;
  line.pending = {};
  if (!Object.keys(fields).length) return;
  return track(api(`/api/eventos/${state.eventId}/detalles/${id}`, { method: "PATCH", json: fields })
    .then(savedNow).catch((e) => setDraftState(t("draft_error", { x: e.message }))));
}

async function addLine(compartimiento = null) {
  try {
    const id = await ensureDraft();
    const r = await api(`/api/eventos/${id}/detalles`, { method: "POST", json: { compartimiento } });
    const node = renderLine({ detalle_id: r.detalle_id, compartimiento, resultado: "sin_registrar" }, true);
    renumber();
    $(".line-comp", node).focus();
  } catch (e) { setDraftState(t("add_failed", { x: e.message })); }
}

async function addAllLines() {
  try {
    await flushSaves();
    const id = await ensureDraft();
    const data = await api(`/api/eventos/${id}/detalles/todos`, { method: "POST" });
    for (const d of data.detalles) if (!state.lines.has(d.detalle_id)) renderLine(d, false);
    renumber();
  } catch (e) { setDraftState(t("add_failed", { x: e.message })); }
}

async function removeLine(id) {
  const v = lineValues(id);
  if ((v.hallazgo + v.accion_realizada + v.accion_recomendada).trim() && !window.confirm(t("line_remove_confirm"))) return;
  try {
    clearTimeout(state.lines.get(id).timer);
    await api(`/api/eventos/${state.eventId}/detalles/${id}`, { method: "DELETE" });
    state.lines.get(id).el.remove();
    state.lines.delete(id);
    renumber();
  } catch (e) { setDraftState(t("remove_failed", { x: e.message })); }
}

// ---- confirm / discard ----
async function confirmRecord() {
  const box = $("#confirm-errors");
  box.hidden = true;
  const btn = $("#btn-confirm");
  btn.disabled = true;
  try {
    await flushSaves();
    const id = await ensureDraft();
    await flushSaves();
    const saved = await api(`/api/eventos/${id}/confirmar`, { method: "POST" });
    storage.set("borrador", null);
    const ev = saved.evento;
    const x = saved.excel;
    $("#done-states").replaceChildren(
      el("span", { class: "chip ok" }, t("done_local")),
      x.pendiente ? el("span", { class: "chip warn", title: x.ultimo_error || "" }, t("done_excel_pending"))
                  : el("span", { class: "chip ok" }, t("done_excel")),
      el("span", { class: "chip warn" }, t("done_sap")),
    );
    const base = { m: equipoName(ev.equipo_id), a: L(state.catalog.actividades[ev.actividad]), d: fmtDate(ev.fecha_trabajo) };
    const count = (s) => saved.inspeccion.filter((r) => r.estado === s).length;
    let summary = isInspection(ev.actividad)
      ? t("done_summary_insp", { ...base, n: saved.inspeccion.length, ok: count("ok"), obs: count("observacion"), f: count("falla"), na: count("na") })
      : t("done_summary_lines", { ...base, n: saved.detalles.length, h: saved.detalles.filter((d) => d.resultado === "con_hallazgo").length });
    if (saved.rellenos.length) {
      const comp = (r) => r.compartimiento || saved.detalles.find((d) => d.detalle_id === r.detalle_id)?.compartimiento || "";
      summary += t("done_topups", { x: saved.rellenos.map((r) => `${comp(r)} ${fmtNum(r.cantidad)} ${r.unidad}`).join("; ") });
    }
    $("#done-summary").textContent = `${summary} ${t("workbook", { x: x.ruta })}`;
    $("#record-form").hidden = true;
    $("#done").hidden = false;
    state.eventId = null;
    state.catalog = await api("/api/catalogo");   // refreshes last hour-meter and technician list
    $("#tecnicos").replaceChildren(...state.catalog.tecnicos.map((x) => el("option", { value: x })));
    refreshStatus();
    window.scrollTo(0, 0);
  } catch (e) {
    box.textContent = e.errores ? `${t("check_before")}\n• ${e.errores.join("\n• ")}` : e.message;
    box.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

async function discardDraft() {
  if (!state.eventId) { newRecord(); return; }
  if (!window.confirm(t("discard_confirm"))) return;
  try {
    await Promise.allSettled([...state.saving]);
    await api(`/api/eventos/${state.eventId}`, { method: "DELETE" });
  } catch (e) { setDraftState(e.message); }
  newRecord();
}

// ---------------- history ----------------
async function loadHistory() {
  const rows = await api("/api/eventos");
  const body = $("#history-body");
  body.replaceChildren(...(rows.length ? rows.map((r) => {
    const insp = isInspection(r.actividad) && (r.n_items || !r.n_lineas);
    const registro = r.estado === "borrador" ? t("hist_draft")
      : t("hist_saved", { x: r.estado_sap === "pendiente" ? t("done_sap") : `SAP: ${r.estado_sap}` });
    const tr = el("tr", { class: r.estado === "borrador" ? "draft" : "" },
      el("td", {}, fmtDate(r.fecha_trabajo)),
      el("td", {}, r.flota ? `${r.flota} · ${r.equipo_id}` : "—"),
      el("td", {}, L(state.catalog.actividades[r.actividad]) || "—"),
      el("td", {}, r.tecnico || "—"),
      el("td", {}, r.horometro != null ? fmtNum(r.horometro) : ""),
      el("td", {}, String(insp ? r.n_items : r.n_lineas)),
      el("td", {}, String(insp ? r.n_defectos : r.n_hallazgos)),
      el("td", {}, L(state.catalog.estados_equipo[r.estado_equipo]) || ""),
      el("td", {}, registro));
    tr.addEventListener("click", () => loadRecord(r.evento_id));
    return tr;
  }) : [el("tr", {}, el("td", { colspan: "9" }, t("hist_empty")))]));
}

function showView(id) {
  $$(".view").forEach((v) => (v.hidden = v.id !== id));
  $$(".tab").forEach((tb) => {
    const on = tb.dataset.view === id;
    tb.classList.toggle("active", on);
    tb.setAttribute("aria-selected", String(on));
  });
  if (id === "view-history") loadHistory();
}

async function setLang(lang) {
  if (lang === LANG) return;
  await flushSaves();
  storage.set("lang", lang);
  location.reload();   // the open draft comes back after the reload
}

// ---------------- boot ----------------
(async function main() {
  applyI18n();
  $$(".lang button").forEach((b) => {
    b.setAttribute("aria-checked", String(b.dataset.lang === LANG));
    b.addEventListener("click", () => setLang(b.dataset.lang));
  });
  $$(".tab").forEach((tb) => tb.addEventListener("click", () => {
    if (tb.dataset.view === "view-record" && (!$("#done").hidden || state.readOnly)) newRecord(); else showView(tb.dataset.view);
  }));
  $$("#dictbar .engine button").forEach((b) => b.addEventListener("click", () => setEngine(b.dataset.engine)));
  $("#chip-mic").addEventListener("click", openMicDialog);
  $("#chip-excel").addEventListener("click", openExcel);
  $("#btn-open-excel").addEventListener("click", openExcel);
  $("#btn-open-excel-done").addEventListener("click", openExcel);
  $("#mic-device").addEventListener("change", changeMicDevice);
  $("#mic-test").addEventListener("click", toggleMicTest);
  state.mic.deviceId = storage.get("micDevice");
  state.engine = storage.get("engine") === "wispr" ? "wispr" : "whisper";
  renderEngine();
  pollStatus();
  state.catalog = await api("/api/catalogo");
  initForm();
  const draft = storage.get("borrador");
  if (draft) {
    try { await loadRecord(draft); if (state.readOnly) newRecord(); } catch { newRecord(); }
  } else {
    newRecord();
  }
})();
