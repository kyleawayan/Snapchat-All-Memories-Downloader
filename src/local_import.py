"""Local import: process Snapchat bulk-export ZIPs (inline media) instead of downloading.

Newer Snapchat exports ("Export your Memories" selected) bundle the actual media
inside the export ZIPs (memories/<UTC-date>_<UUID>-main.<ext> plus optional
memories/<...>-overlay.png) and ship a memories_history.json whose
"Media Download Url" / "Download Link" fields are EMPTY. Nothing can be
downloaded; instead, this module maps each JSON entry to its inline file and
runs the same overlay-merge + metadata pipeline used for downloads.

Mapping key: the ZIP member's DOS timestamp is the entry's UTC capture time at
2-second resolution (odd seconds truncate down). (media type, UTC floored to an
even second) matches every file to its JSON entry; simultaneous bursts inside
the same 2-second window are assigned in chronological order.

JSON entries with no media file are classified into missing_media.csv:
most are duplicate ledger rows whose twin entry (same second/type) HAS the file.
"""

import asyncio
import csv
import re
import time
import zipfile
from bisect import bisect
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tqdm.asyncio import tqdm

from . import config
from .config import OverlayMode, OverlayNaming
from .memory import Memory, MediaType
from .stats import Stats
from .overlay import merge_image_overlay, merge_video_overlay
from .metadata import apply_metadata_and_timestamps
from .download import _filter_memories_to_download

MEMBER_RE = re.compile(
    r"^memories/(\d{4}-\d{2}-\d{2})_([0-9A-Fa-f-]{36})-(main|overlay)\.([A-Za-z0-9]+)$"
)
IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "webp"}
VIDEO_EXTS = {"mp4", "mov"}

# A same-type file within this window is hinted as a possible duplicate ledger row
DUP_TWIN_MAX_S = 2

# Warnings printed during the run are collected and re-printed after the final
# summary -- progress output scrolls them away otherwise.
_run_warnings: list[str] = []


def _warn(message: str) -> None:
    _run_warnings.append(message)
    print(message)


class ExportFile:
    """One -main media member inside an export ZIP (overlay tracked separately)."""

    __slots__ = ("zip_path", "member", "base", "media_type", "mtime", "size")

    def __init__(self, zip_path: Path, member: str, base: str, media_type: str,
                 mtime: datetime, size: int):
        self.zip_path = zip_path
        self.member = member
        self.base = base          # "<UTC-date>_<UUID>"
        self.media_type = media_type  # "image" | "video"
        self.mtime = mtime        # naive UTC, even seconds (DOS resolution)
        self.size = size          # uncompressed bytes


