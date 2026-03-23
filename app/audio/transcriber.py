import httpx
from openai import AsyncOpenAI

from app.config import settings

openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def download_audio(audio_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            audio_url,
            headers={"apikey": settings.EVOLUTION_API_KEY},
        )
        response.raise_for_status()
        return response.content


async def transcribe(audio_url: str, mimetype: str = "audio/ogg") -> str:
    audio_bytes = await download_audio(audio_url)

    ext = "ogg"
    if "mp4" in mimetype:
        ext = "mp4"
    elif "mpeg" in mimetype or "mp3" in mimetype:
        ext = "mp3"
    elif "wav" in mimetype:
        ext = "wav"

    transcript = await openai_client.audio.transcriptions.create(
        model=settings.WHISPER_MODEL,
        file=(f"audio.{ext}", audio_bytes, mimetype),
    )
    return transcript.text
