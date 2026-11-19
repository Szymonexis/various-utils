"""audio-rec: record system audio output + microphone for hours, while
everything keeps playing to your headphones exactly as before.

Backends
  pipewire  Linux: passive patching of the output's monitor and the mic into
            one sample-locked 3-channel recorder node (best quality).
  portable  Windows (WASAPI loopback), macOS (a loopback device such as
            BlackHole) and any PulseAudio Linux: captures the output and the
            mic separately and aligns them (drift + offset) on stop.

Files (in the chosen folder; NAME = name/prefix + date & time by default):
  NAME.opus                  stereo mix: system audio + mic (or --mix-format)
  NAME.tracks.flac           lossless 3-channel master: L, R = system, C = mic
  .audio-rec/NAME.json|.log  session metadata and log (hidden)
"""
