# Scrim Trimmer

Log and OCR-based video editing tool for EVE Online Alliance Tournament practice sessions. Automatically detects `CD`, `GF`, and `WF` commands typed in the local chat window, then clips each `CD → WF/GF` segment and stitches them into a single highlights reel. Works best if the local log file is available but also has a pure ORC mode.

## Warning
This app was partly vibe coded. While it works well enough, expect bugs. Be careful when using the tool, while I have tested it on my personal machine (Ubuntu) to my own satisfaction, it has not been otherwise tested. If you do encounter an error, please raise an issue on GitHub.

You should double check your Youtube account's default video upload configuration before using the upload to Youtube feature to ensure you aren't accadentally uploading videos as public. To the best of my knowladge the default is private but you have been warned.

**OneDrive warning (Windows):** Files stored in OneDrive may not be readable by this tool, and you may see "Permission denied" errors. Cloud-only ("Files On-Demand") files, and files OneDrive is currently syncing, can be locked or unavailable. This includes the default EVE chat log location (`Documents\EVE\logs\Chatlogs`), which is often redirected into OneDrive. To avoid problems, copy your videos and chat logs to a local folder outside OneDrive (or right-click them and choose "Always keep on this device"), and set the output directory somewhere outside OneDrive too.


## How it works

1. **Frame extraction** — samples one frame per second from the input video via OpenCV
2. **OCR** — runs pytesseract on the bottom-left chat region of each frame
3. **Chat analysis** — detects new `CD`/`WF`/`GF` occurrences using a monotonic-count strategy (robust to OCR noise and missed frames); only matches keywords that appear *after* the `>` separator to avoid false positives from player names
4. **Pairing** — for each `WF`, finds the most recent preceding `CD` and records the `(CD_time, WF_time)` boundary
5. **Clipping** — ffmpeg stream-copies each segment (no re-encode)
6. **Stitching** — ffmpeg concatenates all clips into `final_output.mp4`

## Installation

### Executable (recommended)