def _floor2(dt: datetime) -> datetime:
    """Floor to even second (DOS timestamp resolution).

    ZIP members store mtimes as DOS timestamps, whose seconds field holds
    seconds/2 -- only even seconds are representable, so odd-second capture
    times truncate down by one second. Flooring BOTH sides to even seconds
    makes the match an exact key equality, not a range search.

    Empirically validated on a real multi-thousand-file export: treating member
    mtimes as UTC, every file fell within 0-2s of a JSON entry (exactly 0s for
    even-second captures, 1-2s for odd-second truncations) and none beyond --
    a perfect two-bucket split with an empty tail, i.e. quantization, not noise.
    """
    return dt.replace(second=dt.second // 2 * 2, microsecond=0)


def _memory_utc(memory: Memory) -> datetime:
    """Entry capture time as naive UTC (memory.date may have been localized)."""
    return memory.date.astimezone(timezone.utc).replace(tzinfo=None)


def index_zips(zips_dir: Path) -> tuple[list[ExportFile], dict[str, tuple[Path, str]]]:
    """Index all export ZIPs' central directories (no media decompressed).

    Returns (main files, overlay members keyed by base name).
    """
    mains: dict[str, ExportFile] = {}
    overlays: dict[str, tuple[Path, str]] = {}
    zip_paths = sorted(zips_dir.glob("*.zip"))
    if not zip_paths:
        raise FileNotFoundError(f"No .zip files found in {zips_dir}")

    dup_mains = dup_overlays = dup_size_mismatch = 0
    for zip_path in zip_paths:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                m = MEMBER_RE.match(info.filename)
                if not m:
                    continue
                date_str, uuid, kind, ext = m.group(1), m.group(2), m.group(3), m.group(4).lower()
                base = f"{date_str}_{uuid}"
                if kind == "overlay":
                    if base in overlays:
                        dup_overlays += 1
                    else:
                        overlays[base] = (zip_path, info.filename)
                    continue
                if base in mains:
                    # Same basename in more than one ZIP: keep the first, but never
                    # silently -- differing sizes mean the copies are NOT identical.
                    dup_mains += 1
                    if info.file_size != mains[base].size:
                        dup_size_mismatch += 1
                    continue
                if ext in IMAGE_EXTS:
                    media_type = "image"
                elif ext in VIDEO_EXTS:
                    media_type = "video"
                else:
                    print(f"Skipping unknown extension: {info.filename}")
                    continue
                mains[base] = ExportFile(
                    zip_path, info.filename, base, media_type,
                    datetime(*info.date_time), info.file_size,
                )

    print(f"Indexed {len(zip_paths)} ZIPs: {len(mains)} media files, {len(overlays)} overlays")
    if dup_mains or dup_overlays:
        _warn(f"WARNING: duplicate filenames across ZIPs skipped (first occurrence used): "
              f"{dup_mains} media, {dup_overlays} overlays"
              + (f" -- {dup_size_mismatch} of the media duplicates have DIFFERENT sizes, "
                 f"so the copies are not identical!" if dup_size_mismatch else "")
              + "\nIf you combined multiple exports in one folder, process each export separately.")
    return list(mains.values()), overlays


def map_memories(
    memories: list[Memory], files: list[ExportFile]
) -> tuple[list[tuple[Memory, ExportFile]], list[Memory], list[ExportFile]]:
    """Match each export file to its JSON entry via (type, UTC floored to 2s).

    Files/entries sharing one key (simultaneous bursts) are paired in
    chronological order. Returns (matched pairs, entries without file,
    files without entry).
    """
    entries_by_key: dict[tuple[str, datetime], list[Memory]] = defaultdict(list)
    for memory in memories:
        entries_by_key[(memory.media_type.value, _floor2(_memory_utc(memory)))].append(memory)
    for group in entries_by_key.values():
        group.sort(key=_memory_utc)

    matched: list[tuple[Memory, ExportFile]] = []
    unmatched_files: list[ExportFile] = []
    claimed: set[int] = set()

    for file in sorted(files, key=lambda f: (f.mtime, f.base)):
        group = entries_by_key.get((file.media_type, file.mtime), [])
        memory = next((m for m in group if id(m) not in claimed), None)
        if memory is None:
            unmatched_files.append(file)
            continue
        claimed.add(id(memory))
        matched.append((memory, file))

    unmatched_memories = [m for m in memories if id(m) not in claimed]
    return matched, unmatched_memories, unmatched_files


def write_missing_report(
    unmatched_memories: list[Memory],
    matched: list[tuple[Memory, ExportFile]],
    output_dir: Path,
    all_memories: list[Memory],
) -> tuple[int, int, Path] | None:
    """Write missing_media.csv: every JSON entry that has NO media file in the export.

    Every row should be reviewed in the Snapchat app and saved manually if it is a
    real memory -- exports have been observed to silently omit media that still
    exists in the app.

    `duplicate_save_of` names the file of an ADJACENT ledger row with the same
    media type, identical GPS, and a (near-)identical timestamp. That signature
    matches cases where Snapchat stored the same footage under more than one
    save (observed in the wild: a memory shown in the app both as one stitched
    video and as 10-second segments -- each representation gets ledger rows,
    while the export ships media only for the segments; the stitched tile
    appears to be an app-side proxy view over the same footage rather than a
    separately exported asset). Footage for such rows is present via the named
    file. It is strong evidence, not a guarantee -- verify against the app,
    not the hint.
    """
    if not unmatched_memories:
        return
    filed_by_type: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for m, _ in matched:
        filed_by_type[m.media_type.value].append(
            (_memory_utc(m), m.get_filename(occurrence=m.occurrence))
        )
    for entries in filed_by_type.values():
        entries.sort()

    def _nearest(entries: list[tuple[datetime, str]], dt: datetime) -> tuple[float, str]:
        times = [t for t, _ in entries]
        i = bisect(times, dt)
        best: tuple[float, str] = (float("inf"), "")
        for j in (i - 1, i):
            if 0 <= j < len(entries):
                delta = abs((entries[j][0] - dt).total_seconds())
                if delta < best[0]:
                    best = (delta, entries[j][1])
        return best

    # Double-save signature: adjacent ledger row, same type, identical GPS,
    # (near-)identical timestamp, and that row's media IS in the export.
    ledger_pos = {id(m): i for i, m in enumerate(all_memories)}
    filed_memories = {id(m): m.get_filename(occurrence=m.occurrence) for m, _ in matched}

    def _duplicate_save_of(memory: Memory) -> str:
        i = ledger_pos.get(id(memory))
        if i is None:
            return ""
        for j in (i - 1, i + 1):
            if not 0 <= j < len(all_memories):
                continue
            neighbor = all_memories[j]
            if (id(neighbor) in filed_memories
                    and neighbor.media_type == memory.media_type
                    and neighbor.latitude == memory.latitude
                    and neighbor.longitude == memory.longitude
                    and abs((_memory_utc(neighbor) - _memory_utc(memory)).total_seconds()) <= DUP_TWIN_MAX_S):
                return filed_memories[id(neighbor)]
        return ""

    report_path = output_dir / "missing_media.csv"
    dup_saves = 0
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["utc_date", "media_type", "latitude", "longitude",
                         "action", "duplicate_save_of"])
        for memory in sorted(unmatched_memories, key=_memory_utc):
            dt = _memory_utc(memory)
            twin = _duplicate_save_of(memory)
            dup_saves += bool(twin)
            action = (
                "footage already in archive (see duplicate_save_of); verify once in the app"
                if twin else
                "CHECK IN APP at this time; if the memory exists there, save it manually -- the export omitted it"
            )
            writer.writerow([
                dt.strftime("%Y-%m-%d %H:%M:%S"),
                memory.media_type.value,
                memory.latitude if memory.latitude is not None else "",
                memory.longitude if memory.longitude is not None else "",
                action,
                twin,
            ])
    _warn(
        f"WARNING: {len(unmatched_memories)} JSON entries have NO media file in this export\n"
        f"  -> {report_path}\n"
        f"Snapchat exports can silently omit media that still exists in the app.\n"
        f"Review EVERY row in the app and save manually what is real, before closing your account.\n"
        f"({dup_saves} rows match Snapchat's double-save signature -- their footage exists via the\n"
        f"file named in duplicate_save_of, but verify against the app, not the hint.)"
    )
    return len(unmatched_memories), dup_saves, report_path


