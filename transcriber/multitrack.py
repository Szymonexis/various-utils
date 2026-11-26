"""Transcribe a Craig (Discord) multi-track recording into one speaker-labelled transcript.

Craig records every participant into a separate, time-aligned track (1-name.flac, 2-name.flac, ...
plus info.txt). Each track is transcribed on its own, so the speaker of every line comes from the
track it was spoken on and is never guessed: a person always gets the same name. Before sending, each
track is compacted to its speech (long silences replaced by a short gap), and the word timestamps
returned by the API are mapped back to the original timeline so lines from all tracks interleave in
the order they were said.

Whisper invents stock phrases ("Dziękuję.", "Dzień dobry.", "Cześć.") on laughter, breathing and
clicks, and it is confident about them. Every line that is nothing but such a phrase is re-checked on
its own audio by a second, non-whisper model (--verify-model) and only kept if that model hears it too.

usage (see README.md for more examples):
  python multitrack.py audio/<craig-folder> [--language pl] [--names audio/names.json] [-o out.txt]

  --names    optional JSON mapping a track number, file name stem or username to the name to print,
             e.g. {"3": "GM", "some_username": "Alice"}; unmapped speakers keep their Discord username
  --until    only transcribe the first N minutes (cheap test run)

Writes <craig-folder>/transcription.txt by default, plus a .dropped.txt next to it listing everything
removed as a hallucination and why. API responses are cached in <craig-folder>/.cache/, so re-running
only pays for audio that has not been transcribed yet.
"""
import argparse
import base64
import bisect
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import dotenv_values

API_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
RATE = 16000            # whisper works on 16 kHz mono
SILENCE_DB = -50        # quieter than this is silence (craig writes digital silence while someone is quiet)
MIN_SILENCE = 0.4       # shorter pauses stay inside a speech region
MIN_SPEECH = 0.12       # shorter blips are clicks and pops, not speech
PAD = 0.2               # audio kept around each region so word onsets and endings are not clipped
GAP = 1.0               # silence between regions in the compacted audio; closer regions are merged
MAX_REGION = 120.0      # longer regions are split so a single request never gets too long
CHUNK_SECONDS = 180.0   # compacted audio per request, well within the ~60 s upstream processing limit
ORPHAN_SLACK = 0.5      # words further than this from every segment were dropped by the provider itself
CHECK_PAD = 0.3         # audio around a stock phrase sent to the second model...
CHECK_MIN = 1.0         # ...at least this long, as far as the speech region allows
MAX_RETRIES = 5
LINE_GAP = 5.0          # a speaker's line continues across pauses shorter than this...
LINE_MAX = 60.0         # ...until it spans this long, so timestamps stay frequent

TRACK_RE = re.compile(r"^(\d+)-(.+)\.flac$", re.IGNORECASE)
INFO_TRACK_RE = re.compile(r"^\s+(.+?)(?:#(\d+))?\s+\(\d+\)\s*$")

# whisper's well-known hallucinations on noise / silence (it was trained on subtitled videos)
CREDITS_RE = re.compile(
  r"amara\.org|napisy (?:stworzone|wykonane|przygotowane|zrobione)|subtitles by|"
  r"zapraszam do subskrypcji|(?:dzięki|dziękuję|dziękujemy) za (?:obejrzenie|oglądanie)|"
  r"do zobaczenia w (?:następnym|kolejnym) odcinku|thanks for watching|thank you for watching|"
  r"wszelkie prawa zastrzeżone",
  re.IGNORECASE,
)
# stock phrases whisper puts on non-speech; compared after lower-casing and removing diacritics
STOCK_CORE = {
  "dziekuje", "dziekujemy", "dzieki", "dzien", "czesc", "widzenia", "zobaczenia", "dobranoc",
  "thank", "thanks", "bye", "hello",
}
STOCK_FILLER = {
  "bardzo", "za", "uwage", "dobry", "do", "wszystkim", "panstwu", "serdecznie", "you", "very",
  "much", "everyone",
}


@dataclass
class Track:
  num: int
  stem: str
  path: Path
  name: str


@dataclass
class Region:
  start: float    # seconds in the original track, padding included
  end: float
  speech: float   # seconds of detected sound inside, padding excluded


@dataclass
class Unit:
  track: Track
  start: float    # seconds in the original track
  end: float
  text: str
  region: Region
  reason: str | None = None   # why the unit is left out of the transcript, if it is


