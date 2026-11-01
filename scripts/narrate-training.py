"""Generate cached, chapter-aligned AI narration through Replit's OpenAI proxy."""
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import wave
from difflib import SequenceMatcher
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
SERVER = APP / "server" if (APP / "server/manage.py").exists() else APP
ROOT = SERVER.parents[2] if SERVER != APP else APP
SOURCE = SERVER / "docs/training-scripts.json"
OUTPUT = ROOT / "generated-artifacts/training/narration"
MODEL = "gpt-audio"
VOICE = "alloy"


def normalise(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def canonical_wav(content):
    # Streaming WAV responses can leave RIFF/data sizes at 0xffffffff. Measure
    # actual PCM frames rather than trusting that placeholder as the duration.
    chunks = []
    with wave.open(io.BytesIO(content), "rb") as recording:
        channels, width, rate = recording.getnchannels(), recording.getsampwidth(), recording.getframerate()
        total = 0
        while chunk := recording.readframes(65536):
            total += len(chunk)
            if total > rate * channels * width * 120:
                raise ValueError("Narration exceeds the maximum chapter length")
            chunks.append(chunk)
    frames = b"".join(chunks)
    seconds = len(frames) / (channels * width * rate)
    if seconds < 5 or channels not in (1, 2):
        raise ValueError("Narration is empty or invalid")
    fixed = io.BytesIO()
    with wave.open(fixed, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return fixed.getvalue(), seconds


def generate(role, index, chapter):
    text = chapter["title"] + ". " + chapter["instruction"]
    digest = hashlib.sha256((MODEL + VOICE + text).encode()).hexdigest()[:16]
    directory = OUTPUT / role
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory / f"{index}-{digest}.wav"
    metadata = audio.with_suffix(".json")
    if audio.exists() and metadata.exists():
        result = json.loads(metadata.read_text())
        fixed, result["seconds"] = canonical_wav(audio.read_bytes())
        audio.write_bytes(fixed)
        metadata.write_text(json.dumps(result, indent=2))
        print(f"Reusing narration {role} {index+1}", flush=True)
        return result
    payload = {
        "model": MODEL, "modalities": ["text", "audio"],
        "audio": {"voice": VOICE, "format": "wav"},
        "messages": [
            {"role": "system", "content": "Perform text-to-speech only. Read the supplied text verbatim, with no introduction, omission or commentary. Use clear professional English at a calm instructional pace with natural sentence pauses."},
            {"role": "user", "content": "Repeat the following text verbatim: " + text},
        ],
    }
    endpoint = os.environ["AI_INTEGRATIONS_OPENAI_BASE_URL"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["AI_INTEGRATIONS_OPENAI_API_KEY"],
                 "Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                response_data = json.load(response)
            spoken = response_data["choices"][0]["message"]["audio"]
            encoded = spoken["data"]
            transcript = spoken.get("transcript", "")
            ratio = SequenceMatcher(None, normalise(text), normalise(transcript)).ratio()
            if ratio < .97:
                raise ValueError("Narration differs from the approved script")
            content, duration = canonical_wav(base64.b64decode(encoded, validate=True))
            audio.write_bytes(content)
            result = {"role": role, "chapter": index, "path": str(audio.relative_to(ROOT)),
                "seconds": duration, "script": text, "transcript": transcript,
                "match_ratio": ratio, "voice": VOICE, "model": MODEL}
            metadata.write_text(json.dumps(result, indent=2))
            print(f"Narrated {role} {index+1}: {duration:.1f}s", flush=True)
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Narration request failed with HTTP {exc.code}") from None
        except (ValueError, KeyError, TimeoutError):
            if attempt == 2:
                raise RuntimeError(f"Narration validation failed for {role} chapter {index+1}") from None
        time.sleep(2 ** (attempt+1))
    raise RuntimeError("Narration did not complete")


def main():
    scripts = json.loads(SOURCE.read_text())
    work = [(role["slug"], index, chapter) for role in scripts["roles"]
        for index, chapter in enumerate(role["chapters"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(generate, *item) for item in work]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda item: (item["role"], item["chapter"]))
    (OUTPUT / "manifest.json").write_text(json.dumps(results, indent=2))
    print(f"Verified {len(results)} narrated chapters.", flush=True)


if __name__ == "__main__":
    main()
