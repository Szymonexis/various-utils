# Installing audio-rec

| System | What you need | How system audio is captured |
|---|---|---|
| NixOS | nothing (`shell.nix` provides everything) | PipeWire, passive |
| Windows 10/11 | Python 3.10–3.13 (64-bit) + `pip install` | WASAPI loopback, passive |
| macOS 13+ | Python 3.10–3.13 + `pip install` + **BlackHole** | via a Multi-Output Device |
| Other Linux | PipeWire tools + ffmpeg + python3-tk | PipeWire, passive |

In every case your headphones keep playing everything as normal while you record.

## NixOS

Nothing to install. `./audio-rec` loads the tools from `shell.nix` and caches them,
so it starts in about 50 ms.

## Windows 10/11

**You need**

- **Python 3.10–3.13, 64-bit, from [python.org](https://www.python.org/downloads/)**.
  During setup, tick "Add python.exe to PATH". It includes Tk, which the window
  needs. Do not use the Microsoft Store Python: it redirects the app's data
  folders.
- Nothing else. System audio is recorded with Windows' own loopback capture, and
  `pip` installs a private ffmpeg.

**Set up** in a terminal inside this folder:

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
audio-rec.cmd ui
```

**Hotkey:** right-click `audio-rec.cmd` and choose Show more options, then Create
shortcut. In the shortcut's Properties, set:

- Target: add ` toggle` at the end
- Run: Minimized
- Shortcut key: e.g. Ctrl+Alt+R

Move the shortcut to the Desktop or the Start menu; Windows only honours shortcut
keys there.

**Good to know**

- Notifications appear as coming from "Windows PowerShell".
- Bluetooth headsets switch to their low-quality "hands-free" mode while their
  microphone is in use. Use a wired or USB microphone if that bothers you.
- If you use the built `.exe` instead, Windows SmartScreen may warn the first
  time: More info, then Run anyway.

## macOS 13 or newer

**You need**

1. **Python 3.10–3.13 from [python.org](https://www.python.org/downloads/macos/)**,
   which includes Tk. Or use the built app (see below).
2. **BlackHole 2ch**, a free virtual audio device. macOS cannot record its own
   output otherwise; without it only the microphone is recorded. Install it with
   `brew install blackhole-2ch` or the installer from
   [existential.audio/blackhole](https://existential.audio/blackhole/), then
   restart if asked.
3. **A Multi-Output Device**, so you keep hearing everything:
   1. Open Audio MIDI Setup and click "+" (bottom left), then Create
      Multi-Output Device.
   2. Tick your headphones first, then BlackHole 2ch. Enable Drift Correction
      for BlackHole.
   3. In System Settings > Sound > Output, choose the Multi-Output Device.
   4. Volume keys don't work on a Multi-Output Device. Set the headphone volume
      in Audio MIDI Setup instead, or switch the output back to your headphones
      when you're not recording.

**Set up** in Terminal inside this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./audio-rec ui
```

**Microphone permission:** the first recording asks for it. When you run the
script, the request is for Terminal (or iTerm/VS Code); when you use the app, it
is for "Audio Recorder". If recorded voice is silent, allow it in System
Settings > Privacy & Security > Microphone, then restart that app.

**Hearing your own mic** ("hear my mic in headphones") isn't offered on macOS:
whether that sound would reach BlackHole depends on your output setup, so the
recording could lose or double your voice. Use your headset's own sidetone
instead.

**Notifications** come from "Script Editor". Allow it in System Settings >
Notifications if you want to see them.

**Hotkey:** in the Shortcuts app, create a new shortcut with a "Run Shell
Script" action containing `/path/to/this/folder/audio-rec toggle`. Then use the
shortcut's info panel to Add Keyboard Shortcut.

## Other Linux distributions

- **With PipeWire (most current distros):** install PipeWire's command-line tools
  (`pw-record`, `pw-link`, `pw-dump`; the package is called `pipewire`,
  `pipewire-bin` or `pipewire-utils`), plus `ffmpeg` and `python3-tk`. No pip
  packages are needed. Then run `./audio-rec ui`.
- **PulseAudio only:** create the venv and install `requirements.txt` as in the
  macOS steps. `./audio-rec` uses the venv automatically.

## Building standalone apps (optional)

PyInstaller only builds for the system it runs on, so build on each OS. On
macOS, build separately on Intel and Apple Silicon.

```sh
python -m pip install -r requirements.txt -r requirements-build.txt
pyinstaller --noconfirm --clean audio-rec.spec
```

- **Windows** gives you `dist\audio-rec\audio-rec.exe` (keep the whole folder).
- **macOS** gives you `dist/Audio Recorder.app`.
- Opening the app shows the window; `audio-rec.exe toggle` (or
  `"Audio Recorder.app/Contents/MacOS/audio-rec" toggle`) works for hotkeys.

**Without a Windows PC or a Mac:** push this folder to a GitHub repository, open
Actions, choose `build-apps`, then Run workflow, and download the zipped apps
from the finished run.

The builds are unsigned:

- **macOS:** after unzipping, run
  `xattr -dr com.apple.quarantine "Audio Recorder.app"` or use System Settings >
  Privacy & Security > Open Anyway. Each rebuild asks for microphone permission
  again.
- **Windows:** see the SmartScreen note above.

## How the recordings differ by system

- **Linux:** system audio and microphone go through one recorder and are
  sample-locked.
- **Windows and macOS:** the two devices have separate clocks, so they are
  recorded separately and lined up when you stop. Start offsets and clock drift
  are measured, and the alignment is accurate to well under a millisecond in
  tests. The original per-device captures stay in the hidden `.audio-rec`
  folder next to your recordings; delete them once you're happy with a
  recording.

The Windows and macOS code follows verified documentation and library source,
and the shared engine is tested on Linux, but it has not yet been run on a real
Windows PC or Mac.
