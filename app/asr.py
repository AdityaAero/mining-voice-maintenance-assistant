"""Local speech-to-text engines for the maintenance recorder.

Two interchangeable engines run openai/whisper-large-v3 fully offline:

* ``faster-whisper`` (default): the official weights converted once to CTranslate2 int8
  (models/whisper-large-v3-ct2-int8). Fits in ~2 GB RAM and is the fastest option on a CPU.
* ``transformers``: the original Hugging Face checkpoint (models/whisper-large-v3) through
  PyTorch. Needs ~4 GB free RAM in bfloat16; useful as a reference.

Select with the environment variable VOICE_ENGINE=faster-whisper|transformers.
"""
import os
import pathlib
import threading
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
SAMPLE_RATE = 16000


VOCABULARY = {"es": ("vocabulario.txt", "Registro de mantención en el taller. Términos: {}."),
              "en": ("vocabulary_en.txt", "Workshop maintenance record. Terms: {}.")}


def load_vocabulary(language="es"):
    """Company vocabulary passed to Whisper as context (one term per line, # = comment)."""
    name, sentence = VOCABULARY.get(language, VOCABULARY["es"])
    path = ROOT / "data" / name
    if not path.exists():
        return ""
    terms = [t.strip() for t in path.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    # Whisper copies the prompt's style, so give it a punctuated sentence, not a bare list.
    return sentence.format(", ".join(terms)) if terms else ""


class FasterWhisperEngine:
    name = "faster-whisper (CTranslate2 int8, CPU)"

    def __init__(self):
        from faster_whisper import WhisperModel

        self.model_dir = MODELS / "whisper-large-v3-ct2-int8"
        self.model = WhisperModel(
            str(self.model_dir), device="cpu", compute_type="int8", cpu_threads=os.cpu_count() // 2
        )

    def transcribe(self, audio, language, prompt):
        segments, info = self.model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=int(os.environ.get("VOICE_BEAM", "5")),
            initial_prompt=prompt or None,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 700},
        )
        segments = list(segments)
        return {
            "text": " ".join(s.text.strip() for s in segments).strip(),
            "segments": [
                {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                 "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3)}
                for s in segments
            ],
            "speech_s": round(info.duration_after_vad, 2),
        }


class TransformersEngine:
    name = "transformers (PyTorch bfloat16, CPU)"

    def __init__(self):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self.torch = torch
        self.model_dir = MODELS / "whisper-large-v3"
        self.processor = WhisperProcessor.from_pretrained(self.model_dir)
        self.model = WhisperForConditionalGeneration.from_pretrained(
            self.model_dir, dtype=torch.bfloat16, low_cpu_mem_usage=True
        ).eval()

    def transcribe(self, audio, language, prompt):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        # Same Silero voice gate as the default engine: without it Whisper invents text on noise.
        if not get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=700)):
            return {"text": "", "segments": [], "speech_s": 0.0}
        torch = self.torch
        feats =self.processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt").input_features
        kwargs = {"language": language, "task": "transcribe", "num_beams": int(os.environ.get("VOICE_BEAM", "5"))}
        if prompt:
            kwargs["prompt_ids"] = torch.tensor(self.processor.get_prompt_ids(prompt))
        with torch.inference_mode():
            ids = self.model.generate(feats.to(torch.bfloat16), **kwargs)
        text = self.processor.batch_decode(ids, skip_special_tokens=True)[0]
        if prompt and text.strip().startswith(prompt.strip()):
            text = text.strip()[len(prompt.strip()):]
        return {"text": text.strip(), "segments": [], "speech_s": round(len(audio) / SAMPLE_RATE, 2)}


ENGINES = {"faster-whisper": FasterWhisperEngine, "transformers": TransformersEngine}


class SpeechRecognizer:
    """Loads the engine in a background thread and serialises transcription requests."""

    def __init__(self, engine=None):
        self.engine_key = engine or os.environ.get("VOICE_ENGINE", "faster-whisper")
        self.engine = None
        self.error = None
        self.load_s = None
        self._lock = threading.Lock()
        self._loaded = threading.Event()

    def start_loading(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        t = time.perf_counter()
        try:
            self.engine = ENGINES[self.engine_key]()
            self.load_s = round(time.perf_counter() - t, 1)
        except Exception as exc:  # reported through /api/status
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._loaded.set()

    def status(self):
        if self.error:
            state = "error"
        elif self.engine is None:
            state = "cargando"
        else:
            state = "listo"
        return {
            "state": state,
            "model": "openai/whisper-large-v3",
            "engine": ENGINES[self.engine_key].name,
            "load_s": self.load_s,
            "error": self.error,
        }

    def transcribe(self, audio: np.ndarray, language="es"):
        self._loaded.wait()
        if self.engine is None:
            raise RuntimeError(self.error or "model not loaded")
        with self._lock:
            t = time.perf_counter()
            result = self.engine.transcribe(audio, language, load_vocabulary(language))
            result["processing_s"] = round(time.perf_counter() - t, 2)
        result["audio_s"] = round(len(audio) / SAMPLE_RATE, 2)
        result["language"] = language
        result["engine"] = ENGINES[self.engine_key].name
        return result
