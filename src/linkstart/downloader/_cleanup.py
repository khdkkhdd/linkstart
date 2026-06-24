"""Dual-mode dedup/cleanup + ffmpeg/ffprobe helpers (used by both modes)."""
import logging
import re
import shutil
from pathlib import Path

from linkstart.downloader._paths import unique_path
from linkstart.models import ChannelConfig, DownloadResult, LiveInfo

log = logging.getLogger(__name__)

# filename → epoch parser:  {full|edge}.{epoch}[_{NN}].mp4
_EPOCH_RE = re.compile(r"^(?:full|edge)\.(\d+)(?:_\d{2})?\.mp4$")


def _parse_epoch(filename: str) -> int | None:
    """Extract the unix timestamp from a 'full.{epoch}[_NN].mp4' filename."""
    m = _EPOCH_RE.match(filename)
    return int(m.group(1)) if m else None


# Minimum new-timeline seconds an edge file must add to be kept (absorbs ffprobe/HLS noise).
MIN_UNIQUE_COVERAGE_SEC: int = 5

Interval = tuple[int, int]


def _new_coverage_seconds(candidate: Interval, covered: list[Interval]) -> int:
    """Seconds of `candidate` [start, end) not already covered by any interval in `covered`."""
    c_start, c_end = candidate
    if c_end <= c_start:
        return 0

    clipped: list[Interval] = []
    for s, e in covered:
        a = max(c_start, s)
        b = min(c_end, e)
        if b > a:
            clipped.append((a, b))
    if not clipped:
        return c_end - c_start

    clipped.sort()
    merged: list[Interval] = [clipped[0]]
    for s, e in clipped[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    overlap_total = sum(e - s for s, e in merged)
    return (c_end - c_start) - overlap_total


def _edge_keep_path(base_final: Path, index: int) -> Path:
    return base_final.with_name(f"{base_final.stem}.edge_{index:03d}.mp4")


def _recovered_keep_path(base_final: Path, index: int) -> Path:
    return base_final.with_name(f"{base_final.stem}.recovered_{index:03d}.mp4")


async def cleanup_dual(
    paths,
    media,
    channel: ChannelConfig,
    live: LiveInfo,
    parts_dir: Path,
    *,
    retry_count: int,
    full_restarted: bool = False,
) -> DownloadResult:
    """Dedupe dual-mode parts into one final mp4 + extras; return the result."""
    # Filter by _parse_epoch to exclude intermediate per-format files (e.g. full.{epoch}.f140.mp4).
    full_files = [
        f for f in sorted(parts_dir.glob("full.*.mp4"))
        if _parse_epoch(f.name) is not None
    ]
    full_durations: dict[Path, int] = {}
    for f in full_files:
        d = await media.ffprobe_duration(f)
        if d is not None:
            full_durations[f] = d

    if not full_durations:
        return await _cleanup_no_base(
            paths, media, channel, live, parts_dir, retry_count=retry_count
        )

    # 1) base = longest from-start
    base_src = max(full_durations, key=lambda p: full_durations[p])
    base_duration = full_durations[base_src]
    base_start_epoch = _parse_epoch(base_src.name)
    if base_start_epoch is None:
        paths.discard_parts_dir(parts_dir)
        return DownloadResult(
            success=False,
            error=f"could not parse epoch from {base_src.name}",
            retry_count=retry_count,
        )

    # 2) delete shorter full.*.mp4 (strict prefixes of base)
    for f in full_files:
        if f != base_src:
            f.unlink(missing_ok=True)

    # 3) move base to final location
    base_final = unique_path(paths.final_path(channel, live))
    base_src.rename(base_final)

    # 4) edge keep/delete via coverage cursor; the interval list is still
    # collected for fragment recovery, which needs the union.
    if live.started_at is not None:
        base_start = int(live.started_at.timestamp())
        trust_base = True
    else:
        base_start = base_start_epoch
        trust_base = not full_restarted
    base_end = base_start + base_duration
    covered: list[Interval] = [(base_start, base_end)]
    # Collect edge entries (file, start_epoch, duration), sorted by start.
    edge_entries: list[tuple[Path, int, int | None]] = []
    for f in sorted(parts_dir.glob("edge.*.mp4")):
        epoch = _parse_epoch(f.name)
        if epoch is None:
            continue
        d = await media.ffprobe_duration(f)
        edge_entries.append((f, epoch, d))
    edge_entries.sort(key=lambda t: t[1])

    extras: list[Path] = []
    keep_index = 1
    covered_until = base_end
    for f, epoch, d in edge_entries:
        if d is None:
            # ffprobe failed — keep defensively, but do not extend coverage.
            kept = _edge_keep_path(base_final, keep_index)
            f.rename(kept)
            extras.append(kept)
            keep_index += 1
            continue
        edge_end = epoch + d
        new_cov = edge_end - max(epoch, covered_until)
        if trust_base and new_cov < MIN_UNIQUE_COVERAGE_SEC:
            f.unlink(missing_ok=True)
        else:
            kept = _edge_keep_path(base_final, keep_index)
            f.rename(kept)
            extras.append(kept)
            keep_index += 1
            covered.append((epoch, edge_end))
            covered_until = max(covered_until, edge_end)

    # 5) interrupt fragment pairs
    extras += await _recover_fragments(media, parts_dir, base_final, covered)

    # 6) parts dir cleanup
    shutil.rmtree(parts_dir, ignore_errors=True)

    size_bytes = base_final.stat().st_size if base_final.exists() else 0

    # Best-effort: append summary record (mirrors edge-only behavior).
    try:
        from linkstart.summary import append_recording_record
        append_recording_record(
            platform=channel.platform,
            channel_id=channel.channel_id,
            file_path=base_final,
            size_bytes=size_bytes,
            duration_sec=base_duration,
        )
    except Exception:
        log.exception("failed to append recording record")

    return DownloadResult(
        success=True,
        file_path=base_final,
        extra_files=extras,
        size_bytes=size_bytes,
        duration_sec=base_duration,
        retry_count=retry_count,
    )


async def _cleanup_no_base(
    paths,
    media,
    channel: ChannelConfig,
    live: LiveInfo,
    parts_dir: Path,
    *,
    retry_count: int,
) -> DownloadResult:
    """Fallback when no usable full.*.mp4 exists: promote edge files, or recover fragment pairs."""
    edge_files = [
        f for f in sorted(parts_dir.glob("edge.*.mp4"))
        if _parse_epoch(f.name) is not None
    ]

    extras: list[Path] = []
    if edge_files:
        base_final = unique_path(paths.final_path(channel, live))
        edge_files[0].rename(base_final)
        for i, f in enumerate(edge_files[1:], start=1):
            kept = _edge_keep_path(base_final, i)
            f.rename(kept)
            extras.append(kept)
    else:
        # Last-ditch: try fragment recovery into a fresh base.
        base_final = unique_path(paths.final_path(channel, live))
        recovered = await _recover_fragments(media, parts_dir, base_final, covered=[])
        if not recovered:
            paths.discard_parts_dir(parts_dir)
            return DownloadResult(
                success=False,
                error="no usable recordings produced",
                retry_count=retry_count,
            )
        # Promote the first recovered file to base, the rest become extras.
        recovered[0].rename(base_final)
        for i, f in enumerate(recovered[1:], start=1):
            kept = _edge_keep_path(base_final, i)
            f.rename(kept)
            extras.append(kept)

    base_duration = await media.ffprobe_duration(base_final) or 0
    shutil.rmtree(parts_dir, ignore_errors=True)

    size_bytes = base_final.stat().st_size if base_final.exists() else 0

    # Best-effort: append summary record.
    try:
        from linkstart.summary import append_recording_record
        append_recording_record(
            platform=channel.platform,
            channel_id=channel.channel_id,
            file_path=base_final,
            size_bytes=size_bytes,
            duration_sec=base_duration,
        )
    except Exception:
        log.exception("failed to append recording record")

    return DownloadResult(
        success=True,
        file_path=base_final,
        extra_files=extras,
        size_bytes=size_bytes,
        duration_sec=base_duration,
        retry_count=retry_count,
    )


async def _recover_fragments(
    media,
    parts_dir: Path,
    base_final: Path,
    covered: list[Interval],
) -> list[Path]:
    """Remux *.f<itag>.mp4(.part) fragment pairs that add unique coverage beyond `covered`."""
    groups: dict[str, list[Path]] = {}
    seen: set[Path] = set()
    for pattern in ("*.f*.mp4.part", "*.f*.mp4"):
        for p in parts_dir.glob(pattern):
            if p in seen:
                continue
            seen.add(p)
            prefix = p.name.split(".f", 1)[0]
            groups.setdefault(prefix, []).append(p)

    extras: list[Path] = []
    keep_index = 1
    for prefix, members in groups.items():
        if len(members) != 2:
            continue
        members.sort()   # deterministic: f137 < f140 typically
        video, audio = members[0], members[1]

        d = await media.ffprobe_duration(video)
        if d is not None:
            # Approximate the recording epoch by the file's mtime.
            epoch = int(video.stat().st_mtime)
            frag_iv: Interval = (epoch, epoch + d)
            if _new_coverage_seconds(frag_iv, covered) < MIN_UNIQUE_COVERAGE_SEC:
                continue   # already covered → skip

        target = _recovered_keep_path(base_final, keep_index)
        if await media.ffmpeg_remux(video, audio, target):
            extras.append(target)
            keep_index += 1
            if d is not None:
                covered.append(frag_iv)
    return extras
