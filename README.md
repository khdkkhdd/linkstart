# LinkStart

Multi-platform live stream recorder. Detects when configured channels go live, records them via yt-dlp, and notifies you on Discord — automatically restarts on network blips, handles `Ctrl+C` gracefully, and saves a clean `.mp4` when the broadcast ends.

## Features

- Records **YouTube**, **TwitCasting**, **Chzzk** live streams
- **Smart restart**: yt-dlp dies → auto-relaunch as long as the broadcast is still live
- **Dual-loop recording** for YouTube (`--live-from-start` + live-edge, deduped after broadcast ends)
- **Visible interruption boundaries** in edge-only mode: each restart attempt is its own `.mp4`, no silent gap-hiding concat
- **Graceful `Ctrl+C`**: terminates yt-dlp cleanly, finalizes the current recording, exits
- **Discord webhooks** for live detected / recording started / interrupted / finished / errors
- **Daily summary** (optional cron-scheduled digest)
- **Auth-gated streams** via browser cookies (member-only / 19+ content)
- **Channel alias** for human-readable paths and notifications

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- `yt-dlp`, `ffmpeg`, `ffprobe` on `PATH`

## Quick start

```bash
# 1) Install
uv sync

# 2) Configure
cp config.example.yaml config.yaml
$EDITOR config.yaml

# 3) Verify a channel is reachable
uv run linkstart check 0

# 4) Run the daemon
uv run linkstart run
```

Use `tmux` / `nohup` / `launchd` to keep it running in the background.

## Configuration

`config.yaml` (gitignored). Minimal example:

```yaml
defaults:
  save_dir: ~/Downloads/LinkStart      # output root (~/expansion supported)
  poll_interval: 30                    # seconds between live-state checks

notifiers:
  - id: main
    type: discord
    webhook_url: https://discord.com/api/webhooks/.../...

channels:
  - platform: twitcasting
    channel_id: somehandle             # platform-facing id (see below)
    alias: mychannel                   # display name in paths/logs/Discord
    notifier: main
    # cookies_from_browser: chrome     # for member-only / 19+ streams
    # format: "299+140"                # override platform default format
    # poll_interval: 60                # per-channel override

summary:
  enabled: true
  cron: "0 9 * * *"                    # daily 9 AM digest
  notifier: main
```

### Finding `channel_id` per platform

| Platform | Use | Where to find it |
|---|---|---|
| TwitCasting | URL handle | `https://twitcasting.tv/<HERE>` |
| YouTube | `@handle` or `UCxxxx` channel ID | `https://www.youtube.com/<HERE>` |
| Chzzk | 32-char hex UUID | `https://chzzk.naver.com/<HERE>` — **not** the channel name |

`alias` is optional but recommended for Chzzk (UUIDs are unreadable).

## Supported platforms

| Platform | Status | Mode |
|---|---|---|
| TwitCasting | ✅ implemented | edge-only |
| Chzzk | ✅ implemented | edge-only |
| YouTube | ✅ implemented | dual-loop |

Adding a new platform = a single class implementing `Platform` (see `src/linkstart/platforms/base.py`).

## Recording strategy

Each platform records in one of two modes (chosen by `supports_live_from_start`):

- **Edge-only** (TwitCasting, Chzzk): single yt-dlp loop from the moment the broadcast is detected. yt-dlp handles HLS fragment retries; the Downloader restarts immediately if yt-dlp exits while the broadcast is still live. **Each restart attempt becomes its own `.mp4`** so gaps between attempts are visible, not silently glued together.
- **Dual-loop** (YouTube): two yt-dlp instances run concurrently — one with `--live-from-start` (5s between restarts) and one without (no sleep, follows the live edge). When the broadcast ends, ffprobe-based coverage dedup — anchored to the platform-reported broadcast start time — picks the longest from-start file as the base and keeps only edge fragments that add unique timeline coverage (≥ 5s). Output: one base `.mp4` plus zero or more `.edge_NNN.mp4` / `.recovered_NNN.mp4`.

