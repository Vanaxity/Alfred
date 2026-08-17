"""
Voice Service - Brain API integration
"""

import base64
import numpy as np
from typing import Optional

from . import VoicePipeline, WHISPER_AVAILABLE, EDGE_TTS_AVAILABLE


class VoiceService:
    def __init__(self):
        self.pipeline: Optional[VoicePipeline] = None
        self.is_active = False

    def load(self) -> dict:
        self.pipeline = VoicePipeline()
        loaded = self.pipeline.load()
        return {
            "loaded": loaded,
            "stt_available": WHISPER_AVAILABLE,
            "tts_available": EDGE_TTS_AVAILABLE,
        }

    async def transcribe_audio(self, audio_data: bytes) -> str:
        if not self.pipeline or not self.pipeline.stt.model:
            return ""
        try:
            audio_array = self._decode_audio(audio_data)
            if audio_array is None:
                return ""
            return self.pipeline.stt.transcribe(audio_array)
        except Exception as e:
            print(f"[VOICE SVC] Transcribe error: {e}")
            return ""

    async def speak_text(self, text: str) -> bytes:
        if not self.pipeline:
            return b""
        return await self.pipeline.tts.speak(text)

    def _decode_audio(self, data: bytes) -> Optional[np.ndarray]:
        try:
            audio_int16 = np.frombuffer(data, dtype=np.int16)
            return audio_int16.astype(np.float32) / 32768.0
        except Exception as e:
            print(f"[VOICE SVC] Decode error: {e}")
            return None

    def encode_audio(self, audio_data: bytes) -> str:
        return base64.b64encode(audio_data).decode("utf-8")


voice_service = VoiceService()
