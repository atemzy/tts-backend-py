import io
import wave
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from piper import PiperVoice, SynthesisConfig
from pydantic import BaseModel

VOICES = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
        VOICES["de"] = PiperVoice.load("models/de_DE-thorsten-medium.onnx")
        VOICES["no"] = PiperVoice.load("models/no_NO-talesyntese-medium.onnx")
        yield
        VOICES.clear()
        
app = FastAPI(lifespan=lifespan)

class TTSRequest(BaseModel):
        text: str
        language: Literal["de", "no"] = "de"
        length_scale: float = 1.0

@app.post("/tts")
def synthsize(req: TTSRequest):
        if not req.text.strip():
                raise HTTPException(status_code=400, detail="Text cannot be empty")

        voice = VOICES.get(req.language)
        if not voice:
                raise HTTPException(status_code=400, detail="Unsupported language!")

        syn_config = SynthesisConfig(length_scale=req.length_scale)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
                voice.synthesize_wav(req.text, wav_file, syn_config=syn_config)

        wav_buffer.seek(0)
        return StreamingResponse(
                wav_buffer,
                media_type="audio/wav",
                headers={"Content-Disposition": f"inline; filename=speech_{req.language}.wav"}
        )