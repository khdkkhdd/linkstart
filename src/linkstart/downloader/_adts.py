"""Surgical ADTS cleaner for TwitCasting's boundary-contaminated audio —
fillers repeat byte-identically at every segment boundary (real audio never
does), so they're dropped and the original frames copied byte-exact."""
import hashlib
from collections import Counter

TS_PACKET = 188
MAX_REAL_FRAME = 2048          # music at 48kHz stereo stays well below this
ANCHOR_DEPTH = 4               # frames that must chain to resync after junk
BOUNDARY_WINDOW_FRAMES = 120   # ~2.5s after junk where fillers can appear
REPEAT_MIN_CAP = 10            # repetition threshold ceiling


def _adts_header(es: bytes, i: int) -> tuple[int, int, int] | None:
    """(frame_len, sr_idx, channels) at `i`, or None."""
    if i + 7 > len(es) or es[i] != 0xFF or (es[i + 1] & 0xF6) != 0xF0:
        return None
    fl = ((es[i + 3] & 0x03) << 11) | (es[i + 4] << 3) | (es[i + 5] >> 5)
    if fl < 7:
        return None
    sr = (es[i + 2] >> 2) & 0xF
    ch = ((es[i + 2] & 1) << 2) | (es[i + 3] >> 6)
    return fl, sr, ch


def find_audio_pid(ts: bytes) -> int | None:
    """PID whose PES payloads start with ADTS sync (most votes wins)."""
    votes: Counter[int] = Counter()
    for k in range(len(ts) // TS_PACKET):
        p = ts[k * TS_PACKET:(k + 1) * TS_PACKET]
        if p[0] != 0x47 or not (p[1] & 0x40) or not (p[3] & 0x10):
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        off = 4
        if p[3] & 0x20:
            off += 1 + p[off]
        if p[off:off + 3] != b"\x00\x00\x01":
            continue
        body = off + 9 + p[off + 8]
        if body + 2 <= TS_PACKET and p[body] == 0xFF and (p[body + 1] & 0xF6) == 0xF0:
            votes[pid] += 1
    return votes.most_common(1)[0][0] if votes else None


def extract_audio_es(ts: bytes, pid: int) -> bytes:
    """Concatenate the PID's PES payloads into its elementary stream."""
    es = bytearray()
    for k in range(len(ts) // TS_PACKET):
        p = ts[k * TS_PACKET:(k + 1) * TS_PACKET]
        if p[0] != 0x47 or (((p[1] & 0x1F) << 8) | p[2]) != pid or not (p[3] & 0x10):
            continue
        off = 4
        if p[3] & 0x20:
            off += 1 + p[off]
        if p[1] & 0x40 and p[off:off + 3] == b"\x00\x00\x01":
            off = off + 9 + p[off + 8]
        es += p[off:TS_PACKET]
    return bytes(es)


def _dominant_config(es: bytes) -> tuple[int, int] | None:
    """Most common (sr_idx, channels) among sanely-sized frames."""
    votes: Counter[tuple[int, int]] = Counter()
    i = 0
    while i < len(es):
        h = _adts_header(es, i)
        if h is None or h[0] > MAX_REAL_FRAME:
            i += 1
            continue
        votes[(h[1], h[2])] += 1
        i += h[0]
    return votes.most_common(1)[0][0] if votes else None


def clean_adts(es: bytes) -> tuple[bytes, dict]:
    """Drop junk blobs and boundary fillers; return (clean bytes, stats)."""
    config = _dominant_config(es)
    if config is None:
        return b"", {"frames_kept": 0, "frames_dropped": 0,
                     "junk_regions": 0, "duration_sec": 0.0}
    n = len(es)

    def real(i: int) -> int | None:
        h = _adts_header(es, i)
        if h is None or h[0] > MAX_REAL_FRAME or (h[1], h[2]) != config:
            return None
        if i + h[0] > n:   # truncated tail (capture killed mid-frame)
            return None
        return h[0]

    def anchored(i: int) -> bool:
        for _ in range(ANCHOR_DEPTH):
            fl = real(i)
            if fl is None:
                return False
            i += fl
            if i >= n:
                return True
        return True

    # Pass 1: linear walk; on junk, rescan to the next anchored position.
    frames: list[tuple[bytes, int]] = []   # (frame, frames since last junk)
    junk_regions = 0
    i = 0
    since = 0                              # stream start counts as a boundary
    if not anchored(i):
        junk_regions += 1
        while i < n and not anchored(i):
            i += 1
    while i < n:
        fl = real(i)
        if fl is not None:
            frames.append((es[i:i + fl], since))
            since += 1
            i += fl
        else:
            junk_regions += 1
            since = 0
            i += 1
            while i < n and not anchored(i):
                i += 1

    # Pass 2: fillers recur once per boundary → threshold tracks boundary count.
    counts = Counter(hashlib.md5(f).digest() for f, _ in frames)
    repeat_min = max(3, min(REPEAT_MIN_CAP, junk_regions))
    out = bytearray()
    kept = dropped = 0
    for f, s in frames:
        if s < BOUNDARY_WINDOW_FRAMES and counts[hashlib.md5(f).digest()] >= repeat_min:
            dropped += 1
        else:
            out += f
            kept += 1

    sample_rates = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
                    22050, 16000, 12000, 11025, 8000, 7350]
    rate = sample_rates[config[0]] if config[0] < len(sample_rates) else 48000
    return bytes(out), {
        "frames_kept": kept,
        "frames_dropped": dropped,
        "junk_regions": junk_regions,
        "duration_sec": kept * 1024 / rate,
    }
