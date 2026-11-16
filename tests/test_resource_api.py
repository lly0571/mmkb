import asyncio
from io import BytesIO

import pytest

from mmkb.api import OriginalResponse


def test_response_closes_original_when_sending_fails():
    stream = BytesIO(b"image data")
    response = OriginalResponse(stream, "image/png")

    async def send(message):
        if message["type"] == "http.response.body":
            raise RuntimeError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError, match="client disconnected"):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert stream.closed


def test_response_closes_original_when_reading_fails():
    class BrokenStream(BytesIO):
        def read(self, size=-1):
            raise RuntimeError("storage connection lost")

    stream = BrokenStream()
    response = OriginalResponse(stream, "image/png")

    async def send(message):
        pass

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError, match="storage connection lost"):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert stream.closed
