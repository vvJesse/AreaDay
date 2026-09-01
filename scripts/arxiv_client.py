"""Small arXiv client construction helpers."""

from __future__ import annotations

from typing import Any


def make_arxiv_client(
    *,
    page_size: int,
    delay_seconds: float = 3.0,
    num_retries: int = 2,
    request_timeout_seconds: float = 30.0,
) -> Any:
    """Build arxiv.Client with its courtesy delay and a bounded HTTP request.

    arxiv 4.x does not expose a timeout constructor argument. It does keep a
    requests.Session on the client, so a Session subclass is the narrowest way
    to supply a default without replacing arxiv's pagination, delay, or retry
    logic. An explicit timeout passed by the library in a future release wins.
    """
    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")

    import arxiv
    import requests

    class _DefaultTimeoutSession(requests.Session):
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", request_timeout_seconds)
            return super().request(method, url, **kwargs)

    client = arxiv.Client(
        page_size=page_size,
        delay_seconds=delay_seconds,
        num_retries=num_retries,
    )
    previous_session = getattr(client, "_session", None)
    client._session = _DefaultTimeoutSession()
    if previous_session is not None:
        previous_session.close()
    return client