def pick_subset(matched: list[tuple[Memory, ExportFile]], overlays: dict, n: int):
    """Curate n items spread across (media type x GPS x overlay) buckets so a
    small test exercises every risky combination."""
    buckets: dict[tuple, list] = defaultdict(list)
    for memory, file in matched:
        key = (file.media_type, memory.location_available, file.base in overlays)
        buckets[key].append((memory, file))

    # Overlay-having buckets first: they exercise the most failure modes
    ordered = sorted(buckets, key=lambda k: (not k[2], k[0], not k[1]))
    print("Subset buckets available: " + ", ".join(
        f"{k[0]}{'+gps' if k[1] else ''}{'+overlay' if k[2] else ''}: {len(buckets[k])}"
        for k in ordered
    ))
    picked, i = [], 0
    while len(picked) < n and any(i < len(buckets[k]) for k in ordered):
        for k in ordered:
            if len(picked) >= n:
                break
            if i < len(buckets[k]):
                picked.append(buckets[k][i])
        i += 1
    return picked


async def _process_one(
    memory: Memory,
    file: ExportFile,
    overlays: dict[str, tuple[Path, str]],
    semaphore: asyncio.Semaphore,
    stats: Stats,
    progress_bar,
) -> None:
    """Read one memory's media from its ZIP, merge overlay, embed metadata.

    Mirrors download.py/zip_processor.py output semantics exactly:
    - overlay 'none':  raw main written to output root
    - overlay 'with':  merged version (when overlay exists) else raw main
    - overlay 'both':  merged to with_overlays/, raw main to without_overlays/
    """
    async with semaphore:
        try:
            main_data = await asyncio.to_thread(_read_member, file.zip_path, file.member)
            overlay_ref = overlays.get(file.base) if config.overlay_mode != OverlayMode.NONE else None
            overlay_data = (
                await asyncio.to_thread(_read_member, overlay_ref[0], overlay_ref[1])
                if overlay_ref else None
            )

            filename = memory.get_filename(occurrence=memory.occurrence)
            is_image = memory.media_type == MediaType.IMAGE

            if config.overlay_mode == OverlayMode.BOTH and overlay_data:
                if config.overlay_naming == OverlayNaming.SEPARATE_FOLDERS:
                    merged_path = config.output_dir / config.WITH_OVERLAYS_DIR / filename
                    raw_path = config.output_dir / config.WITHOUT_OVERLAYS_DIR / filename
                else:
                    merged_path = config.output_dir / memory.get_filename(has_overlay=True, occurrence=memory.occurrence)
                    raw_path = config.output_dir / filename
                memory.path_with_overlay = merged_path
                memory.path_without_overlay = raw_path
                if is_image:
                    merge_image_overlay(merged_path, main_data, overlay_data, memory)
                    stats.total_images += 1
                    stats.images_with_overlay += 1
                else:
                    await merge_video_overlay(merged_path, main_data, overlay_data, memory)
                    stats.total_videos += 1
                    stats.videos_with_overlay += 1
                raw_path.write_bytes(main_data)
                if is_image:
                    stats.extra_images_without_overlay += 1
                else:
                    stats.extra_videos_without_overlay += 1
                if config.save_overlays_only:
                    overlay_copy = config.output_dir / config.overlays_dir / memory.get_overlay_filename(occurrence=memory.occurrence)
                    overlay_copy.parent.mkdir(parents=True, exist_ok=True)
                    overlay_copy.write_bytes(overlay_data)
            elif config.overlay_mode == OverlayMode.WITH and overlay_data:
                merged_path = config.output_dir / filename
                memory.path_with_overlay = merged_path
                # Fallback target: on overlay-merge failure, overlay.py saves the
                # raw main bytes to path_without_overlay so the memory is never
                # lost. Without this it would be None and the fallback would crash.
                memory.path_without_overlay = merged_path
                if is_image:
                    merge_image_overlay(merged_path, main_data, overlay_data, memory)
                    stats.total_images += 1
                    stats.images_with_overlay += 1
                else:
                    await merge_video_overlay(merged_path, main_data, overlay_data, memory)
                    stats.total_videos += 1
                    stats.videos_with_overlay += 1
            else:
                # No overlay (or 'none' mode): raw main bytes, no re-encode
                if config.overlay_mode == OverlayMode.BOTH and config.overlay_naming == OverlayNaming.SEPARATE_FOLDERS:
                    raw_path = config.output_dir / config.WITHOUT_OVERLAYS_DIR / filename
                else:
                    raw_path = config.output_dir / filename
                raw_path.write_bytes(main_data)
                memory.path_without_overlay = raw_path
                if is_image:
                    stats.total_images += 1
                    stats.images_without_overlay += 1
                else:
                    stats.total_videos += 1
                    stats.videos_without_overlay += 1

            await asyncio.to_thread(apply_metadata_and_timestamps, memory)
            stats.downloaded += 1
            stats.mb += len(main_data) / 1024 / 1024
        except Exception as e:
            stats.failed += 1
            print(f"\nError processing {file.member}: {e}")
        finally:
            progress_bar.update(1)