Download the latest executable for your platform from the [Releases](https://github.com/AckbadP/scrim-trimmer/releases) page. No Python installation required.

You still need to install **ffmpeg** and **tesseract** on your system:

- **Ubuntu/Debian:** `sudo apt-get install ffmpeg tesseract-ocr`
- **macOS:** `brew install ffmpeg tesseract`
- **Windows:** install [ffmpeg](https://ffmpeg.org/download.html) and [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki), both added to PATH

Then just run the executable.

### From source

<details>
<summary>Build/run from source</summary>

System packages (Ubuntu):

```bash
sudo apt-get install tesseract-ocr ffmpeg python3-tk
```

```bash
git clone <repo>
cd scrim-trimmer
python -m venv env
source env/bin/activate
pip install -r requirements.txt
```

Then launch the GUI:

```bash
python src/gui.py
```

</details>

## GUI

Drag a `.mp4` or `.mkv` file onto the window (or click Browse), configure options, and click Run.

### Default directories

The **Defaults** section at the top of the window lets you configure four persistent directories:

| Field | Behaviour |
|---|---|
| **Video directory** | On launch (and whenever this is set via Browse), the most recently modified `.mp4`/`.mkv` in the directory is auto-loaded. |
| **Log directory** | When a video is loaded, the `.txt` file in this directory whose name contains `"Local"` and whose modification time is closest to the video's is auto-selected as the chat log. |
| **Output directory** | Pre-fills the Output dir option each time a video is loaded. |
| **Chapters file dir** | Directory where the YouTube chapter timestamps file is written after a successful run. If left blank, no file is written. |

These settings are saved to `~/.config/eve-trimmer/config.json` and restored on next launch.

### Chat region

When a video is loaded, a thumbnail is shown. **Click and drag on the thumbnail to select the chat window region** — a green dashed rectangle marks the selected area. The region is saved automatically and restored on next launch.

The thumbnail is resizable: drag the window edges to make it larger for more precise selection. The image maintains its original aspect ratio.

### Match label

When **Show Match label?** is enabled (Main tab), each clip gets its chapter name (`Match 1`, `Match 2`, ... — the same names as the YouTube chapters) burned into the video for the whole clip. A sample label appears on the thumbnail; select **Drag sets: Match label position** under the thumbnail, then click or drag on the thumbnail to place it. The position is saved automatically. Labelling requires re-encoding the video (libx264, audio is still copied), so runs are slower than the default stream-copy.

### Options — Main tab

| Field | Description |
|---|---|
| **Output dir** | Directory where clips and `final_output.mp4` are written. |
| **Chat log** | One or more EVE chat log `.txt` files (semicolon-separated). When provided, OCR is skipped and the log is used for CD/WF detection instead. |
| **Show Match label?** | Off by default. Burn each clip's chapter name (`Match N`) into the video; position it on the thumbnail (see [Match label](#match-label)). |

### Options — Advanced tab

| Field | Default | Description |
|---|---|---|
| **t0** | *(auto)* | EVE game time (UTC) at video second 0 — `HH:MM:SS`. Required when using a chat log if it cannot be auto-detected from the video filename. |
| **Clip buffers (Normal start/end)** | 0 / 0 | Seconds added to each clip's start/end when detecting CD/WF commands (normal mode). Negative values move the edge earlier. |
| **Clip buffers (Tournament start/end)** | 0 / 10 | Seconds added to each clip's start/end when using Tournament Match mode. The default +10s end buffer preserves the trailing "Match completed!" text on screen. |
| **Threads** | CPU count | Number of parallel threads used for frame extraction and OCR. The hint shows the number of logical cores available on this machine. |
| **RAM cap (GB)** | 10 | Maximum gigabytes of decoded frames held in memory at once. Reduce if the process is killed by the OS. The hint shows total physical RAM available on this machine. |
| **Verbose** | off | Print OCR text and detection info per frame to the console. |
| **Force Python clipper** | off | Use the pure-Python ffmpeg wrapper instead of the native ffmpeg binary for clipping. Slower but useful for debugging. |
| **Show timestamps popup** | on | After a successful run, open a popup window displaying YouTube chapter timestamps (see below). |
| **Close when done** | off | Automatically close the application once processing completes. |
| **Raw mode (don't cut video)** | off | Detect matches as usual but don't cut anything. The full source video is uploaded unchanged, with chapter timestamps marking each match in the original recording (see below). Match label is ignored. |
| **Upload to YouTube** | off | Automatically upload `final_output.mp4` to YouTube as an unlisted video once processing finishes (see below). |
| **Video title** | today's date (UTC, `MM-DD`) | Title for the uploaded YouTube video. Re-derived from the current date every time the app launches or is reset (not remembered between runs). Leave blank to use the video filename. |

### YouTube chapter timestamps

After each successful run, the tool generates a list of YouTube chapter timestamps — one line per clip in `0:00 Match 1` format. If **Show timestamps popup** is enabled, a window opens showing the timestamps with a **Copy to Clipboard** button. If a **Chapters file dir** is configured, the timestamps are also written to a `.txt` file in that directory.

In **Raw mode** the timestamps point into the uncut source video instead: `Match N` at each match start, plus `Pre-match`, `Break` and `Post-match` chapters for downtime of 10 seconds or more (YouTube's minimum chapter length). Shorter gaps fold into the neighbouring chapter.

### YouTube upload

When **Upload to YouTube** is enabled, the finished video is automatically uploaded to YouTube as an **unlisted** video after processing completes. The chapter timestamps are included in the video description.

**First-time setup** — the first time you run with upload enabled, a browser window will open asking you to sign in to Google and grant the app permission to upload videos to your YouTube channel. After approving, a token is saved to `~/.config/eve-trimmer/youtube_token.json` and the browser step is not repeated.

Once the upload finishes:
- The YouTube URL is shown in the status bar
- If **Show timestamps popup** is enabled, the URL appears at the top of the timestamps window with an **Open** button

## CLI Usage

```bash
python src/main.py <video.mp4>
```

| Argument | Default | Description |
|---|---|---|
| `video` | *(required)* | Path to input `.mp4` or `.mkv` file |
| `--output`/`-o` | `out/` | Output directory for clips and final video |
| `--chat-region X1 Y1 X2 Y2` | `0.0 0.35 0.15 1.0` | Chat window region as fractions of frame dimensions (left, top, right, bottom) |
| `--chat-log` | *(none)* | EVE chat log `.txt` to use instead of OCR (repeatable for multiple files) |
| `--t0` | *(auto)* | EVE game time (UTC) at video second 0 — `HH:MM:SS`; required with `--chat-log` if it cannot be auto-detected |
| `--buffer-start SECONDS` | `0` | Seconds added to each clip's start in normal mode (negative = earlier) |
| `--buffer-end SECONDS` | `0` | Seconds added to each clip's end in normal mode (negative = earlier) |
| `--tournament-buffer-start SECONDS` | `0` | Seconds added to each clip's start in `--tournament-match` mode (negative = earlier) |
| `--tournament-buffer-end SECONDS` | `10` | Seconds added to each clip's end in `--tournament-match` mode (negative = earlier) |
| `--match-label` | off | Burn each clip's chapter name (`Match N`) into the video. Re-encodes (slower) |
| `--match-label-pos X Y` | `0.5 0.05` | Center of the match label as fractions of frame width/height (default: top center) |
| `--raw` | off | Raw mode: detect matches but don't cut the video; chapters use source-video timestamps |
| `--threads N` | CPU count | Number of parallel OCR worker threads |
| `--ram-cap-gb GB` | `10` | Maximum GB of decoded frames held in memory at once; reduce if the process is killed by the OS |
| `--chapters-dir DIR` | *(output dir)* | Directory to write the YouTube chapter timestamps `.txt` file |
| `--force-python-clipper` | off | Use the Python/OpenCV clipper instead of ffmpeg (slower, re-encodes, strips audio) |
| `--force-ocr` | off | Run the OCR pipeline even when `--chat-log` is provided |
| `--verbose`/`-v` | off | Print OCR text and detection info per frame |

Examples:

```bash
# Save clips to a custom directory
python src/main.py recording.mp4 --output clips/

# Use a chat log file instead of OCR
python src/main.py recording.mp4 --chat-log ~/EVE/logs/Chatlogs/Local_20240101.txt

# Custom chat region (x1 y1 x2 y2 as fractions)
python src/main.py recording.mp4 --chat-region 0.0 0.30 0.20 1.0

# Debug OCR output
python src/main.py recording.mp4 --verbose
```

## Output

```
out/
  clip_001.mp4      # first CD→WF segment
  clip_002.mp4      # second CD→WF segment
  ...
  final_output.mp4  # all clips concatenated
```

Unpaired events (a `CD` with no following `WF`, or a `WF` with no preceding `CD`) are reported as warnings but do not stop processing.

## Tests

```bash
source env/bin/activate
python -m pytest src/tests/ -v
```

Tests cover keyword matching, OCR-noise robustness, monotonic detection, and all pairing edge cases.
