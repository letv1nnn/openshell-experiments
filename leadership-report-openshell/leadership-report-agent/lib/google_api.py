"""Shared HTTP client for Google APIs."""

import json
import urllib.error
import urllib.request

from .errors import AgentError


def google_api_request(method: str, url: str, headers: dict, body=None) -> dict:
    """Make an HTTP request to a Google API and return the parsed JSON response.

    Raises AgentError on HTTP or network failure.
    """
    data = json.dumps(body).encode("utf-8") if body else None
    req_headers = {**headers}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:500]
        if e.code == 401:
            raise AgentError(
                f"Authentication rejected by API (401). Token may be expired.",
                retriable=False,
            ) from e
        if e.code == 403:
            raise AgentError(
                f"Access denied (403). Check scopes or permissions.\n{err_body}",
                retriable=False,
            ) from e
        if e.code == 404:
            raise AgentError(
                f"Resource not found (404).\n{err_body}",
                retriable=False,
            ) from e
        if e.code >= 500:
            raise AgentError(
                f"Server error ({e.code}). Retry later.\n{err_body}",
                retriable=True,
            ) from e
        raise AgentError(f"HTTP {e.code}\n{err_body}", retriable=False) from e
    except urllib.error.URLError as e:
        raise AgentError(f"Network error: {e.reason}", retriable=True) from e
