"""
Tenacity-based retry helper for registry HTTP fetches.

Registry GETs are the only thing in the backend code worth retrying: they sit
on the network boundary, the upstream registries (npm, PyPI) occasionally
serve a transient 5xx, and a quick second look usually clears it. Everything
that happens after the response is in our hands (JSON decoding, schema
validation, dependency parsing) is deterministic and a retry can't help.

`get_with_retry` wraps a single `httpx.AsyncClient.get` with exponential
backoff and a small attempt cap. The retry policy:

- `httpx.TransportError` (DNS, connect, read timeout) is always retried.
- A 5xx status code is retried by raising a sentinel inside the wrapper so
  tenacity sees a retryable failure; the response is returned normally on
  the final attempt.
- 4xx and 2xx responses bypass retry and are returned to the caller, who
  decides whether they're errors (e.g. 404 means the package doesn't
  exist).

The total worst-case wall time with the defaults is around five seconds
across three attempts, which is short enough to keep CLI feel snappy while
still giving a hiccupping registry a chance to recover.
"""

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)


class Retryable5xx(Exception):
    """Sentinel raised inside `retried_get` to mark a 5xx response as worth retrying."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"upstream returned HTTP {response.status_code}")
        self.response = response


retry_decorator = retry(
    retry=retry_if_exception_type((httpx.TransportError, Retryable5xx)),
    wait=wait_exponential_jitter(initial=0.25, max=4.0),
    stop=stop_after_attempt(3),
    reraise=True,
)


@retry_decorator
async def retried_get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """The retry-wrapped GET. Use `get_with_retry` from production code; this is the raw entrypoint that tests reach into to inspect or override the tenacity policy."""
    res = await http.get(url)
    if 500 <= res.status_code < 600:
        raise Retryable5xx(res)
    return res


async def get_with_retry(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """
    `GET url` with retry for transport errors and 5xx responses.

    Returns the final `httpx.Response`. On success this is the first 2xx/3xx/4xx
    response received; on persistent 5xx this is the last 5xx response (after
    the attempts are exhausted); on persistent transport failure the original
    `httpx.TransportError` propagates to the caller.
    """
    try:
        return await retried_get(http, url)
    except Retryable5xx as exc:
        # Exhausted retries on a 5xx; hand the response back so the caller's
        # status check can convert it into a `RegistryError`.
        return exc.response
