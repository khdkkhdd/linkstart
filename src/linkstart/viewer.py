"""Read-only recording browser. Bind to localhost and share through Tailscale Serve."""
import asyncio
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from linkstart.config import AppConfig


def create_app(config: AppConfig) -> web.Application:
    roots = list(dict.fromkeys(
        p.resolve() for p in [config.defaults.save_dir, *(
            ch.save_dir for ch in config.channels if ch.save_dir is not None
        )]
    ))

    def recordings():
        items = []
        seen = set()
        for idx, root in enumerate(roots):
            for path in root.glob("*/*/*.mp4"):
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root) or resolved in seen:
                        continue
                    stat = resolved.stat()
                    if not resolved.is_file() or stat.st_size == 0:
                        continue
                    seen.add(resolved)
                    relative = path.relative_to(root)
                    items.append(dict(
                        name=path.stem, platform=relative.parts[0],
                        channel=relative.parts[1], size=stat.st_size,
                        modified=stat.st_mtime,
                        url=f"/media/{idx}/{quote(relative.as_posix(), safe='/')}",
                    ))
                except OSError:
                    continue
        return sorted(items, key=lambda item: (item["modified"], item["name"]), reverse=True)

    async def index(request):
        return web.FileResponse(Path(__file__).with_name("viewer.html"))

    async def listing(request):
        return web.json_response(await asyncio.to_thread(recordings), headers={"Cache-Control": "no-store"})

    async def media(request):
        try:
            idx = int(request.match_info["root"])
            if not 0 <= idx < len(roots):
                raise ValueError
            root = roots[idx]
            relative = Path(request.match_info["path"])
            path = (root / relative).resolve()
            if (relative.is_absolute() or len(relative.parts) != 3
                    or any(part.startswith(".") for part in relative.parts)
                    or not path.is_relative_to(root) or path.suffix != ".mp4"
                    or not path.is_file()):
                raise ValueError
        except (ValueError, OSError):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "video/mp4"})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/recordings", listing)
    app.router.add_get("/media/{root}/{path:.*}", media)
    return app
