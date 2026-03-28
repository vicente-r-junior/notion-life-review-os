import base64
import httpx
from openai import AsyncOpenAI

from app.config import settings

openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def download_audio_via_evolution(message_id: str) -> tuple[bytes, str]:
    """
    Download and decrypt WhatsApp audio through Evolution API.
    Returns (audio_bytes, mimetype).
    """
    url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{settings.EVOLUTION_INSTANCE}"
    payload = {"message": {"key": {"id": message_id}}}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"apikey": settings.EVOLUTION_API_KEY},
        )
        response.raise_for_status()
        data = response.json()

    b64 = data.get("base64", "")
    mimetype = data.get("mimetype", "audio/ogg")
    audio_bytes = base64.b64decode(b64)
    return audio_bytes, mimetype


def _ext_from_mimetype(mimetype: str) -> str:
    if "mp4" in mimetype:
        return "mp4"
    if "mpeg" in mimetype or "mp3" in mimetype:
        return "mp3"
    if "wav" in mimetype:
        return "wav"
    if "webm" in mimetype:
        return "webm"
    if "m4a" in mimetype:
        return "m4a"
    return "ogg"


async def transcribe(message_id: str) -> str:
    audio_bytes, mimetype = await download_audio_via_evolution(message_id)
    ext = _ext_from_mimetype(mimetype)

    transcript = await openai_client.audio.transcriptions.create(
        model=settings.WHISPER_MODEL,
        file=(f"audio.{ext}", audio_bytes, mimetype),
    )
    return transcript.text
