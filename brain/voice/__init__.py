"""
Alfred Voice Module
STT + TTS for push-to-talk voice interaction.
"""

import asyncio
import io
import sounddevice as sd
import numpy as np


try:
    import edge_tts

    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    from faster_whisper import WhisperModel

    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False


class VoiceConfig:
    SAMPLE_RATE = 16000
    CHUNK_SIZE = 2048
    WHISPER_MODEL = "base"
    TTS_VOICE = "en-US-ChristopherNeural"
    TTS_RATE = "+0%"
    TTS_PITCH = "+0Hz"


class SpeechToText:
    def __init__(self, config: VoiceConfig = None):
        self.config = config or VoiceConfig()
        self.model = None

    def load(self):
        if not WHISPER_AVAILABLE:
            print("[STT] faster-whisper not available")
            return False
        try:
            self.model = WhisperModel(
                self.config.WHISPER_MODEL, device="cpu", compute_type="int8"
            )
            print(f"[STT] Whisper '{self.config.WHISPER_MODEL}' loaded")
            return True
        except Exception as e:
            print(f"[STT] Error: {e}")
            return False

    def transcribe(self, audio_samples: np.ndarray) -> str:
        if not self.model:
            return ""
        try:
            audio_int16 = (audio_samples * 32767).astype(np.int16)
            segments, info = self.model.transcribe(
                audio_int16, language="en", beam_size=5
            )
            text = " ".join([s.text for s in segments])
            print(f"[STT] '{text}' ({info.duration:.1f}s)")
            return text.strip()
        except Exception as e:
            print(f"[STT] Error: {e}")
            return ""


class TextToSpeech:
    def __init__(self, config: VoiceConfig = None):
        self.config = config or VoiceConfig()

    async def speak(self, text: str) -> bytes:
        if not EDGE_TTS_AVAILABLE:
            print("[TTS] edge-tts not available")
            return b""
        try:
            communicate = edge_tts.Communicate(
                text,
                voice=self.config.TTS_VOICE,
                rate=self.config.TTS_RATE,
                pitch=self.config.TTS_PITCH,
            )
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            print(f"[TTS] Generated {len(audio_data)} bytes")
            return audio_data
        except Exception as e:
            print(f"[TTS] Error: {e}")
            return b""

    def speak_sync(self, text: str) -> bytes:
        return asyncio.run(self.speak(text))


class VoicePipeline:
    def __init__(self, config: VoiceConfig = None):
        self.config = config or VoiceConfig()
        self.stt = SpeechToText(config)
        self.tts = TextToSpeech(config)
        self.is_ready = False
        self._audio_buffer = []

    def load(self) -> bool:
        print("[VOICE] Loading...")
        stt_ok = self.stt.load()
        tts_ok = EDGE_TTS_AVAILABLE
        self.is_ready = stt_ok and tts_ok
        print(f"[VOICE] STT={stt_ok}, TTS={tts_ok}")
        return self.is_ready

    def record(self, duration: float = 5.0) -> np.ndarray:
        print(f"[VOICE] Recording ({duration}s max)...")
        self._audio_buffer = []

        q = asyncio.Queue()

        def callback(indata, frames, time, status):
            if status:
                print(f"[VOICE] Status: {status}")
            self._audio_buffer.append(indata[:, 0].copy())
            if len(self._audio_buffer) > int(
                duration * self.config.SAMPLE_RATE / self.config.CHUNK_SIZE
            ):
                q.put_nowait(None)

        try:
            with sd.InputStream(
                samplerate=self.config.SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=self.config.CHUNK_SIZE,
                callback=callback,
            ):
                try:
                    asyncio.get_event_loop().run_until_complete(
                        asyncio.wait_for(q.get(), timeout=duration)
                    )
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            print(f"[VOICE] Recording error: {e}")

        if self._audio_buffer:
            return np.concatenate(self._audio_buffer)
        return np.array([], dtype=np.float32)

    async def speak_async(self, text: str):
        audio = await self.tts.speak(text)
        if audio:
            with io.BytesIO(audio) as bio:
                import wave

                with wave.open(bio, "rb") as wf:
                    data = wf.readframes(wf.getnframes())
                    audio_int16 = (
                        np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                    )
                    sd.play(audio_int16, wf.getframerate())
                    sd.wait()

    def speak(self, text: str):
        asyncio.run(self.speak_async(text))


async def demo():
    print("Alfred Voice Pipeline")
    print("=" * 40)

    pipeline = VoicePipeline()
    if not pipeline.load():
        print("Failed to load voice pipeline")
        return

    print("\nRecording demo - speak now!")
    audio = pipeline.record(duration=5)

    if len(audio) > 0:
        text = pipeline.stt.transcribe(audio)
        if text:
            print(f"You said: '{text}'")
            await pipeline.speak_async(f"You said: {text}")
        else:
            print("No speech detected")


if __name__ == "__main__":
    asyncio.run(demo())
