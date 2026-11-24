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

## Usage

The examples call the venv's Python directly. If the venv is activated, plain `python` works too. On Linux / macOS use `.venv/bin/python`.

Single file:

```powershell
.venv\Scripts\python main.py    # reads audio.wav, writes transcription.txt
```

Craig recording (unzip it first; the folder holds `1-name.flac`, `2-name.flac`, ... and `info.txt`):

```powershell
.venv\Scripts\python multitrack.py craig-XXXX.flac --language pl
.venv\Scripts\python multitrack.py craig-XXXX.flac --language pl --until 10           # cheap test: first 10 minutes
.venv\Scripts\python multitrack.py craig-XXXX.flac --language pl --names names.json   # e.g. {"3": "GM"}
```

This writes `transcription.txt` into the recording folder, plus `transcription.dropped.txt` listing every line removed as a Whisper hallucination. API responses are cached in the recording folder's `.cache`, so a second run only pays for audio that has not been transcribed yet. Run `multitrack.py --help` for all options.

Recordings, `.env`, `.venv` and transcripts are git-ignored. Keep them that way, because this repository is public.
