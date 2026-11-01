"""Download openai/whisper-large-v3 from Hugging Face and convert it for the app.

1. Downloads only the files needed to run the model (fp16 safetensors + tokenizer/config) into
   models/whisper-large-v3, not the duplicate fp32 / Flax / .bin copies in the Hugging Face repo.
2. Converts it once to CTranslate2 int8 in models/whisper-large-v3-ct2-int8 (the default engine:
   ~2 GB RAM, the fastest option on a CPU). Needs `transformers` and `torch` for this step only.

Run once while online; afterwards everything loads from the local folders.
usage: python download_model.py [--skip-convert]
"""
import json
import pathlib
import sys

from huggingface_hub import HfApi, snapshot_download

REPO = "openai/whisper-large-v3"
ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "models" / "whisper-large-v3"
CT2 = ROOT / "models" / "whisper-large-v3-ct2-int8"

info = HfApi().model_info(REPO)
print(f"{REPO} @ revision {info.sha}")

path = snapshot_download(
    repo_id=REPO,
    revision=info.sha,
    local_dir=OUT,
    allow_patterns=[
        "config.json",
        "generation_config.json",
        "preprocessor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
        "added_tokens.json",
        "special_tokens_map.json",
        "normalizer.json",
        "model.safetensors",
        "README.md",
    ],
)

files = sorted(p for p in OUT.rglob("*") if p.is_file() and ".cache" not in p.parts)
for p in files:
    print(f"  {p.relative_to(OUT)}  {p.stat().st_size / 1e6:,.1f} MB")
(OUT / "REVISION.json").write_text(json.dumps({"repo": REPO, "revision": info.sha}, indent=2))
print("downloaded:", path)
if not (OUT / "model.safetensors").exists():
    sys.exit(1)

if "--skip-convert" not in sys.argv and not (CT2 / "model.bin").exists():
    from ctranslate2.converters import TransformersConverter

    print(f"converting to CTranslate2 int8 -> {CT2} (a few minutes)")
    TransformersConverter(str(OUT), copy_files=["tokenizer.json", "preprocessor_config.json"]).convert(
        str(CT2), quantization="int8", force=True)
print("ready:", CT2 if (CT2 / "model.bin").exists() else "conversion skipped")
