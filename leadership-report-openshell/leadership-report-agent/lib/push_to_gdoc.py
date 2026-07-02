#!/usr/bin/env python3
"""Insert a leadership report entry at the top of a Google Doc.

Usage:
    python -m lib.push_to_gdoc <report_json_path>

report_json_path: path to a JSON file with structure:
    {
        "date": "Jun 18, 2026",
        "summary": "One-paragraph summary ...",
        "bullets": [
            {"label": "Category Name:", "text": " Detail text ...\n"},
            ...
        ]
    }

Environment variables:
    LEADERSHIP_DOC_ID        Google Doc ID to push the report to (required)

Exit codes:
    0  Success
    1  Usage error or invalid input
    2  Permanent failure (auth invalid, access denied, bad doc structure)
    3  Transient failure (network, server error — safe to retry)

Requires:
    Local:  gcloud auth login --enable-gdrive-access
"""

import json
import os
import sys

from .auth import get_auth_headers
from .errors import AgentError, AuthError, EXIT_USAGE, EXIT_PERMANENT, EXIT_TRANSIENT
from .google_api import google_api_request
from .report import validate_report


def _get_doc_id() -> str:
    doc_id = os.environ.get("LEADERSHIP_DOC_ID", "").strip()
    if not doc_id:
        raise AgentError(
            "LEADERSHIP_DOC_ID environment variable is required. "
            "Set it to the Google Doc ID (the alphanumeric string from the doc URL).",
            retriable=False,
        )
    return doc_id


def find_insert_index(headers: dict, doc_id: str) -> int:
    """Find the index right after the 'Purpose of the document' block."""
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    doc = google_api_request("GET", url, headers)
    for elem in doc["body"]["content"]:
        if "paragraph" not in elem:
            continue
        text = ""
        for el in elem["paragraph"].get("elements", []):
            text += el.get("textRun", {}).get("content", "")
        if "please add the current date" in text.lower():
            end = elem["endIndex"]
            next_elem_end = end
            for e2 in doc["body"]["content"]:
                if e2.get("startIndex", 0) == end:
                    next_elem_end = e2.get("endIndex", end)
                    break
            return next_elem_end

    raise AgentError(
        "Could not find insertion point in document. "
        "The 'Purpose of the document' block may have been removed or changed.",
        retriable=False,
    )


def build_requests(report: dict, insert_index: int) -> list:
    date_line = report["date"] + "\n"
    blank = "\n"
    summary_line = report["summary"].strip() + "\n"

    full_text = date_line + blank + summary_line
    for b in report["bullets"]:
        full_text += b["label"] + b["text"]
    full_text += "\n\n\n"

    reqs = [{"insertText": {"location": {"index": insert_index}, "text": full_text}}]

    pos = insert_index
    pos += len(date_line) + len(blank)

    summary_start = pos
    summary_end = pos + len(summary_line)
    pos = summary_end

    bold_ranges = []
    bullet_ranges = []
    for b in report["bullets"]:
        label = b["label"]
        text = b["text"]
        bold_ranges.append((pos, pos + len(label)))
        bullet_ranges.append((pos, pos + len(label) + len(text)))
        pos += len(label) + len(text)

    for bs, be in bold_ranges:
        reqs.append({
            "updateTextStyle": {
                "range": {"startIndex": bs, "endIndex": be},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })

    for bs, be in bullet_ranges:
        reqs.append({
            "createParagraphBullets": {
                "range": {"startIndex": bs, "endIndex": be},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })

    reqs.append({
        "updateParagraphStyle": {
            "range": {"startIndex": summary_start, "endIndex": summary_end},
            "paragraphStyle": {
                "spaceBelow": {"magnitude": 12, "unit": "PT"},
                "indentFirstLine": {"unit": "PT"},
                "indentStart": {"unit": "PT"},
            },
            "fields": "spaceBelow,indentFirstLine,indentStart",
        }
    })

    for bs, be in bullet_ranges:
        reqs.append({
            "updateParagraphStyle": {
                "range": {"startIndex": bs, "endIndex": be},
                "paragraphStyle": {
                    "spaceAbove": {"magnitude": 12, "unit": "PT"},
                    "spaceBelow": {"magnitude": 12, "unit": "PT"},
                },
                "fields": "spaceAbove,spaceBelow",
            }
        })

    return reqs


def push_report(report: dict) -> None:
    """Validate and push a report dict to the Google Doc.

    Raises AuthError on auth failure. Raises AgentError on push failure.
    """
    doc_id = _get_doc_id()
    validate_report(report)
    headers = get_auth_headers()
    insert_index = find_insert_index(headers, doc_id)
    reqs = build_requests(report, insert_index)

    url = f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate"
    google_api_request("POST", url, headers, {"requests": reqs})
    print(f"PUSH_OK: Report for {report['date']} added to doc {doc_id}.")


def push_report_from_file(report_path: str) -> None:
    """Load a report JSON file and push it."""
    try:
        with open(report_path) as f:
            report = json.load(f)
    except FileNotFoundError:
        raise AgentError(f"Report file not found: {report_path}", retriable=False)
    except json.JSONDecodeError as e:
        raise AgentError(f"Invalid JSON in report file: {e}", retriable=False)

    push_report(report)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <report_json_path>", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    try:
        push_report_from_file(sys.argv[1])
    except (AuthError, AgentError) as e:
        print(f"PUSH_FAILED: {e}", file=sys.stderr)
        sys.exit(EXIT_TRANSIENT if e.retriable else EXIT_PERMANENT)


if __name__ == "__main__":
    main()
