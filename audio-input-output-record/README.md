# audio-rec

Records what you hear (system audio) and what you say (mic) while everything
keeps playing to your headphones. Runs on Linux, Windows and macOS; see
[INSTALL.md](INSTALL.md) for what each system needs.

## Use

- **Window:** `./audio-rec ui` (Windows: `audio-rec.cmd ui`, or open the built
  app). It has Start / Pause / Stop buttons plus a folder, name and format.
- **Hotkey:** bind `./audio-rec` (Windows: `audio-rec.cmd toggle`). Each press
  starts or stops a recording.
  - KDE: System Settings → Keyboard → Shortcuts → Add New → Command or Script.
  - Windows and macOS: see [INSTALL.md](INSTALL.md).
- **Terminal:**

  ```sh
  ./audio-rec              # start / stop
  ./audio-rec pause        # pause (resume with: ./audio-rec resume)
  ./audio-rec status       # what's recording
  ./audio-rec --help       # all options
  ```

The window and the hotkey use the same folder and name settings. Closing the
window doesn't stop a recording.

## Output

Files are saved to `~/Music/Recordings` (or the folder you picked):

- `NAME.opus`: the mix of system audio and mic
- `NAME.tracks.flac`: a lossless master (left/right = system audio,
  centre = mic)