def load_api_key():
  for env in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
    if env.is_file() and dotenv_values(env).get("OPEN_ROUTER_API_KEY"):
      return dotenv_values(env)["OPEN_ROUTER_API_KEY"]
  if os.environ.get("OPEN_ROUTER_API_KEY"):
    return os.environ["OPEN_ROUTER_API_KEY"]
  sys.exit("OPEN_ROUTER_API_KEY not found: copy .env.example to .env and fill it in")


def squash(name):
  return re.sub(r"[^0-9a-z]", "", name.lower())


def tokens(text):
  """Lower-case words without diacritics or punctuation ("Dziękuję!" -> ["dziekuje"])."""
  text = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
  text = "".join(c for c in text if not unicodedata.combining(c))
  return re.sub(r"[^\w\s]", " ", text).split()


def read_info(folder):
  """Recording id, start time and the track usernames (in track order) from craig's info.txt."""
  info = {"id": folder.name, "start": None, "names": []}
  path = folder / "info.txt"
  if not path.is_file():
    return info
  in_tracks = False
  for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if m := re.match(r"Recording (\S+)", line):
      info["id"] = m.group(1)
    elif m := re.match(r"Start time:\s*(\S+)", line):
      info["start"] = m.group(1)
    elif line.startswith("Tracks:"):
      in_tracks = True
    elif in_tracks and (m := INFO_TRACK_RE.match(line)):
      user, discriminator = m.group(1), m.group(2)
      info["names"].append(user if discriminator in (None, "0") else f"{user}#{discriminator}")
    elif in_tracks and line.strip():
      in_tracks = False
  return info


def find_tracks(folder, info, names_file):
  files = sorted(
    (int(m.group(1)), m.group(2), p) for p in folder.iterdir() if (m := TRACK_RE.match(p.name))
  )
  if not files:
    sys.exit(f"no craig tracks (N-name.flac) found in {folder}")
  overrides = json.loads(Path(names_file).read_text(encoding="utf-8")) if names_file else {}
  tracks = []
  for num, stem, path in files:
    # info.txt has the exact username (file names have '.' and other characters replaced)
    username = stem
    if num <= len(info["names"]) and squash(info["names"][num - 1]) == squash(stem):
      username = info["names"][num - 1]
    name = next(
      (overrides[k] for k in (str(num), stem, username) if k in overrides), username
    )
    tracks.append(Track(num, stem, path, name))
  return tracks


def decode(track, tmpdir):
  """Decode a track to 16 kHz mono wav and return (wav path, duration, raw speech intervals)."""
  wav = Path(tmpdir) / f"{track.num}.wav"
  proc = subprocess.run(
    [
      "ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(track.path),
      "-af", f"silencedetect=noise={SILENCE_DB}dB:d={MIN_SILENCE}",
      "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(wav),
    ],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
  )
  if proc.returncode:
    raise RuntimeError(f"ffmpeg failed on {track.path.name}: {proc.stderr[-500:]}")
  with wave.open(str(wav), "rb") as w:
    duration = w.getnframes() / RATE

  silences, start = [], None
  for line in proc.stderr.splitlines():
    if m := re.search(r"silence_start: (-?[\d.]+)", line):
      start = max(0.0, float(m.group(1)))
    elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
      silences.append((start, float(m.group(1))))
      start = None
  if start is not None:
    silences.append((start, duration))

  speech, pos = [], 0.0
  for a, b in silences:
    if a > pos:
      speech.append((pos, a))
    pos = max(pos, b)
  if pos < duration:
    speech.append((pos, duration))
  return wav, duration, speech


def plan_regions(speech, duration, until):
  """Pad speech intervals, merge the ones closer than GAP and split the ones longer than MAX_REGION."""
  merged = []
  for a, b in speech:
    if b - a < MIN_SPEECH or (until is not None and a >= until):
      continue
    pa, pb = max(0.0, a - PAD), min(duration, b + PAD)
    if merged and pa - merged[-1].end < GAP:
      merged[-1].end = max(merged[-1].end, pb)
      merged[-1].speech += b - a
    else:
      merged.append(Region(pa, pb, b - a))

  regions = []
  for r in merged:
    parts = math.ceil((r.end - r.start) / MAX_REGION)
    step = (r.end - r.start) / parts
    for i in range(parts):
      regions.append(Region(r.start + i * step, r.start + (i + 1) * step, r.speech / parts))
  return regions


