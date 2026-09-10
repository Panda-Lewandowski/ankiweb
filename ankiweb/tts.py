from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave


class TTSUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _voices() -> dict[str, list[str]]:
    if not shutil.which("say"):
        return {}
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, check=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    voices: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
        if match:
            voices.setdefault(match.group(2), []).append(match.group(1).strip())
    return voices


def _voice(locale: str) -> str:
    available = _voices().get(locale, [])
    preferred = {"es_ES": "Mónica", "en_US": "Samantha"}.get(locale)
    if preferred in available:
        return preferred
    if available:
        return available[0]
    raise TTSUnavailable(f"No system voice is installed for {locale}")


def synthesize_tts(text: str, locale: str) -> bytes:
    """Create browser-compatible WAV audio with macOS voices, without persisting card text."""
    if not text or len(text) > 2_000:
        raise TTSUnavailable("TTS text is empty or too long")
    if not shutil.which("say") or not shutil.which("afconvert"):
        raise TTSUnavailable("System TTS is unavailable on this host")
    with tempfile.TemporaryDirectory(prefix="language-trainer-tts-") as directory:
        aiff = Path(directory) / "speech.aiff"
        wav = Path(directory) / "speech.wav"
        try:
            subprocess.run(
                ["say", "-v", _voice(locale), "-o", str(aiff)],
                input=text, capture_output=True, check=True, text=True, timeout=30,
            )
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@22050", str(aiff), str(wav)],
                capture_output=True, check=True, timeout=30,
            )
            with wave.open(str(wav), "rb") as audio:
                if audio.getnframes() == 0:
                    raise TTSUnavailable("System TTS produced empty audio")
            return wav.read_bytes()
        except TTSUnavailable:
            raise
        except (OSError, subprocess.SubprocessError, wave.Error) as exc:
            raise TTSUnavailable("System TTS could not synthesize audio") from exc
