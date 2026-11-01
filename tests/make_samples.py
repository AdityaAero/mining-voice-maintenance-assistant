"""Create Spanish test clips (16 kHz mono WAV) with their reference transcripts.

Uses Microsoft's online neural voices through edge-tts (needs internet once); only the
fictional sentences below are sent. The app itself never needs internet.
"""
import asyncio
import json
import pathlib
import wave

import av
import edge_tts
import numpy as np

OUT = pathlib.Path(__file__).resolve().parent / "samples"


def decode_audio(path, sampling_rate=16000):
    """Decode any audio file to mono float32 at `sampling_rate` with PyAV."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32) / 32768.0

SAMPLES = [
    ("s01", "es-CL-CatalinaNeural", "hidraulico", "hallazgo",
     "Fuga de aceite en la manguera de levante, lado izquierdo.", ["fuga", "manguera de levante", "izquierdo"]),
    ("s02", "es-CL-LorenzoNeural", "frenos", "hallazgo",
     "Frenos sin novedades.", ["sin novedades"]),
    ("s03", "es-CL-LorenzoNeural", "hidraulico", "accion",
     "No se cambió la manguera, queda pendiente para la próxima mantención.", ["no se cambió", "pendiente"]),
    ("s04", "es-CL-CatalinaNeural", "transmision", "hallazgo",
     "Temperatura del aceite de transmisión de ochenta y dos grados, presión de quince bar.",
     ["transmisión", "82", "15", "bar"]),
    ("s05", "es-CL-LorenzoNeural", "balde", "hallazgo",
     "Diente número tres del balde quebrado y adaptador con desgaste.", ["diente", "tres", "quebrado", "adaptador"]),
    ("s06", "es-CL-CatalinaNeural", "convertidor", "accion",
     "Se reemplazó el filtro de aceite del convertidor de par y se rellenaron doce litros.",
     ["filtro", "convertidor de par", "12", "litros"]),
]


def write_wav(path, audio):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sid, voice, subsystem, field, text, critical in SAMPLES:
        mp3 = OUT / f"{sid}.mp3"
        await edge_tts.Communicate(text, voice).save(str(mp3))
        audio = decode_audio(str(mp3), sampling_rate=16000)
        # pad with 0.5 s silence each side, like a push-to-talk clip
        pad = np.zeros(8000, dtype=np.float32)
        write_wav(OUT / f"{sid}.wav", np.concatenate([pad, audio, pad]))
        mp3.unlink()
        manifest.append({"id": sid, "audio": f"{sid}.wav", "voice": voice, "subsystem": subsystem,
                         "field": field, "text": text, "critical_terms": critical})
    # a non-speech clip: 3 s of low broadband noise (stands in for machinery hum)
    rng = np.random.default_rng(0)
    write_wav(OUT / "s07_noise.wav", (rng.normal(0, 0.02, 48000)).astype(np.float32))
    manifest.append({"id": "s07_noise", "audio": "s07_noise.wav", "voice": None, "subsystem": "motor",
                     "field": "hallazgo", "text": "", "critical_terms": []})
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(manifest)} clips in {OUT}")


asyncio.run(main())
