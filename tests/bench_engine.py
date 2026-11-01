"""Transcribe the test clips directly with the engine (no server) and report speed + critical terms.

usage: python tests/bench_engine.py [faster-whisper|transformers] [beam ...]
"""
import json
import os
import pathlib
import sys
import time
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HF_HUB_OFFLINE"] = "1"

from app.asr import SpeechRecognizer  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"


def read_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0


engine = sys.argv[1] if len(sys.argv) > 1 else "faster-whisper"
beams = [int(b) for b in sys.argv[2:]] or [5]
manifest = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))

rec = SpeechRecognizer(engine)
t = time.perf_counter()
rec._load()
print(f"engine={engine} status={rec.status()} wall_load={time.perf_counter() - t:.1f}s")

for beam in beams:
    os.environ["VOICE_BEAM"] = str(beam)
    tot_audio = tot_proc = 0
    hits = total = 0
    print(f"\n--- beam {beam}")
    for item in manifest:
        audio = read_wav(SAMPLES / item["audio"])
        r = rec.transcribe(audio)
        tot_audio += r["audio_s"]
        tot_proc += r["processing_s"]
        low = r["text"].lower()
        missing = [c for c in item["critical_terms"] if c.lower() not in low]
        hits += len(item["critical_terms"]) - len(missing)
        total += len(item["critical_terms"])
        print(f"{item['id']:10s} {r['audio_s']:4.1f}s audio -> {r['processing_s']:5.2f}s | {r['text']!r}"
              + (f"  MISSING {missing}" if missing else ""))
        if not item["text"]:
            print(f"{'':10s} non-speech clip: {'OK, empty' if not r['text'] else 'INVENTED TEXT'}")
    print(f"beam {beam}: {tot_proc:.1f}s for {tot_audio:.1f}s audio (RTF {tot_proc / tot_audio:.2f}); "
          f"critical terms {hits}/{total}")
