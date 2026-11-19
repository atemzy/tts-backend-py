import io
import json
import logging
import math
import os
import threading
import time
import wave
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from piper import PiperVoice, SynthesisConfig
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().with_name(".env"))

logger = logging.getLogger(__name__)
VOICES = {}
MAX_ANONYMOUS_TTS_REQUESTS_PER_MINUTE = 60
MAX_AUTHENTICATED_TTS_REQUESTS_PER_MINUTE = 300
RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_ANONYMOUS_TEXT_LENGTH = 100
MAX_AUTHENTICATED_TEXT_LENGTH = 5000
_rate_limit_lock = threading.Lock()
_anonymous_rate_limit_requests: deque[float] = deque()
_authenticated_rate_limit_requests: deque[float] = deque()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


def has_authenticated_session(request: Request) -> bool:
    cookie = request.headers.get("cookie")
    if not cookie:
        return False

    session_url = os.getenv("BETTER_AUTH_SESSION_URL", "").strip()
    try:
        parsed_url = urlsplit(session_url)
    except ValueError as exc:
        logger.error("BETTER_AUTH_SESSION_URL is invalid")
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable") from exc
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        logger.error("BETTER_AUTH_SESSION_URL must be an HTTPS URL")
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable")

    auth_request = URLRequest(
        session_url,
        headers={"Cookie": cookie, "Accept": "application/json"},
    )
    try:
        with build_opener(_NoRedirectHandler).open(auth_request, timeout=5) as response:
            session_response = json.loads(response.read())
    except HTTPError as exc:
        if exc.code in (401, 403):
            return False
        logger.warning(
            "Better Auth session endpoint returned HTTP %s", exc.code)
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable") from exc
    except (OSError, ValueError) as exc:
        logger.warning("Could not verify the Better Auth session: %s", exc)
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable") from exc

    if session_response is None:
        return False
    if not isinstance(session_response, dict):
        logger.error("Better Auth returned an unexpected session response")
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable")

    session = session_response.get("session")
    user = session_response.get("user")
    if session is None or user is None:
        return False
    if not isinstance(session, dict) or not isinstance(user, dict):
        logger.error("Better Auth returned an unexpected session response")
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable")

    user_id = user.get("id")
    if not isinstance(user_id, str) or not user_id:
        logger.error("Better Auth returned a session without a valid user ID")
        raise HTTPException(
            status_code=503, detail="Authentication service is unavailable")
    return True


def _enforce_rate_limit(
        timestamps: deque[float],
        limit: int,
        limit_name: str,
) -> None:
    now = time.monotonic()

    with _rate_limit_lock:
        while timestamps and now - timestamps[0] >= RATE_LIMIT_WINDOW_SECONDS:
            timestamps.popleft()

        if len(timestamps) >= limit:
            retry_after = math.ceil(
                RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0])
            )
            raise HTTPException(
                status_code=429,
                detail=f"{limit_name} TTS rate limit exceeded",
                headers={"Retry-After": str(max(1, retry_after))},
            )

        timestamps.append(now)


def enforce_anonymous_rate_limit() -> None:
    _enforce_rate_limit(
        _anonymous_rate_limit_requests,
        MAX_ANONYMOUS_TTS_REQUESTS_PER_MINUTE,
        "Anonymous",
    )


def enforce_authenticated_rate_limit() -> None:
    _enforce_rate_limit(
        _authenticated_rate_limit_requests,
        MAX_AUTHENTICATED_TTS_REQUESTS_PER_MINUTE,
        "Authenticated",
    )


def enforce_request_rate_limit(
        authenticated: bool = Depends(has_authenticated_session),
) -> bool:
    if authenticated:
        enforce_authenticated_rate_limit()
    else:
        enforce_anonymous_rate_limit()
    return authenticated


@asynccontextmanager
async def lifespan(app: FastAPI):
    VOICES["de"] = PiperVoice.load("models/de_DE-thorsten-medium.onnx")
    VOICES["no"] = PiperVoice.load("models/no_NO-talesyntese-medium.onnx")
    yield
    VOICES.clear()

app = FastAPI(lifespan=lifespan)
allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if "*" in allowed_origins:
    raise ValueError(
        "ALLOWED_ORIGINS must list explicit origins when credentials are enabled")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


class TTSRequest(BaseModel):
    text: str = Field(max_length=MAX_AUTHENTICATED_TEXT_LENGTH)
    language: Literal["de", "no"] = "de"
    length_scale: float = 1.0


@app.post("/tts")
def synthsize(
        req: TTSRequest,
        authenticated: bool = Depends(enforce_request_rate_limit),
):
    max_text_length = (
        MAX_AUTHENTICATED_TEXT_LENGTH
        if authenticated
        else MAX_ANONYMOUS_TEXT_LENGTH
    )
    if len(req.text) > max_text_length:
        raise HTTPException(
            status_code=422,
            detail=f"Text must be at most {max_text_length} characters",
        )

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
