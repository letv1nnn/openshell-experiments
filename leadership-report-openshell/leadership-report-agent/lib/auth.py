"""Shared Google OAuth2 authentication.

In Kubernetes/sandboxed environments: reads individual credential values from
environment variables and exchanges for an access token.

Locally: falls back to `gcloud auth print-access-token`.

Environment variables:
    GOOGLE_CLIENT_ID         OAuth2 client ID
    GOOGLE_CLIENT_SECRET     OAuth2 client secret
    GOOGLE_REFRESH_TOKEN     OAuth2 refresh token
    GOOGLE_QUOTA_PROJECT     GCP project for API quota
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .errors import AuthError, EXIT_PERMANENT, EXIT_TRANSIENT

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def get_token() -> str:
    """Return a valid Google OAuth2 access token."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        return _token_from_env(client_id, client_secret, refresh_token)
    return _token_from_gcloud()


def get_auth_headers() -> dict:
    """Return headers for authenticated API calls (token + quota project if set)."""
    headers = {"Authorization": f"Bearer {get_token()}"}
    quota_project = os.environ.get("GOOGLE_QUOTA_PROJECT")
    if quota_project:
        headers["x-goog-user-project"] = quota_project
    return headers


def _token_from_env(client_id: str, client_secret: str, refresh_token: str) -> str:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            return result["access_token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 400 and "invalid_grant" in body:
            raise AuthError(
                f"Refresh token expired or revoked. Re-authenticate and update the secret.\n{body}",
                retriable=False,
            ) from e
        if e.code == 401:
            raise AuthError(
                f"Invalid client credentials (client_id/client_secret mismatch).\n{body}",
                retriable=False,
            ) from e
        if e.code >= 500:
            raise AuthError(
                f"Google token endpoint unavailable ({e.code}). Retry later.\n{body}",
                retriable=True,
            ) from e
        raise AuthError(f"Token refresh failed ({e.code}): {body}", retriable=False) from e
    except urllib.error.URLError as e:
        raise AuthError(
            f"Network error reaching token endpoint: {e.reason}",
            retriable=True,
        ) from e


def _token_from_gcloud() -> str:
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        raise AuthError("gcloud CLI not found. Install it or set GOOGLE_CREDENTIALS_FILE.", retriable=False)
    except subprocess.TimeoutExpired:
        raise AuthError("gcloud auth timed out after 30s.", retriable=True)
    except subprocess.CalledProcessError as e:
        raise AuthError(f"gcloud auth failed: {e.stderr.strip()}", retriable=False) from e


if __name__ == "__main__":
    try:
        headers = get_auth_headers()
        print("AUTH_OK")
        print(f"Token length: {len(headers['Authorization'])} chars")
        if "x-goog-user-project" in headers:
            print(f"Quota project: {headers['x-goog-user-project']}")
    except AuthError as e:
        print(f"AUTH_FAILED: {e}", file=sys.stderr)
        sys.exit(EXIT_PERMANENT if not e.retriable else EXIT_TRANSIENT)