def plan_chunks(regions):
  """Group regions into requests of at most CHUNK_SECONDS of compacted audio.

  Every chunk is a list of (frame offset in the chunk, first frame, last frame, region): the audio of
  the region (frames first..last of the decoded track) starts at that offset in the compacted audio.
  """
  gap = int(GAP * RATE)
  chunks, current, length = [], [], 0
  for r in regions:
    a, b = round(r.start * RATE), round(r.end * RATE)
    if current and length + gap + (b - a) > CHUNK_SECONDS * RATE:
      chunks.append(current)
      current, length = [], 0
    if current:
      length += gap
    current.append((length, a, b, r))
    length += b - a
  if current:
    chunks.append(current)
  return chunks


def chunk_audio(wav_path, layout):
  out = io.BytesIO()
  with wave.open(str(wav_path), "rb") as src, wave.open(out, "wb") as dst:
    dst.setnchannels(1)
    dst.setsampwidth(2)
    dst.setframerate(RATE)
    written = 0
    for offset, a, b, _ in layout:
      dst.writeframes(b"\0\0" * (offset - written))
      src.setpos(a)
      dst.writeframes(src.readframes(b - a))
      written = offset + (b - a)
  return out.getvalue()


def request_transcription(key, audio, args, model, timestamps):
  body = {"model": model, "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"}}
  if timestamps:
    body["response_format"] = "verbose_json"
    body["timestamp_granularities"] = ["word", "segment"]
    if args.prompt:
      body["prompt"] = args.prompt
  if args.language:
    body["language"] = args.language
  if not args.allow_data_collection:
    body["provider"] = {"data_collection": "deny"}

  error = None
  for attempt in range(1, MAX_RETRIES + 1):
    try:
      response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=300,
      )
      if response.ok:
        result = response.json()
        # openrouter can report an error inside a 200 response; never accept (and cache) one
        if not isinstance(result, dict) or "error" in result or not isinstance(result.get("text"), str):
          error = f"unexpected response: {response.text[:300]}"
        elif timestamps and result["text"].strip() and not (result.get("words") or result.get("segments")):
          error = f"response has text but no timestamps (keys: {sorted(result)})"
        else:
          return result
      else:
        error = f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code in (400, 401, 402, 403):
          raise RuntimeError(error)
    except (requests.RequestException, ValueError) as e:
      error = f"{type(e).__name__}: {e}"
    print(f"  attempt {attempt}/{MAX_RETRIES} failed: {error}", file=sys.stderr, flush=True)
    if attempt < MAX_RETRIES:
      time.sleep(5 * attempt)
  raise RuntimeError(f"giving up after {MAX_RETRIES} attempts: {error}")


def cache_file(folder, prefix, parts):
  digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:20]
  return folder / ".cache" / f"{prefix}-{digest}.json"


def cached(path, fetch):
  """Return (result, cost): the cached result for free, or fetch() and cache it."""
  if path.is_file():
    return json.loads(path.read_text(encoding="utf-8")), 0.0
  result = fetch()
  path.parent.mkdir(exist_ok=True)
  tmp = path.with_suffix(".tmp")
  tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
  tmp.replace(path)
  return result, (result.get("usage") or {}).get("cost") or 0.0


