import pytest
from asyncgh import client


@pytest.fixture(autouse=True)
def _reset_http_client():
    # httpx2.AsyncClient is bound to the event loop it was created under, and
    # pytest-asyncio's default function-scoped loop means a default client
    # surviving across tests would raise "attached to a different loop"
    # errors.
    client._default_client = None
    yield
    client._default_client = None