def _read_member(zip_path: Path, member: str) -> bytes:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(member)


def _write_unmatched_report(unmatched_files: list[ExportFile], output_dir: Path) -> Path:
    """Write unmatched_files.csv: media files present in the export but absent
    from the JSON."""
    report_path = output_dir / "unmatched_files.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["zip", "member", "utc_timestamp_from_file", "media_type"])
        for file in unmatched_files:
            writer.writerow([file.zip_path.name, file.member,
                             file.mtime.strftime("%Y-%m-%d %H:%M:%S"), file.media_type])
    return report_path


def _synthesize_memories(
    unmatched_files: list[ExportFile], taken_names: set[str]
) -> list[tuple[Memory, ExportFile]]:
    """Build Memory objects for files that have no JSON entry, so their media
    can be imported instead of dropped. The file's own ZIP timestamp (UTC)
    becomes the capture time; no GPS is available."""
    synthetic: list[tuple[Memory, ExportFile]] = []
    for file in unmatched_files:
        memory = Memory(**{
            "Date": file.mtime.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Media Type": file.media_type,
            "Media Download Url": "",
        })
        # No suffix unless the name is taken by an existing same-second output
        occurrence = 0
        while memory.get_filename(occurrence=occurrence) in taken_names:
            occurrence += 1
        memory.occurrence = occurrence
        taken_names.add(memory.get_filename(occurrence=occurrence))
        synthetic.append((memory, file))
    return synthetic


