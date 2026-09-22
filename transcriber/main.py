import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

CONFIG = dotenv_values('.env')

AUDIO_FILE = "audio.wav"
OUTPUT_FILE = "transcription.txt"
CHUNK_SECONDS = 300  # 5 min of 16 kHz mono pcm stays well under request size limits
MAX_RETRIES = 3


def split_audio(tmpdir):
  # downsample to 16 kHz mono (all whisper uses) and split into chunks
  pattern = str(Path(tmpdir) / "chunk_%04d.wav")
  subprocess.run(
    [
      "ffmpeg", "-v", "error", "-i", AUDIO_FILE,
      "-ac", "1", "-ar", "16000",
      "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
      "-reset_timestamps", "1",
      pattern,
    ],
    check=True,
  )
  return sorted(Path(tmpdir).glob("chunk_*.wav"))


def transcribe(chunk_path):
  with open(chunk_path, "rb") as f:
    base64_audio = base64.b64encode(f.read()).decode("utf-8")

  for attempt in range(1, MAX_RETRIES + 1):
    response = requests.post(
      url="https://openrouter.ai/api/v1/audio/transcriptions",
      headers={
        "Authorization": f"Bearer {CONFIG['OPEN_ROUTER_API_KEY']}",
        "Content-Type": "application/json",
      },
      data=json.dumps({
        "model": "openai/whisper-large-v3-turbo",
        "input_audio": {
          "data": base64_audio,
          "format": "wav"
        }
      }),
      timeout=600,
    )
    if response.ok:
      return response.json()
    print(f"  attempt {attempt} failed ({response.status_code}): {response.text[:200]}", file=sys.stderr)
    if attempt < MAX_RETRIES:
      time.sleep(5 * attempt)
  raise RuntimeError(f"{chunk_path.name}: giving up after {MAX_RETRIES} failed attempts")


with tempfile.TemporaryDirectory() as tmpdir:
  print(f"splitting {AUDIO_FILE} into {CHUNK_SECONDS}s chunks...")
  chunks = split_audio(tmpdir)
  print(f"{len(chunks)} chunks to transcribe")

  texts = []
  total_cost = 0.0
  for i, chunk in enumerate(chunks, 1):
    print(f"transcribing chunk {i}/{len(chunks)}...")
    result = transcribe(chunk)
    texts.append(result["text"].strip())
    total_cost += result.get("usage", {}).get("cost", 0)

Path(OUTPUT_FILE).write_text("\n".join(texts) + "\n", encoding="utf-8")
print(f"done: wrote {OUTPUT_FILE} (total cost: ${total_cost:.4f})")
