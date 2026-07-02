#!/usr/bin/env python3
"""Fetch a Google Doc as plain text.

Usage:
    python -m lib.fetch_gdoc <output_path>                  # auto-fetch latest Gemini notes
    python -m lib.fetch_gdoc <doc_id_or_url> <output_path>  # fetch specific doc

Calendar mode searches Google Drive for the most recent Gemini notes document
matching the meeting name (set via MEETING_NAME env var).

Environment variables:
    MEETING_NAME             Meeting name to search for in Google Drive (required)
    GOOGLE_QUOTA_PROJECT     GCP project for API quota

Exit codes:
    0  Success
    1  Usage error
    2  Permanent failure (bad config, missing doc, auth invalid)
    3  Transient failure (network, server error — safe to retry)

Requires:
    Local:  gcloud auth login --enable-gdrive-access
"""

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from .auth import get_auth_headers
from .errors import AgentError, AuthError, EXIT_USAGE, EXIT_PERMANENT, EXIT_TRANSIENT
from .google_api import google_api_request


def extract_doc_id(input_str: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", input_str)
    if match:
        return match.group(1)
    return input_str


def export_doc(doc_id: str, output_path: str, headers: dict) -> None:
    url = (
        f"https://www.googleapis.com/drive/v3/files/{doc_id}"
        f"/export?mimeType=text/plain"
    )
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 404:
            raise AgentError(f"Document {doc_id} not found or not accessible.", retriable=False) from e
        if e.code >= 500:
            raise AgentError(f"Drive export error ({e.code}). Retry later.\n{body}", retriable=True) from e
        raise AgentError(f"Export failed ({e.code})\n{body}", retriable=False) from e
    except urllib.error.URLError as e:
        raise AgentError(f"Network error during export: {e.reason}", retriable=True) from e

    with open(output_path, "wb") as f:
        f.write(content)
    print(f"FETCH_OK: {doc_id} -> {output_path} ({len(content)} bytes)")


def find_latest_notes_doc(headers: dict, meeting_name: str) -> str:
    """Search Drive for the most recent Gemini notes doc matching the meeting name."""
    safe_name = meeting_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name contains '{safe_name}' "
        f"and mimeType = 'application/vnd.google-apps.document' "
        f"and trashed = false"
    )
    params = urllib.parse.urlencode({
        "q": query,
        "orderBy": "createdTime desc",
        "pageSize": 5,
        "fields": "files(id,name,createdTime)",
    })
    url = f"https://www.googleapis.com/drive/v3/files?{params}"

    result = google_api_request("GET", url, headers)
    files = result.get("files", [])

    if not files:
        raise AgentError(
            f"No Google Doc found matching '{meeting_name}'. "
            f"Gemini note-taking may not be enabled for this meeting.",
            retriable=False,
        )

    doc = files[0]
    print(f"Found: \"{doc['name']}\" (created {doc['createdTime']})")
    return doc["id"]


def fetch_notes(output_path: str, meeting_name: str | None = None, doc_ref: str | None = None) -> str:
    """Fetch meeting notes to output_path. Returns the output path on success.

    Raises AuthError on auth failure. Raises AgentError on fetch failure.
    """
    headers = get_auth_headers()

    if doc_ref:
        doc_id = extract_doc_id(doc_ref)
    else:
        name = meeting_name or os.environ.get("MEETING_NAME", "").strip()
        if not name:
            raise AgentError(
                "MEETING_NAME environment variable is required. "
                "Set it to the Google Meet meeting name to search for.",
                retriable=False,
            )
        print(f"Searching for Gemini notes matching '{name}'...")
        doc_id = find_latest_notes_doc(headers, name)

    export_doc(doc_id, output_path, headers)
    return output_path


def main():
    if len(sys.argv) == 2:
        output = sys.argv[1]
        try:
            fetch_notes(output, doc_ref=None)
        except (AuthError, AgentError) as e:
            print(f"FETCH_FAILED: {e}", file=sys.stderr)
            sys.exit(EXIT_TRANSIENT if e.retriable else EXIT_PERMANENT)

    elif len(sys.argv) == 3:
        input_str = sys.argv[1]
        output = sys.argv[2]
        try:
            fetch_notes(output, doc_ref=input_str)
        except (AuthError, AgentError) as e:
            print(f"FETCH_FAILED: {e}", file=sys.stderr)
            sys.exit(EXIT_TRANSIENT if e.retriable else EXIT_PERMANENT)

    else:
        print(
            f"Usage:\n"
            f"  {sys.argv[0]} <output_path>                  # auto-fetch from Drive\n"
            f"  {sys.argv[0]} <doc_id_or_url> <output_path>  # fetch specific doc",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)


if __name__ == "__main__":
    main()
