#!/usr/bin/env python3
"""Leadership Report Agent — self-contained pipeline.

Fetches meeting notes from Google Drive, analyzes them via an LLM
(vLLM / OpenAI-compatible endpoint), generates a structured leadership
report, and pushes it to a Google Doc.

Environment variables:
    OPENAI_BASE_URL          Inference endpoint (default: https://inference.local/v1)
    OPENAI_API_KEY           API key ("none" for unauthenticated vLLM)
    MODEL_ID                 Model identifier served by the endpoint
    LEADERSHIP_DOC_ID        Google Doc ID to push the report to (required)
    GOOGLE_QUOTA_PROJECT     GCP project for API quota
    MEETING_NAME             Google Meet meeting name to search for (required)

Exit codes:
    0  Success
    1  Usage error
    2  Permanent failure (auth invalid, doc missing, bad config)
    3  Transient failure (network, server error)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from lib.errors import AgentError, EXIT_OK, EXIT_PERMANENT, EXIT_TRANSIENT
from lib.auth import get_auth_headers
from lib.fetch_gdoc import fetch_notes
from lib.llm import analyze_notes
from lib.push_to_gdoc import push_report

NOTES_PATH = "/tmp/meeting_notes.txt"
REPORT_PATH = "/tmp/leadership_report.json"


def log(step: str, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{step}] {message}")


def run() -> int:
    log("init", "Leadership Report Agent starting")

    try:
        log("auth", "Checking Google credentials...")
        get_auth_headers()
        log("auth", "AUTH_OK")

        log("fetch", "Fetching meeting notes from Google Drive...")
        meeting_name = os.environ.get("MEETING_NAME")
        fetch_notes(NOTES_PATH, meeting_name=meeting_name)

        with open(NOTES_PATH) as f:
            notes_text = f.read()

        if not notes_text.strip():
            log("fetch", "FETCH_FAILED: Meeting notes file is empty.")
            return EXIT_PERMANENT

        log("fetch", f"Fetched {len(notes_text)} bytes of meeting notes")

        log("llm", "Sending notes to LLM for analysis...")
        report = analyze_notes(notes_text)
        log("llm", f"Report generated: {report['date']}, {len(report['bullets'])} bullets")

        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        log("llm", f"Report JSON written to {REPORT_PATH}")

        log("push", "Pushing report to Google Doc...")
        push_report(report)

    except AgentError as e:
        log("error", str(e))
        return EXIT_TRANSIENT if e.retriable else EXIT_PERMANENT

    log("done", "Leadership report published successfully")
    return EXIT_OK


def main() -> None:
    exit_code = run()

    if exit_code == EXIT_TRANSIENT:
        log("retry", "Transient failure — retrying in 30s...")
        time.sleep(30)
        exit_code = run()
        if exit_code != EXIT_OK:
            log("retry", "Retry failed. Giving up.")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
