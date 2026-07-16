"""Tests for _process_diff, _parse_output, and _split_findings in review.py."""
import pytest

from review import _process_diff, _parse_output, _split_findings


# ── _process_diff ─────────────────────────────────────────────────────────────

def test_process_diff_addition_is_numbered_and_valid():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        " existing\n"
        "+added\n"
    )
    annotated, valid = _process_diff(diff)
    assert ("foo.py", 1) in valid  # "existing" is context → line 1
    assert ("foo.py", 2) in valid  # "+added" → line 2


def test_process_diff_deletion_not_in_valid():
    diff = (
        "diff --git a/bar.py b/bar.py\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-removed\n"
        " kept\n"
    )
    annotated, valid = _process_diff(diff)
    assert ("bar.py", 1) in valid   # "kept" is line 1 (no removal shifted nothing)
    # The deleted line does not appear in valid at all.
    assert len([v for v in valid if v[0] == "bar.py"]) == 1


def test_process_diff_deletion_gets_dashes_prefix():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    annotated, _ = _process_diff(diff)
    assert "[---]-old" in annotated


def test_process_diff_hunk_offset_respected():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -5,3 +10,3 @@\n"
        " lineA\n"
        " lineB\n"
        " lineC\n"
    )
    annotated, valid = _process_diff(diff)
    assert ("a.py", 10) in valid
    assert ("a.py", 11) in valid
    assert ("a.py", 12) in valid


def test_process_diff_multiple_hunks_in_one_file():
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -1,2 +1,2 @@\n"
        " line1\n"
        "+line2\n"
        "@@ -10,2 +10,3 @@\n"
        " lineA\n"
        "+lineB\n"
        " lineC\n"
    )
    _, valid = _process_diff(diff)
    assert ("lib.py", 1) in valid
    assert ("lib.py", 2) in valid
    assert ("lib.py", 10) in valid
    assert ("lib.py", 11) in valid
    assert ("lib.py", 12) in valid


def test_process_diff_multiple_files_independent():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+lineA\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,1 +5,1 @@\n"
        "+lineB\n"
    )
    _, valid = _process_diff(diff)
    assert ("a.py", 1) in valid
    assert ("b.py", 5) in valid
    assert ("a.py", 5) not in valid
    assert ("b.py", 1) not in valid


def test_process_diff_no_newline_marker_not_counted():
    diff = (
        "diff --git a/z.py b/z.py\n"
        "--- a/z.py\n"
        "+++ b/z.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+last line\n"
        "\\ No newline at end of file\n"
    )
    _, valid = _process_diff(diff)
    assert ("z.py", 1) in valid
    assert ("z.py", 2) not in valid


def test_process_diff_empty_diff_returns_empty():
    annotated, valid = _process_diff("")
    assert annotated == ""
    assert valid == set()


def test_process_diff_line_number_format_padded():
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+x\n"
    )
    annotated, _ = _process_diff(diff)
    assert "[   1]" in annotated


# ── _parse_output ─────────────────────────────────────────────────────────────

_FINDINGS_BLOCK = (
    '<!-- FINDINGS [\n'
    '  {"file": "src/foo.py", "line": 10, "severity": "critical", "body": "bug here"}\n'
    '] -->'
)


def test_parse_output_clean():
    raw = "Good summary.\n\n" + _FINDINGS_BLOCK
    prose, findings = _parse_output(raw)
    assert prose == "Good summary."
    assert len(findings) == 1
    assert findings[0]["file"] == "src/foo.py"
    assert findings[0]["line"] == 10


def test_parse_output_empty_findings_array():
    raw = "Summary.\n\n<!-- FINDINGS [] -->"
    prose, findings = _parse_output(raw)
    assert prose == "Summary."
    assert findings == []


def test_parse_output_no_sentinel_falls_back_to_prose():
    raw = "Just prose, no findings block."
    prose, findings = _parse_output(raw)
    assert prose == raw.strip()
    assert findings == []


def test_parse_output_unclosed_sentinel_falls_back():
    raw = "Summary.\n<!-- FINDINGS [{bad"
    prose, findings = _parse_output(raw)
    assert prose == raw.strip()
    assert findings == []


def test_parse_output_malformed_json_falls_back():
    raw = "Summary.\n<!-- FINDINGS {not an array} -->"
    prose, findings = _parse_output(raw)
    assert prose == raw.strip()
    assert findings == []


def test_parse_output_findings_not_list_falls_back():
    raw = 'Summary.\n<!-- FINDINGS {"key": "value"} -->'
    prose, findings = _parse_output(raw)
    assert prose == raw.strip()
    assert findings == []


def test_parse_output_prose_trimmed():
    raw = "\n\n  Prose with whitespace.  \n\n" + _FINDINGS_BLOCK
    prose, _ = _parse_output(raw)
    assert prose == "Prose with whitespace."


# ── _split_findings ───────────────────────────────────────────────────────────

_VALID = {("src/foo.py", 10), ("src/foo.py", 15), ("src/bar.py", 3)}


def test_split_findings_exact_match_goes_inline():
    findings = [{"file": "src/foo.py", "line": 10, "body": "issue"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert len(inline) == 1
    assert inline[0]["path"] == "src/foo.py"
    assert inline[0]["line"] == 10
    assert fallback == []


def test_split_findings_snap_to_nearest():
    # Line 12 is not valid; nearest valid in foo.py is 10 or 15 — 12 is closer to 10.
    findings = [{"file": "src/foo.py", "line": 12, "body": "snapped"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert len(inline) == 1
    assert inline[0]["line"] == 10


def test_split_findings_snap_picks_closer_candidate():
    # Line 14 — equidistant between 10 and 15; min() picks 10 (first in sorted order).
    # Line 13 — closer to 15 (diff 2) than 10 (diff 3).
    findings = [{"file": "src/foo.py", "line": 13, "body": "x"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert inline[0]["line"] == 15


def test_split_findings_file_not_in_diff_falls_back():
    findings = [{"file": "src/unknown.py", "line": 5, "body": "msg"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert inline == []
    assert len(fallback) == 1


def test_split_findings_non_int_line_falls_back():
    findings = [{"file": "src/foo.py", "line": "ten", "body": "msg"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert inline == []
    assert len(fallback) == 1


def test_split_findings_missing_line_falls_back():
    findings = [{"file": "src/foo.py", "body": "no line key"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert inline == []
    assert len(fallback) == 1


def test_split_findings_empty_path_falls_back():
    findings = [{"file": "", "line": 10, "body": "msg"}]
    inline, fallback = _split_findings(findings, _VALID)
    assert inline == []
    assert len(fallback) == 1


def test_split_findings_inline_comment_has_right_side():
    findings = [{"file": "src/foo.py", "line": 10, "body": "issue"}]
    inline, _ = _split_findings(findings, _VALID)
    assert inline[0]["side"] == "RIGHT"


def test_split_findings_empty_findings_empty_results():
    inline, fallback = _split_findings([], _VALID)
    assert inline == []
    assert fallback == []


def test_split_findings_mixed_batch():
    findings = [
        {"file": "src/foo.py", "line": 10, "body": "exact"},
        {"file": "src/foo.py", "line": 11, "body": "snapped"},
        {"file": "src/unknown.py", "line": 5, "body": "fallback"},
    ]
    inline, fallback = _split_findings(findings, _VALID)
    assert len(inline) == 2
    assert len(fallback) == 1
