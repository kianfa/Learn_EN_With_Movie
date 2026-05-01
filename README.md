# 🎬 PhraseClipper — Learn English With Movies

> Search any phrase across your movie/series subtitle library and instantly compile a highlight reel of every scene where it's spoken — with burned-in subtitles.

---

## What it does

PhraseClipper scans your `.srt` subtitle files, finds every moment a phrase is spoken, automatically matches each subtitle to its video file, and exports a single compilation video containing all those scenes — back-to-back, with the subtitle burned in on screen.

It's a practical tool for language learners who want to hear a word or expression used naturally in context, across dozens of movies and shows at once.

---

## Features

- **Phrase search** across an entire subtitle library in seconds (multi-threaded)
- **Automatic video matching** — intelligently pairs `.srt` files to their video counterparts (supports movies and series with `SxxEyy` naming)
- **Subtitle burning** using Pillow — no ImageMagick dependency required
- **Per-clip padding control** — adjust how many seconds before/after each line are included, globally or per individual clip
- **Live preview** — render and watch a single clip before committing to a full export
- **Compilation export** — concatenates all selected scenes into one `.mp4` output
- **Dark / Light theme** toggle
- **Persistent settings** — remembers your folders, phrase, padding, and window layout between sessions
- **Clean PyQt5 GUI** — no command line needed

---

## Screenshots

> *(Add screenshots here once the app is running — e.g. the main window with matches, the dark theme, a rendered preview)*

---

## Requirements

- Python 3.9+
- [MoviePy](https://github.com/Zulko/moviepy)
- [PyQt5](https://pypi.org/project/PyQt5/)
- [Pillow](https://pypi.org/project/Pillow/)
- [NumPy](https://numpy.org/)
- [proglog](https://pypi.org/project/proglog/)

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/kianfa/Learn_EN_With_Movie.git
cd Learn_EN_With_Movie

# 2. (Recommended) Create and activate a virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install moviepy PyQt5 Pillow numpy proglog
```

> **Windows users:** Make sure `ffmpeg` is installed and on your `PATH`. MoviePy uses it for video encoding. You can get it from [ffmpeg.org](https://ffmpeg.org/download.html).

---

## Usage

Launch the GUI:

```bash
python phraseclipper_ui.py
```

### Step-by-step

1. **Add subtitle folder(s)** — point the app at the directory (or directories) where your `.srt` files live. Up to 2 folders are supported.
2. **Add video root folder(s)** — the folder(s) where your movie/episode files are stored. The app will search recursively and match videos to subtitles automatically.
3. **Enter a phrase** — type a word or expression (e.g. `I don't know`, `exactly`, `on the other hand`).
4. **Click Scan** — the app searches all subtitle files in parallel and lists every match.
5. **Select matches** — pick the clips you want. Use the padding override panel to fine-tune the start/end of individual clips.
6. **Preview** — optionally render a single clip to check it looks right before exporting.
7. **Export** — compiles all selected clips into one `.mp4` in your chosen output folder.

---

## Configuration options

| Option | Description |
|---|---|
| **Before / After padding** | Seconds of context added before and after the matched subtitle line (can be negative to trim) |
| **Burn subs** | Overlay the subtitle text on the video frame |
| **Black gaps** | Insert a short black clip between scenes |
| **Black clip** | Use a custom video file as the gap between scenes instead of a generated black frame |
| **Case-insensitive** | Match the phrase regardless of capitalisation |
| **Output folder** | Where the final `.mp4` is saved |
| **Theme** | Dark or Light |

All settings are saved automatically and restored on next launch.

---

## Project structure

```
Learn_EN_With_Movie/
├── phraseclipper_core.py   # All processing logic: SRT parsing, phrase search,
│                           # video matching, clip building, export pipeline
└── phraseclipper_ui.py     # PyQt5 desktop GUI
```

---

## How video matching works

The app uses a multi-pass fuzzy matching strategy to find the video file for a given `.srt`:

1. **Exact stem match** in the same folder as the subtitle
2. **Fuzzy stem match** in the same folder
3. **Series episode match** (`SxxEyy`) — searches video root folders recursively, requiring both the episode code and show name token to match (prevents cross-show collisions)
4. **Movie title match** — extracts title tokens and year from the release name and matches against video filenames, with and without the year

---

## Known limitations

- Only `.srt` and `.txt` (SRT-formatted) subtitle files are supported.
- Subtitle and video files must follow common release naming conventions for automatic matching to work reliably.
- Exporting long compilations can be slow depending on your hardware — MoviePy re-encodes every clip.
- The preview and export workers cannot be cancelled mid-render once MoviePy has started writing the file.

---

## License

[MIT](LICENSE)

---

## Contributing

Issues and pull requests are welcome. If automatic video matching fails for a particular naming convention, please open an issue with an anonymised example of the filename pair.
