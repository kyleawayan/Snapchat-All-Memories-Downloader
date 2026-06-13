# Snapchat-All-Memories-Downloader
This script will download all your Snapchat memories in bulk, **including the timestamp and geolocation**.

![demo](./demo.gif)


## Getting your Data
- Login to Snapchat and request your data: https://accounts.snapchat.com/accounts/downloadmydata
- Select the `Export your Memories` and `Export JSON Files` option and continue
- Date Range: Select "All Time" to get all your memories
- You'll receive one or more ZIP files (e.g. `mydata~XXXXX.zip`, `mydata~XXXXX-2.zip`, …). **Download all of them into a single folder** — newer exports put your actual photos and videos inside these ZIPs, so you need the whole set, not just the first.

![export configuration](https://github.com/user-attachments/assets/dfcdb6a0-e554-46e8-bdba-77fe41c88a03)

## Processing your export (media bundled in the ZIPs)

Recent Snapchat exports put the actual media **inside** the ZIPs and leave the download links in `memories_history.json` empty — so instead of downloading, this tool reads the media straight out of your ZIPs and writes the timestamps, GPS, and caption overlays onto them.

### 1. Install prerequisites
- **uv** — manages Python and the dependencies for you
- **ffmpeg** — required to merge video caption overlays and write video metadata
- **exiftool** *(optional but recommended)* — needed for **video GPS** to show up in Google Photos (location + correct local time). Without it, photo GPS still works.

### 2. Get the code and install dependencies
```
git clone https://github.com/ToTheMax/Snapchat-All-Memories-Downloader.git
cd Snapchat-All-Memories-Downloader
uv sync
```

### 3. Find your `memories_history.json`
It's inside the first ZIP, under `json/memories_history.json`. Extract just that one file (any unzip tool works).

### 4. Organize your files
Put **all** your ZIPs together in a folder by themselves, and keep `memories_history.json` outside it:

```
snapchat-export/
├── memories_history.json        <- extracted from the first ZIP
└── zips/                        <- ALL your mydata~*.zip files, and nothing else
    ├── mydata~1234567.zip
    ├── mydata~1234567-2.zip
    ├── mydata~1234567-3.zip
    └── …
```

### 5. (Recommended) Run a quick test first
Before processing thousands of files, run a small test export first:
```
uv run python main.py snapchat-export/memories_history.json --from-zips snapchat-export/zips -o test --test --overlay with
```
This command exports a small sample of your memories covering **every metadata scenario** (photo/video, with/without GPS, captions, and other edge cases), plus **`TEST_EXPECTATIONS.txt`** listing the **expected metadata for each one**. Upload the `test` folder to your photo app and **cross-check each file against `TEST_EXPECTATIONS.txt`**. When it all matches, continue.

### 6. Process everything
```
uv run python main.py snapchat-export/memories_history.json --from-zips snapchat-export/zips -o downloads --overlay with
```
- `--from-zips` points at the **folder containing all your ZIPs**.
- `--overlay with` merges caption/sticker overlays into the media (needs ffmpeg). Use `--overlay none` to skip overlays.

### 7. Review `missing_media.csv` (important — don't skip)
Snapchat's index sometimes lists memories whose media is **not in the export**. Every such entry is written to `missing_media.csv` in your output folder. Open it (Excel, Numbers, or any text editor) — each row has an `action` column telling you what to do:

- **`duplicate_save_of` is filled in** → the same footage is already in your output under the named file (Snapchat just listed it twice). Nothing to do; verify one or two in the app if you want.
- **action says "CHECK IN APP" / "SAVE MANUALLY"** → the export left this one out. Open the Snapchat app at that date/time; if the memory is still there, **save it by hand** before deleting your account.

> [!WARNING]
> **My Eyes Only** memories are **not included in Snapchat exports at all** — no entry, no file, so they won't even appear in `missing_media.csv`. Save those manually from the app.

### 8. Upload
Upload your output folder to Google Photos (or your photo app of choice). Captions are merged in, dates and GPS are embedded. See `TEST_EXPECTATIONS.txt` from step 5 for exactly how each type appears.

## Downloading your Memories
- Clone or [Download](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader/archive/refs/heads/main.zip) this Repository
- (Recommended) Copy the memories_history.json to the extracted or cloned folder
- Run the script:
    - Requirements: Python3.10+
    - Install the required packages: 
	```
	pip install -r requirements.txt
	```
    - Run the script: 
    ```
    python main.py
    ```


### Optional Arguments
```
usage: main.py [-h] [-o OUTPUT] [-c CONCURRENT] [--no-exif] [--no-skip-existing] 
               [--overlay {none,with,both}] [--overlay-naming {single-folder,separate-folders}]
               [--ffmpeg-path FFMPEG_PATH] [--prefix PREFIX] [--ocr-metadata] [--copy-overlays]
               [json_file]

Download Snapchat memories from data export

positional arguments:
  json_file             Path to memories_history.json (default: json/memories_history.json)

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   Output directory (default: ./downloads)
  -c, --concurrent CONCURRENT
                        Max concurrent downloads (default: 40)
  --no-exif             Disable metadata writing (no location, time or other metadata)
  --no-skip-existing    Re-download existing files
  --overlay {none,with,both}
                        Overlay handling mode:
                          - none: Skip overlays entirely (fast, default)
                          - with: Download only files with overlays
                          - both: Download both overlayed and non-overlayed versions (organization controlled by --overlay-naming)
  --overlay-naming {single-folder,separate-folders}
                        When using --overlay both:
                          - separate-folders: Split into 'with_overlays' and 'without_overlays' folders (default)
                          - single-folder: Keep all in one folder, overlayed files get '_overlayed' suffix
  --ffmpeg-path FFMPEG_PATH
                        Path to ffmpeg executable (default: ffmpeg in system PATH)
                        Required only when using --overlay with or --overlay both for video overlay merging
  --prefix PREFIX       Prefix to add to all downloaded filenames (e.g., 'SC_' creates 'SC_filename.ext')
  --copy-overlays       Save a copy of overlay files to 'overlays' subfolder
                        Requires: --overlay both
```

## Requires ffmpeg

The following features require ffmpeg to be installed:

- **Video overlay merging** - Compositing stickers, text, and filters onto videos
- **Video metadata** - Adding timestamps and GPS location to video files

If ffmpeg is not installed, you can still:
- Download all memories without overlays (`--overlay none`, the default)
- Download images with full metadata (EXIF tags including GPS and timestamps)
- Download videos, but metadata (creation time, GPS location) will not be applied

### Install ffmpeg
Download and install ffmpeg from [https://www.ffmpeg.org/download.html](https://www.ffmpeg.org/download.html)


## Downloading with Overlays
To download Snapchat memories with their overlays (stickers, text, filters), you'll need ffmpeg installed on your system.


### Download with overlays
Once ffmpeg is installed, you can download memories with overlays:

```bash
# Download only memories with overlays
python main.py --overlay with

# Download both overlayed and non-overlayed versions in separate folders
python main.py --overlay both

# Download both versions in a single folder with '_overlayed' suffix for overlaid files
python main.py --overlay both --overlay-naming single-folder
```


### Limitations
Works best on basic captions and location filters. Limited support for large/artistic fonts, blended text, and complex overlays.

### Examples

```bash
# Basic usage
python main.py memories_history.json --overlay both
```

## Troubleshooting
1. Make sure you get a fresh zip-file from Snapchat before running the script, links will expire over time
2. If you are missing the `memories_history.json` file, make sure you selected the right options in the export configuration
3. Still problems? Please open a new [issue](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader/issues) 
