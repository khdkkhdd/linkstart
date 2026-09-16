"""Tests for the surgical ADTS cleaner (TwitCasting boundary-filler removal)."""
import pytest

from linkstart.downloader import _adts


# ---------- synthetic stream builders ----------

def adts_frame(payload: bytes, sr_idx: int = 3, ch: int = 2) -> bytes:
    """A structurally valid ADTS frame (AAC-LC) wrapping `payload`."""
    fl = len(payload) + 7
    hdr = bytearray(7)
    hdr[0] = 0xFF
    hdr[1] = 0xF1
    hdr[2] = (1 << 6) | (sr_idx << 2) | ((ch >> 2) & 1)
    hdr[3] = ((ch & 3) << 6) | ((fl >> 11) & 3)
    hdr[4] = (fl >> 3) & 0xFF
    hdr[5] = ((fl & 7) << 5) | 0x1F
    hdr[6] = 0xFC
    return bytes(hdr) + payload


def music(i: int, size: int = 300) -> bytes:
    """A unique 'real audio' frame — content varies with i."""
    return adts_frame(bytes((i + j) % 251 for j in range(size)))


def ts_packet(pid: int, payload: bytes, *, pusi: bool = False, cc: int = 0) -> bytes:
    assert len(payload) <= 184
    p = bytearray(188)
    p[0] = 0x47
    p[1] = (0x40 if pusi else 0) | (pid >> 8)
    p[2] = pid & 0xFF
    p[3] = 0x10 | (cc & 0x0F)
    if len(payload) < 184:
        # adaptation field pads the packet to 188 bytes
        p[3] |= 0x20
        af_len = 183 - len(payload)
        p[4] = af_len
        if af_len > 0:
            p[5] = 0x00
        start = 4 + 1 + af_len
    else:
        start = 4
    p[start:start + len(payload)] = payload
    return bytes(p)


def pes_header(es_first: bytes) -> bytes:
    """Minimal PES header (with PTS) followed by the first ES bytes."""
    return b"\x00\x00\x01\xc0\x00\x00\x80\x80\x05" + b"\x21\x00\x01\x00\x01" + es_first


def es_to_ts(es: bytes, pid: int = 257, chunk: int = 170) -> bytes:
    """Pack an elementary stream into TS packets, one PES per call."""
    out = b""
    first = pes_header(es[:chunk])
    out += ts_packet(pid, first[:184], pusi=True, cc=0)
    rest = first[184:] + es[chunk:]
    cc = 1
    while rest:
        out += ts_packet(pid, rest[:184], cc=cc)
        rest = rest[184:]
        cc = (cc + 1) % 16
    return out


# ---------- extract_audio_es ----------

def test_extract_recovers_es_across_continuation_packets():
    es = b"".join(music(i) for i in range(30))
    ts = es_to_ts(es, pid=257)
    assert _adts.extract_audio_es(ts, 257) == es


def test_find_audio_pid_picks_the_adts_stream():
    audio = es_to_ts(b"".join(music(i) for i in range(10)), pid=257)
    video = es_to_ts(b"\x00\x00\x00\x01\x67" + b"v" * 500, pid=256)
    assert _adts.find_audio_pid(video + audio) == 257


def test_find_audio_pid_none_when_no_adts():
    video = es_to_ts(b"\x00\x00\x00\x01\x67" + b"v" * 500, pid=256)
    assert _adts.find_audio_pid(video) is None


# ---------- clean_adts ----------

def test_clean_passes_pure_stream_through_byte_exact():
    es = b"".join(music(i) for i in range(50))
    cleaned, stats = _adts.clean_adts(es)
    assert cleaned == es
    assert stats["frames_dropped"] == 0


def test_clean_removes_unparseable_junk_between_frames():
    frames = [music(i) for i in range(40)]
    es = b"".join(frames[:20]) + b"\x00\x01\x02" * 500 + b"".join(frames[20:])
    cleaned, stats = _adts.clean_adts(es)
    assert cleaned == b"".join(frames)
    assert stats["junk_regions"] >= 1


def test_clean_drops_wrong_config_frames():
    # 44.1kHz mono pseudo-frames chained inside a 48kHz stereo stream.
    fakes = adts_frame(b"z" * 100, sr_idx=4, ch=1) * 3
    frames = [music(i) for i in range(40)]
    es = b"".join(frames[:20]) + fakes + b"".join(frames[20:])
    cleaned, _ = _adts.clean_adts(es)
    assert cleaned == b"".join(frames)


def test_clean_drops_repeated_filler_frames_after_junk():
    """Repeated boundary fillers all go; real frames stay byte-exact."""
    filler = adts_frame(b"\x39\xed\x65" * 120)   # constant content
    real = [music(i) for i in range(300)]
    es = b""
    idx = 0
    for seg in range(6):   # 6 segment boundaries
        es += b"\xde\xad" * 200          # junk blob
        es += filler * 4                  # repeated fakes (count 24 >= threshold)
        for _ in range(50):
            es += real[idx]
            idx += 1
    cleaned, stats = _adts.clean_adts(es)
    assert cleaned == b"".join(real)
    assert stats["frames_dropped"] == 24


def test_clean_keeps_repeated_frames_far_from_boundaries():
    """Encoded silence repeats identically — away from junk it must survive."""
    silence = adts_frame(b"\x00" * 60)
    head = [music(i) for i in range(150)]
    tail = [music(1000 + i) for i in range(150)]
    es = b"\xde\xad" * 100 + b"".join(head) + silence * 40 + b"".join(tail)
    cleaned, _ = _adts.clean_adts(es)
    assert cleaned == b"".join(head) + silence * 40 + b"".join(tail)


def test_clean_keeps_unique_frames_adjacent_to_junk():
    """Unique frames after a boundary survive — the repeat rule protects them."""
    real = [music(i) for i in range(120)]
    es = b"".join(real[:60]) + b"\xde\xad" * 300 + b"".join(real[60:])
    cleaned, _ = _adts.clean_adts(es)
    assert cleaned == b"".join(real)


def test_clean_short_stream_with_few_boundaries_still_drops_fillers():
    """Threshold adapts to boundary count so short captures are cleaned too."""
    filler = adts_frame(b"\x39\xed\x65" * 120)
    real = [music(i) for i in range(90)]
    es = b""
    idx = 0
    for seg in range(3):
        es += b"\xde\xad" * 200 + filler * 2
        for _ in range(30):
            es += real[idx]
            idx += 1
    cleaned, _ = _adts.clean_adts(es)
    assert cleaned == b"".join(real)


def test_clean_empty_and_garbage_only_inputs():
    assert _adts.clean_adts(b"")[0] == b""
    assert _adts.clean_adts(b"\xde\xad" * 1000)[0] == b""


def test_clean_drops_truncated_final_frame():
    """A capture killed mid-frame ends with a partial frame — including those
    bytes makes the output undecodable, so the tail must be dropped."""
    frames = [music(i) for i in range(30)]
    es = b"".join(frames) + music(99)[:150]   # final frame cut short
    cleaned, _ = _adts.clean_adts(es)
    assert cleaned == b"".join(frames)
