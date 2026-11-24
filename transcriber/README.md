# transcriber

Speech-to-text through OpenRouter's Whisper models.

* `main.py` transcribes a single `audio.wav` into `transcription.txt`.
* `multitrack.py` turns a Craig (Discord bot) recording, one track per person, into one transcript where every line is labelled with the speaker's name and lines appear in the order they were said.

## Requirements

* Python 3.10 or newer
* [ffmpeg](https://ffmpeg.org/) on your `PATH` (`winget install ffmpeg`, `brew install ffmpeg`, `apt install ffmpeg`)
* An OpenRouter API key, from https://openrouter.ai/keys

Copy `.env.example` to `.env` and put your key in it:

```bash
cp .env.example .env
```

## Setting up a virtual environment

Run these commands in this folder. They create a `.venv` folder that holds its own Python and packages, so nothing gets installed system-wide.

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Calling the venv's own `python -m pip` makes sure the packages land in `.venv`, whether or not the venv is activated. You only need to do this once. After `requirements.txt` changes, run the last command again.

You can activate the venv so that plain `python` and `pip` use it:

| shell              | activate                       |
|--------------------|--------------------------------|
| PowerShell         | `.venv\Scripts\Activate.ps1`   |
| cmd                | `.venv\Scripts\activate.bat`   |
| bash / zsh         | `source .venv/bin/activate`    |

Type `deactivate` to leave it. PowerShell may refuse to run `Activate.ps1` under the default execution policy. Either allow local scripts once with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or skip activation and call `.venv\Scripts\python` directly as shown below.

With Nix, `nix-shell shell.nix` gives you Python, the packages and ffmpeg in one step instead.

## Calling the scripts

Run everything from this folder. Which Python you call depends on how you set things up:

| setup                     | Windows                                   | Linux / macOS                           |
|---------------------------|-------------------------------------------|-----------------------------------------|
| venv, activated           | `python multitrack.py ...`                | `python multitrack.py ...`              |
| venv, not activated       | `.venv\Scripts\python multitrack.py ...`  | `.venv/bin/python multitrack.py ...`    |
| Nix                       |                                           | `nix-shell shell.nix`, then `python multitrack.py ...` |

The examples below use plain `python` (activated venv). Paths work with `/` on every OS.

### Where recordings go

Put recordings in the `audio/` folder. It is git-ignored, so call audio, transcripts and the API cache never end up in this public repo.

From Craig, download the **FLAC multi-track** version. Its zip has the tracks at the top level, so unzip it into a folder of its own:

```powershell
Expand-Archive craig-XXXX.flac.zip -DestinationPath audio\craig-XXXX.flac      # PowerShell
```

```bash
unzip craig-XXXX.flac.zip -d audio/craig-XXXX.flac                             # bash / zsh
```

The folder then holds `1-name.flac`, `2-name.flac`, ... (one track per person) and `info.txt`.

### main.py: one audio file

```bash
python main.py
```

It takes no arguments. It reads `audio.wav` from this folder and writes `transcription.txt` next to it. Convert other formats first, e.g. `ffmpeg -i recording.mp3 audio.wav`.

### multitrack.py: Craig multi-track recording

```
python multitrack.py <recording folder> [options]
```

Common calls:

```bash
# the usual: Polish call, all defaults
python multitrack.py audio/craig-XXXX.flac --language pl

# cheap test on the first 10 minutes, written elsewhere so it doesn't overwrite the full transcript
python multitrack.py audio/craig-XXXX.flac --language pl --until 10 -o audio/test.txt

# print your own names instead of Discord usernames
python multitrack.py audio/craig-XXXX.flac --language pl --names audio/names.json

# help whisper with words and names that come up in the call
python multitrack.py audio/craig-XXXX.flac --language pl --prompt "Sesja RPG. Postacie: Alice, Bob."

# skip the second-opinion check and drop every stock-phrase-only line unchecked
python multitrack.py audio/craig-XXXX.flac --language pl --verify-model none

# let whisper detect the language and send more requests in parallel
python multitrack.py audio/craig-XXXX.flac --workers 12
```

All options (`python multitrack.py --help` prints them too):

| option                    | default                          | what it does |
|---------------------------|----------------------------------|--------------|
| `<recording folder>`      | required                         | unzipped Craig recording with the `N-name.flac` tracks and `info.txt` |
| `-o`, `--output FILE`     | `<folder>/transcription.txt`     | where to write the transcript. The list of removed lines goes next to it as `<name>.dropped.txt` |
| `--language CODE`         | auto-detect                      | ISO-639-1 code such as `pl` or `en`. Setting it is recommended, because detection runs per chunk and can pick the wrong language on chunks with little speech |
| `--model MODEL`           | `openai/whisper-large-v3`        | OpenRouter model that transcribes the tracks. It has to return word timestamps, which the Whisper models do. `openai/whisper-large-v3-turbo` is cheaper but less accurate |
| `--verify-model MODEL`    | `openai/gpt-4o-mini-transcribe`  | second model that re-checks lines consisting only of a stock phrase ("Dziękuję.", "Dzień dobry.", "Cześć."), which Whisper invents on laughter and noise. `none` drops all such lines without checking |
| `--prompt TEXT`           | none                             | context for Whisper: names, places, jargon used in the call |
| `--names FILE`            | Discord usernames                | JSON file that renames speakers, see below |
| `--until MINUTES`         | whole recording                  | only transcribe the first N minutes |
| `--workers N`             | `6`                              | how many requests run in parallel |
| `--allow-data-collection` | off                              | also allow OpenRouter providers that may keep or train on the audio. By default they are excluded |

Speaker names come from the track each line was spoken on, so a person always gets the same name. By default that's their Discord username from `info.txt`. To print something else, write a names file and keep it in `audio/`, because it may hold real names. Keys can be the track number (the `3` in `3-name.flac`), the file name part after the number, or the username:

```json
{
  "3": "GM",
  "some_username": "Alice"
}
```

Output, cost and re-runs:

* The transcript has one line per turn, `[hh:mm:ss] name: text`, with times measured from the start of the recording. `transcription.dropped.txt` lists every removed hallucination with the reason, so you can check nothing real was lost.
* API responses are cached in `<folder>/.cache`. Running again with the same `--model`, `--language`, `--prompt` and `--allow-data-collection` costs nothing; it only rebuilds the text. Changing one of those transcribes again.
* If some requests fail, the script writes nothing and says so. Run the same command again: only the missing pieces are sent.
* For scale: a 5-hour call with 6 people cost about $0.30.

`audio/`, `.env`, `.venv`, `names.json` and transcripts are git-ignored. Keep them that way, because this repository is public.
