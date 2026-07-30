"""Shared aiohttp test client for OpenWebUI-scoped API server requests.

The deployed API server fails closed when the trusted OpenWebUI user header is
missing.  Endpoint tests that are not specifically exercising that guard
should therefore model a valid scoped request by default.
"""

from aiohttp.test_utils import TestClient as AiohttpTestClient


TEST_USER_ID = "test-user"
_DEFAULT_SCOPE_HEADERS = {
    "X-OpenWebUI-User-Id": TEST_USER_ID,
    "X-OpenWebUI-User-Groups": "test-group",
}


class ScopedTestClient(AiohttpTestClient):
    """TestClient with a valid OpenWebUI user scope on every request."""

    def __init__(self, *args, **kwargs):
        headers = dict(_DEFAULT_SCOPE_HEADERS)
        headers.update(kwargs.pop("headers", {}))
        super().__init__(*args, headers=headers, **kwargs)