async def import_all(memories: list[Memory]) -> None:
    """Entry point: map memories to inline export files and process them."""
    assert config.from_zips is not None, "import_all requires config.from_zips"
    _run_warnings.clear()
    files, overlays = index_zips(config.from_zips)
    if overlays and config.overlay_mode == OverlayMode.NONE:
        _warn(f"WARNING: this export contains {len(overlays)} overlay (caption) files, but "
              f"--overlay none skips merging them.\n"
              f"Captions would be missing from the output. Use --overlay with (or both) to keep them.")
    matched, unmatched_memories, unmatched_files = map_memories(memories, files)

    print(f"Mapped {len(matched)}/{len(files)} media files to JSON entries")
    if unmatched_files:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = _write_unmatched_report(unmatched_files, config.output_dir)
        if config.import_unlisted:
            taken = {m.get_filename(occurrence=m.occurrence) for m, _ in matched}
            matched = matched + _synthesize_memories(unmatched_files, taken)
            _warn(f"{len(unmatched_files)} files have no JSON entry -> {report_path}\n"
                  f"--import-unlisted: importing them with the file's own timestamp (UTC), no GPS.")
        else:
            _warn(f"WARNING: {len(unmatched_files)} of {len(files)} files are not listed in this JSON\n"
                  f"  -> {report_path}\n"
                  f"They are NOT imported. If these are real memories (and not just a partial JSON),\n"
                  f"re-run with --import-unlisted to import them using each file's own timestamp.")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.overlay_mode == OverlayMode.BOTH and config.overlay_naming == OverlayNaming.SEPARATE_FOLDERS:
        (config.output_dir / config.WITH_OVERLAYS_DIR).mkdir(parents=True, exist_ok=True)
        (config.output_dir / config.WITHOUT_OVERLAYS_DIR).mkdir(parents=True, exist_ok=True)

    missing_report = write_missing_report(unmatched_memories, matched, config.output_dir, memories)

    if config.subset:
        matched = pick_subset(matched, overlays, config.subset)
        print(f"Subset mode: processing {len(matched)} curated items")

    stats = Stats()
    to_process = matched
    if config.skip_existing:
        keep = set(id(m) for m in _filter_memories_to_download([m for m, _ in matched], stats))
        to_process = [(m, f) for m, f in matched if id(m) in keep]
    if not to_process:
        print("All files already imported!")
        return

    # Local processing is CPU/disk-bound (ffmpeg merges), not network-bound
    concurrency = min(config.max_concurrent, 8)
    semaphore = asyncio.Semaphore(concurrency)
    progress_bar = tqdm(total=len(to_process), desc="Importing", unit="file")
    start_time = time.time()

    await asyncio.gather(
        *[_process_one(m, f, overlays, semaphore, stats, progress_bar) for m, f in to_process]
    )

    progress_bar.close()
    stats.print_summary(time.time() - start_time)

    # Re-print everything important AFTER the summary: warnings printed during
    # the run scroll out of sight behind the progress output.
    if missing_report:
        total, dup_saves, report_path = missing_report
        print("=" * 70)
        print("REPORTS")
        print("=" * 70)
        print(f"{report_path.name}: {total} ledger entries have no media file in the export")
        print(f"  - {dup_saves} match the double-save signature (footage present via the named file)")
        print(f"  - {total - dup_saves} need review in the Snapchat app")
        print(f"  -> {report_path}")
    if stats.failed:
        print(f"NOTE: {stats.failed} files failed processing -- see the messages above for each one.")
    if _run_warnings:
        print("=" * 70)
        print("WARNINGS RECAP (already shown above, repeated so they aren't missed)")
        print("=" * 70)
        for w in _run_warnings:
            print(w)
            print("-" * 70)