def run_parallel(jobs, fn, workers, label):
  """Run fn on every job in parallel, printing progress. Returns ([(job, result)], cost, failures)."""
  results, cost, failures = [], 0.0, []
  step = max(1, len(jobs) // 20)
  with ThreadPoolExecutor(max_workers=workers) as pool:
    futures = {pool.submit(fn, job): job for job in jobs}
    for done, future in enumerate(as_completed(futures), 1):
      try:
        result, spent = future.result()
        results.append((futures[future], result))
        cost += spent
      except Exception as e:
        failures.append(e)
        print(f"  {label} FAILED: {e}", file=sys.stderr, flush=True)
      if done % step == 0 or done == len(jobs):
        print(f"  {label}: {done}/{len(jobs)} (spent ${cost:.4f})", flush=True)
  return results, cost, failures


def nearest(intervals, t):
  """Index of the (start, end) interval closest to t; intervals are sorted and non-overlapping."""
  i = bisect.bisect_right([s for s, _ in intervals], t) - 1
  candidates = [j for j in (i, i + 1) if 0 <= j < len(intervals)]
  return min(candidates, key=lambda j: max(intervals[j][0] - t, t - intervals[j][1], 0.0))


def chunk_units(result, layout, track):
  """Turn one API response into units placed on the original timeline of the track."""
  spans = [(o / RATE, (o + b - a) / RATE) for o, a, b, _ in layout]

  def to_original(j, t):
    _, a, _, _ = layout[j]
    lo, hi = spans[j]
    return a / RATE + min(max(t, lo), hi) - lo

  def mid(item):
    return (item["start"] + item["end"]) / 2

  segments = sorted(
    (s for s in result.get("segments") or [] if s.get("text", "").strip()), key=lambda s: s["start"]
  )
  words = sorted(
    (w for w in result.get("words") or [] if w.get("word", "").strip()), key=lambda w: w["start"]
  )
  units = []

  def add(j, start, end, text, reason=None):
    units.append(Unit(track, to_original(j, start), to_original(j, end), text, layout[j][3], reason))

  def add_words(ws, keep_text=None, reason=None):
    """Add words as units, one per run of consecutive words that fall into the same speech region."""
    runs = []
    for w in ws:
      j = nearest(spans, mid(w))
      if runs and runs[-1][0] == j:
        runs[-1][1].append(w)
      else:
        runs.append((j, [w]))
    for j, run in runs:
      # a segment that stays inside one region keeps whisper's own text for it
      text = keep_text if keep_text and len(runs) == 1 else " ".join(w["word"].strip() for w in run)
      add(j, run[0]["start"], run[-1]["end"], text, reason)

  if not segments and not words:
    if result.get("text", "").strip():
      add(0, spans[0][0], spans[0][1], result["text"].strip())
    return units
  if not segments:
    add_words(words)
    return units

  # the provider drops repetition loops from its segments but can leave their words in `words`
  seg_spans = [(s["start"], s["end"]) for s in segments]
  seg_words, orphans = [[] for _ in segments], []
  for w in words:
    k = nearest(seg_spans, mid(w))
    if seg_spans[k][0] - ORPHAN_SLACK <= mid(w) <= seg_spans[k][1] + ORPHAN_SLACK:
      seg_words[k].append(w)
    else:
      orphans.append(w)
  add_words(orphans, reason="words missing from the provider's own segments (a repetition loop)")

  for seg, ws in zip(segments, seg_words):
    if ws:
      add_words(ws, keep_text=seg["text"].strip())
    else:
      add(nearest(spans, mid(seg)), seg["start"], seg["end"], seg["text"].strip())
  return units


def drop_reason(unit):
  if CREDITS_RE.search(unit.text):
    return "subtitle-credit hallucination"
  if not tokens(unit.text):
    return "no words"
  data = unit.text.encode("utf-8")
  if len(data) >= 20 and len(data) / len(zlib.compress(data)) > 2.4:   # whisper's own loop threshold
    return "repetition loop"
  return None


def is_stock(text):
  words = set(tokens(text))
  return bool(words & STOCK_CORE) and words <= STOCK_CORE | STOCK_FILLER


def check_frames(unit):
  """Frames of the unit's own audio (a little context included) for the second opinion."""
  r = unit.region
  a, b = max(r.start, unit.start - CHECK_PAD), min(r.end, unit.end + CHECK_PAD)
  short = CHECK_MIN - (b - a)
  if short > 0:
    a, b = max(r.start, a - short / 2), min(r.end, b + short / 2)
  return round(a * RATE), round(b * RATE)


def clock(seconds):
  seconds = int(seconds)
  return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def build_lines(units):
  units = sorted(units, key=lambda u: (u.start, u.track.num, u.end))
  lines = []
  for u in units:
    last = lines[-1] if lines else None
    if (
      last
      and last["track"] is u.track
      and u.start - last["end"] <= LINE_GAP
      and u.end - last["start"] <= LINE_MAX
    ):
      last["text"] += " " + u.text
      last["end"] = max(last["end"], u.end)
    else:
      lines.append({"track": u.track, "start": u.start, "end": u.end, "text": u.text})
  return lines


def main():
  for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("folder", type=Path, help="craig recording folder (N-name.flac tracks + info.txt)")
  parser.add_argument("-o", "--output", type=Path, help="default: <folder>/transcription.txt")
  parser.add_argument("--language", help="ISO-639-1 code such as pl; auto-detected when omitted")
  parser.add_argument(
    "--model", default="openai/whisper-large-v3-turbo",
    help="OpenRouter model that transcribes the tracks (default: %(default)s)",
  )
  parser.add_argument(
    "--verify-model", default="microsoft/mai-transcribe-2",
    help="second opinion for lines that are only a stock phrase, 'none' drops them unchecked "
         "(default: %(default)s)",
  )
  parser.add_argument("--prompt", help="context passed to whisper, e.g. names and terms used in the call")
  parser.add_argument("--names", type=Path, help="JSON mapping track number / file stem / username to a name")
  parser.add_argument("--until", type=float, metavar="MINUTES", help="only transcribe the first N minutes")
  parser.add_argument("--workers", type=int, default=6, help="parallel requests (default 6)")
  parser.add_argument(
    "--allow-data-collection", action="store_true",
    help="also route to providers that may retain or train on the audio (default: deny)",
  )
  args = parser.parse_args()

  folder = args.folder.resolve()
  output = args.output or folder / "transcription.txt"
  until = args.until * 60 if args.until is not None else None
  key = load_api_key()
  info = read_info(folder)
  tracks = find_tracks(folder, info, args.names)
  print("speakers: " + ", ".join(f"{t.num}={t.name}" for t in tracks), flush=True)

  with tempfile.TemporaryDirectory() as tmpdir:
    print(f"decoding {len(tracks)} tracks and detecting speech...", flush=True)
    with ThreadPoolExecutor(max_workers=len(tracks)) as pool:
      decoded = list(pool.map(lambda t: decode(t, tmpdir), tracks))
    wavs = {t.num: wav for t, (wav, _, _) in zip(tracks, decoded)}

    jobs = []
    for track, (wav, duration, speech) in zip(tracks, decoded):
      regions = plan_regions(speech, duration, until)
      chunks = plan_chunks(regions)
      talk = sum(r.speech for r in regions)
      print(f"  {track.name}: {talk / 60:.1f} min of speech in {len(regions)} regions, {len(chunks)} requests")
      jobs += [(track, layout) for layout in chunks]

    def transcribe(job):
      track, layout = job
      path = cache_file(folder, str(track.num), [
        args.model, args.language, args.prompt, args.allow_data_collection, RATE,
        track.stem, [(o, a, b) for o, a, b, _ in layout],
      ])
      return cached(path, lambda: request_transcription(
        key, chunk_audio(wavs[track.num], layout), args, args.model, timestamps=True
      ))

    print(f"transcribing {len(jobs)} chunks with {args.model}...", flush=True)
    results, cost, failures = run_parallel(jobs, transcribe, args.workers, "transcribed")
    if failures:
      sys.exit(f"{len(failures)} chunks failed, nothing written; re-run to retry them (finished ones are cached)")

    units = [u for (track, layout), result in results for u in chunk_units(result, layout, track)]
    for u in units:
      u.reason = u.reason or drop_reason(u)

    suspects = [u for u in units if not u.reason and is_stock(u.text)]
    if suspects and args.verify_model.lower() == "none":
      for u in suspects:
        u.reason = "stock phrase whisper invents on noise (not verified)"
    elif suspects:
      def verify(u):
        a, b = check_frames(u)
        path = cache_file(folder, f"check-{u.track.num}", [
          args.verify_model, args.language, args.allow_data_collection, RATE, u.track.stem, a, b,
        ])
        return cached(path, lambda: request_transcription(
          key, chunk_audio(wavs[u.track.num], [(0, a, b, None)]), args, args.verify_model, timestamps=False
        ))

      print(f"double-checking {len(suspects)} stock-phrase lines with {args.verify_model}...", flush=True)
      checked, check_cost, failures = run_parallel(suspects, verify, args.workers, "checked")
      if failures:
        sys.exit(f"{len(failures)} checks failed, nothing written; re-run to retry them (finished ones are cached)")
      cost += check_cost
      for u, result in checked:
        heard = result["text"].strip()
        if not set(tokens(heard)) & STOCK_CORE:
          u.reason = f"hallucination: {args.verify_model} heard " + (repr(heard) if heard else "nothing")

  kept = [u for u in units if not u.reason]
  dropped = sorted((u for u in units if u.reason), key=lambda u: (u.start, u.track.num))
  lines = build_lines(kept)

  header = [f"# Craig recording {info['id']}" + (f", started {info['start']}" if info["start"] else "")]
  header.append("# [hh:mm:ss] is the time since the recording started")
  header.append("# speakers: " + ", ".join(t.name for t in tracks))
  body = [f"[{clock(line['start'])}] {line['track'].name}: {line['text']}" for line in lines]
  output.write_text("\n".join(header + [""] + body) + "\n", encoding="utf-8")

  dropped_path = output.with_name(output.stem + ".dropped.txt")
  dropped_path.write_text(
    "".join(f"[{clock(u.start)}] {u.track.name}: {u.text}  ({u.reason})\n" for u in dropped),
    encoding="utf-8",
  )
  print(
    f"done: wrote {len(lines)} lines to {output} "
    f"({len(dropped)} hallucinations removed, listed in {dropped_path.name}; spent ${cost:.4f})"
  )


if __name__ == "__main__":
  main()
