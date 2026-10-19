from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from linkstart.config import AppConfig, Defaults
from linkstart.viewer import create_app


@pytest.mark.asyncio
async def test_listing_and_range_with_unicode_paths(tmp_path):
    root = tmp_path / 'recordings'
    channel = root / 'twitcasting' / 'Ao'
    channel.mkdir(parents=True)
    video = channel / '방송 제목.mp4'
    video.write_bytes(b'0123456789')
    parts = channel / 'unfinished.parts'
    parts.mkdir()
    (parts / 'partial.mp4').write_bytes(b'partial')
    outside = tmp_path / 'secret.mp4'
    outside.write_bytes(b'secret')
    (channel / 'escape.mp4').symlink_to(outside)
    async with TestClient(TestServer(create_app(AppConfig(defaults=Defaults(save_dir=root))))) as client:
        response = await client.get('/api/recordings')
        items = await response.json()
        assert len(items) == 1
        assert items[0]['name'] == '방송 제목'
        response = await client.get(items[0]['url'], headers={'Range': 'bytes=2-5'})
        assert response.status == 206
        assert response.headers['Content-Range'] == 'bytes 2-5/10'
        assert await response.read() == b'2345'
        for url in ['/media/0/twitcasting/Ao/escape.mp4', '/media/0/twitcasting/Ao/unfinished.parts/partial.mp4', '/media/-1/twitcasting/Ao/방송 제목.mp4', '/media/0/../secret.mp4']:
            response = await client.get(url)
            assert response.status == 404
        response = await client.get('/')
        assert response.status == 200
        assert '<video' in await response.text()
