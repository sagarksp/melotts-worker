import logging
import os
import uuid
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("melotts-worker")

OUTPUT_DIR = Path("/tmp/generated")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEVICE = os.getenv("TTS_DEVICE", "cpu")
DEFAULT_LANGUAGE = os.getenv("MELOTTS_DEFAULT_LANGUAGE", "EN")
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "6000"))
MODEL_CACHE: Dict[str, object] = {}


class GenerateRequest(BaseModel):
    text: str = Field(..., min_length=1)
    language: str = "en"


def normalize_language(value: str) -> str:
    lang = str(value or DEFAULT_LANGUAGE).strip().lower()
    if lang in {"hi", "hindi"}:
        return "HI"
    if lang in {"pa", "punjabi"}:
        return "EN"
    if lang in {"zh", "chinese"}:
        return "ZH"
    if lang in {"ja", "japanese"}:
        return "JP"
    if lang in {"es", "spanish"}:
        return "ES"
    if lang in {"fr", "french"}:
        return "FR"
    if lang in {"ko", "korean"}:
        return "KR"
    return "EN"


def get_model(language: str):
    lang = normalize_language(language)
    if lang in MODEL_CACHE:
        return MODEL_CACHE[lang]

    from melo.api import TTS

    logger.info("Loading MeloTTS model", extra={"language": lang, "device": DEVICE})
    try:
        model = TTS(language=lang, device=DEVICE)
    except Exception:
        if lang == "EN":
            raise
        logger.exception("MeloTTS language load failed, falling back to EN")
        lang = "EN"
        model = TTS(language=lang, device=DEVICE)
    MODEL_CACHE[lang] = model
    return model


def pick_speaker_id(model) -> int:
    speaker_ids = getattr(getattr(model, "hps", None), "data", None).spk2id
    if not speaker_ids:
        return 0
    keys = list(speaker_ids.keys())
    preferred = [key for key in keys if str(key).upper() in {"EN-US", "EN"}]
    return speaker_ids[preferred[0] if preferred else keys[0]]


def public_audio_url(request: Request, filename: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/generated/{filename}"


app = FastAPI(title="SyncWave MeloTTS Worker", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/generated", StaticFiles(directory=str(OUTPUT_DIR)), name="generated")


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled worker error")
    return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@app.on_event("startup")
def load_default_model():
    get_model(DEFAULT_LANGUAGE)
    logger.info("MeloTTS worker ready")


@app.get("/health")
def health():
    return {"status": "ok", "service": "melotts-worker", "modelLoaded": bool(MODEL_CACHE)}


@app.post("/generate")
def generate(payload: GenerateRequest, request: Request):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail=f"text exceeds {MAX_TEXT_CHARS} characters")

    filename = f"melotts-{uuid.uuid4().hex}.wav"
    target = OUTPUT_DIR / filename
    logger.info("Generating MeloTTS audio", extra={"filename": filename, "language": payload.language, "chars": len(text)})

    model = get_model(payload.language)
    speaker_id = pick_speaker_id(model)
    model.tts_to_file(text, speaker_id, str(target), speed=float(os.getenv("MELOTTS_SPEED", "1.0")))

    return {"success": True, "audioUrl": public_audio_url(request, filename)}