When yt-dlp itself crashes mid-recording, a `DOWNLOAD_INTERRUPTED` Discord embed fires (deduped within a 5-minute window so flaky uplinks don't spam) and the loop restarts automatically. If yt-dlp exits repeatedly without producing any data (unreadable cookies, disk full, ...), the recorder gives up after 3 consecutive attempts and surfaces the captured yt-dlp stderr as an `ERROR` notification instead of spinning silently — the next poll retries.

### Per-channel format override

For dual-loop platforms the two yt-dlp instances must download identical media for dedup to work. Default formats are platform-supplied, but overridable:

```yaml
channels:
  - platform: youtube
    channel_id: "@somechannel"
    format: "299+140"     # overrides platform default
```

## Output structure

```
~/Downloads/LinkStart/
├── twitcasting/mychannel/
│   ├── 2026-06-04_BroadcastTitle.mp4              # attempt 0
│   ├── 2026-06-04_BroadcastTitle.part_001.mp4    # attempt 1 (after a network blip)
│   └── 2026-06-04_BroadcastTitle.part_002.mp4    # attempt 2
└── youtube/yourstreamer/
    ├── 2026-06-04_LiveTitle.mp4                   # base (longest from-start)
    ├── 2026-06-04_LiveTitle.edge_001.mp4         # edge tail past base end
    └── 2026-06-04_LiveTitle.recovered_001.mp4    # ffmpeg-remuxed leftover fragment
```

State (last seen `live_id` per channel) lives at `$XDG_STATE_HOME/linkstart/state.json` (default `~/.local/state/linkstart/`). The summary log is `recordings.jsonl` in the same directory.

## Discord notifications

| Event | When |
|---|---|
| 🔴 LIVE_STARTED | A new broadcast was detected |
| 📥 DOWNLOAD_STARTED | yt-dlp launched |
| ⚠️ DOWNLOAD_INTERRUPTED | yt-dlp died mid-broadcast (5-min dedup per channel) |
| ✅ DOWNLOAD_FINISHED | Broadcast ended; final mp4 saved (with size / duration / retry count) |
| ❌ ERROR | Recording failed — includes the yt-dlp error output (rate-limited to one per 5 min per channel) |
| 📊 SUMMARY | Daily digest at the configured cron time |

The webhook URL is treated as a secret — keep `config.yaml` out of version control (it is gitignored by default).

## CLI

```bash
uv run linkstart run                  # run the recorder daemon
uv run linkstart list                 # list configured channels + last-seen live ids
uv run linkstart check <index>        # one-shot live-state check for channel at this index
```

Stop with `Ctrl+C`: yt-dlp receives `SIGTERM`, flushes its current part, ffmpeg remuxes, the final mp4 is saved, then the daemon exits. A second `Ctrl+C` force-exits without cleanup.

Restarting the daemon mid-broadcast needs no special care: while a channel is live, it gets recorded — the new session simply continues into a new file (`..._2.mp4`).

## Member-only / authenticated streams

For YouTube member-only, age-restricted, or Chzzk 19+/subscriber-only content, point LinkStart at your browser's cookie store:

```yaml
- platform: chzzk
  channel_id: <uuid>
  alias: mychannel
  notifier: main
  cookies_from_browser: firefox    # or chrome / edge / brave / opera / safari
```

LinkStart extracts the relevant domain's cookies from the named browser and forwards them to both the live-state API call and yt-dlp. **You must be logged in on that browser** for this to work (the browser does not need to be running). If the session expires you'll see `live=false` for gated content.

Firefox is the most reliable source for an unattended daemon: on macOS, Chrome triggers a Keychain prompt on first access and Safari requires Full Disk Access for the daemon's terminal. Log in once in Firefox with "stay signed in" checked.

## FAQ

**Q: I see `chzzk: request failed for ...` for a Chzzk channel.**
The Chzzk API rejects requests without a browser-like `User-Agent` — already handled in code. If you still hit this, the channel UUID is probably wrong, or your network can't reach `api.chzzk.naver.com`.

**Q: The broadcast ended but the recording never finalized.**
Check the `.parts/` directory under the channel's save dir — yt-dlp may still be running. The Downloader confirms broadcast-end via the platform API (with 3 retries × 5s). If the platform API is also down, finalization waits. `Ctrl+C` forces a graceful stop.

**Q: Discord webhook isn't firing.**
Test it directly:
```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"content":"test"}' "<your webhook URL>"
```
If Discord shows the message, the webhook is fine — check `config.yaml` `notifier:` matches the `notifiers[].id`.

**Q: Can I record multiple channels of the same platform?**
Yes — add multiple `channels:` entries with different `channel_id`. Worker tasks run independently per channel.

**Q: Same alias on two platforms?**
Fine. Files live under `{platform}/{alias}/`, so platform separates them. State is keyed by `(platform, channel_id)`, never `alias`.

## Tests

```bash
uv run pytest -q
```

## License

MIT — see [LICENSE](LICENSE).

LinkStart is intended for personal archival of broadcasts you have the right to record. Respect each platform's terms of service and applicable copyright law.
