<div align="center">

# NotchIsland

**The MacBook notch and the iPhone Dynamic Island, on Windows.**

A black notch lives at the top of your screen. It comes alive when you listen to music,
shows the volume and the next track, and turns into a mini player when you hover over it.

![Demo](docs/demo.gif)

[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)](#installation)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#run-from-source)
[![PySide6](https://img.shields.io/badge/UI-PySide6%20(Qt)-41CD52?logo=qt&logoColor=white)](https://doc.qt.io/qtforpython-6/)
[![MIT License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

[**⬇ Download NotchIsland.exe**](https://github.com/VaticUI/NotchIsland/releases/latest)

</div>

---

## Contents

- [Features](#features)
- [States](#states)
- [Installation](#installation)
- [Usage](#usage)
- [Supported apps](#supported-apps)
- [Run from source](#run-from-source)
- [Build the executable](#build-the-executable)
- [How it works](#how-it-works)
- [Customization](#customization)
- [Known limitations](#known-limitations)
- [License](#license)

## Features

- 🎵 **Reacts to music**: as soon as a track plays, the notch widens to show the album art and an animated equalizer.
- 📈 **Equalizer driven by the real sound**: the bars follow your PC's actual audio level, not a looping animation.
- 🎨 **Album art colors**: the equalizer and the app name take on the dominant color of the cover.
- 🔔 **Track change preview**: title, artist and cover appear for a few seconds.
- 🔊 **Volume indicator**: the notch shows the volume level when you change it (and when muted).
- 🖱️ **Full player on hover**: album art, scrolling title, source app, clickable progress bar, previous / play / next.
- 🕒 **Clock**: with no music playing, hovering shows the time and date.
- 🪄 **Spring animations**: a slight bounce, in the spirit of Apple's animations.
- 🫥 **Unobtrusive**: no taskbar entry, never steals focus, and clicks next to the notch go straight through.
- 🚀 **Optional autostart** from the system tray icon.

## States

| State | Preview |
|---|---|
| **Idle**: just the black notch | ![Idle](docs/idle.png) |
| **Music playing**: album art + equalizer | ![Compact](docs/compact.png) |
| **New track**: preview for ~3 s | ![Preview](docs/peek.png) |
| **Volume**: when you turn the sound up or down | ![Volume](docs/volume.png) |
| **Hover**: the full player | ![Player](docs/expanded.png) |
| **Hover with no music**: time and date | ![Clock](docs/clock.png) |

## Installation

### Option 1: the executable (easiest)

1. Download **`NotchIsland.exe`** from the [Releases](https://github.com/VaticUI/NotchIsland/releases/latest) page.
2. Double-click it. That's it, nothing to install.

> Windows SmartScreen may show a warning because the executable is not signed:
> click **More info → Run anyway**.

### Option 2: from source

See [Run from source](#run-from-source).

## Usage

| Action | Result |
|---|---|
| Play music (Spotify, YouTube…) | The notch widens with the album art and equalizer |
| Hover over the notch | The full player opens |
| Click ⏮ ⏯ ⏭ | Previous track / play-pause / next track |
| Click the progress bar | Seek forward or backward in the track |
| Change the volume | The notch shows the volume level |
| Right-click the icon next to the clock | **Launch at Windows startup** / **Quit** |

Only one instance can run at a time: launching the program again does not create a second notch.

## Supported apps

NotchIsland reads the information apps share with Windows' own media controls
(the ones that appear when you press the volume keys). Anything that shows up there works, for example:

- Spotify, Deezer, Apple Music, Tidal
- YouTube, YouTube Music, SoundCloud, Twitch… in **Chrome**, **Edge**, **Firefox**, **Brave**, **Opera**
- Windows Media Player, VLC (recent versions), and many more

If there are several sources, the notch picks the one that **is currently playing**.

## Run from source

Requirements: **Windows 10 or 11** and **Python 3.10+**.

```bash
git clone https://github.com/VaticUI/NotchIsland.git
```

```bash
cd NotchIsland
```

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

```bash
.venv\Scripts\pythonw notch.py
```

Use `python` instead of `pythonw` to see error messages in the console.

## Build the executable

```bash
.venv\Scripts\pip install pyinstaller
```

```bash
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name NotchIsland --collect-submodules winrt --collect-submodules comtypes notch.py
```

The executable is created in `dist\NotchIsland.exe`.

## How it works

```
┌──────────────────────────┐   Qt signals   ┌────────────────────────────┐
│ MediaWorker (thread)     │ ─────────────▶ │ Notch (Qt window, 60 fps)  │
│ asyncio + WinRT          │                │ springs + QPainter drawing │
│ title, artist, cover     │ ◀───────────── │ player clicks + seeking    │
│ position, controls       │    commands    └─────────────┬──────────────┘
└──────────────────────────┘                              │
                                             ┌────────────▼─────────────┐
                                             │ AudioProbe (pycaw/WASAPI)│
                                             │ sound level + volume     │
                                             └──────────────────────────┘
```

- **Media info**: the Windows API `GlobalSystemMediaTransportControlsSessionManager`
  (via [PyWinRT](https://github.com/pywinrt/pywinrt)) provides the title, artist, album art and position, and
  lets the app control playback. It is polled every 350 ms on a separate thread.
- **Sound**: [pycaw](https://github.com/AndreMiras/pycaw) reads the peak level of the audio output (`IAudioMeterInformation`)
  to drive the equalizer, and the master volume (`IAudioEndpointVolume`) for the volume indicator.
- **Rendering**: a transparent, frameless, always-on-top Qt window. The notch shape (with its small concave joins
  to the screen edge) is drawn with Bézier curves. Its width, height and corners are animated by slightly
  underdamped springs, which is where the bounce comes from.
- **Clicks**: a window mask follows the notch shape, so everything next to it stays clickable as usual.

## Customization

Everything is set at the top of [`notch.py`](notch.py):

```python
SIZES = {
    "idle": (200, 32, 10),       # (width, height, corner radius)
    "compact": (300, 32, 12),
    "volume": (320, 32, 12),
    "peek": (420, 78, 26),
    "expanded": (500, 196, 34),
}
EAR = 8   # size of the concave joins with the screen edge
```

Animation stiffness and damping live in the `Spring` class (`k` and `d`).

## Known limitations

- Windows' own volume indicator still appears alongside the notch's.
- The equalizer follows **all** PC sound (notifications, games…), not only the music.
- The notch appears on the **primary** screen only.
- Some apps share neither the album art nor the playback position: the notch then shows a default cover
  and hides the progress bar.

## License

[MIT](LICENSE). Feel free to use, modify and share it.

*Not affiliated with Apple. "MacBook", "iPhone" and "Dynamic Island" are trademarks of Apple Inc.*
