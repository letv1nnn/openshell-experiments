#!/usr/bin/env python3
"""Per-PR review orchestration. Invoked as a subprocess by entrypoint.py."""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PAYLOAD_DIR, "lib"))

from config import load_config
from github import get_pr_diff, get_prior_reviews, post_review
from repos import repo_dir as _repo_dir

log = logging.getLogger(os.environ.get("REVIEW_LOG_PREFIX", "review"))

# ── Cross-file analysis ───────────────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",
    ".rs": "rust",
}

# Only match public symbols (non-underscore start) on removed (-) diff lines.
_LANG_SYMBOL_RE: dict[str, re.Pattern] = {
    "python":     re.compile(r"^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z][A-Za-z0-9_]*)"),
    "go":         re.compile(r"^-\s*(?:func|type)\s+([A-Z][A-Za-z0-9_]*)"),
    "typescript": re.compile(r"^-\s*export\s+(?:function|class|const)\s+([A-Za-z][A-Za-z0-9_]*)"),
    "rust":       re.compile(r"^-\s*pub(?:\([^)]*\))?\s+(?:fn|struct|enum|trait|type)\s+([A-Za-z][A-Za-z0-9_]*)"),
}


def extract_changed_symbols(diff: str) -> list[str]:
    """Return public symbols removed/renamed in the diff (- lines only, deduplicated)."""
    symbols: list[str] = []
    current_lang: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            ext = os.path.splitext(line[6:])[1].lower()
            current_lang = _EXT_TO_LANG.get(ext)
        elif current_lang:
            pattern = _LANG_SYMBOL_RE.get(current_lang)
            if pattern:
                m = pattern.match(line)
                if m:
                    symbols.append(m.group(1))
    return list(dict.fromkeys(symbols))  # deduplicate, preserve insertion order


def _read_file_from_tree(repo_dir_path: str, tree_ish: str, path: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo_dir_path, "show", f"{tree_ish}:{path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _merge_ranges(ranges: list[tuple[int, int]], gap: int = 3) -> list[tuple[int, int]]:
    if not ranges:
        return []
    sorted_ranges = sorted(ranges)
    out = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        if start <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _extract_snippets(
    file_lines: list[str],
    match_lines: list[int],
    context: int = 10,
    max_lines: int = 30,
) -> tuple[list[tuple[int, int]], str]:
    """Return (used_ranges, snippet_text) capped at max_lines. Lines are 1-indexed."""
    n = len(file_lines)
    raw_ranges = [(max(1, ln - context), min(n, ln + context)) for ln in match_lines]
    merged = _merge_ranges(raw_ranges)

    parts: list[str] = []
    used_ranges: list[tuple[int, int]] = []
    total = 0
    truncated = False

    for start, end in merged:
        if total >= max_lines:
            truncated = True
            break
        if end - start + 1 > max_lines - total:
            end = start + (max_lines - total) - 1
            truncated = True
        chunk = "\n".join(f"{i:>4} | {file_lines[i - 1]}" for i in range(start, end + 1))
        parts.append(chunk)
        used_ranges.append((start, end))
        total += end - start + 1

    if truncated:
        parts.append("... (truncated, line cap reached)")

    return used_ranges, "\n".join(parts)


def find_related_files(
    symbols: list[str],
    repo_dir_path: str,
    tree_ish: str,
    diff_files: set[str],
    max_files: int,
    grep_timeout: int = 10,
) -> dict[str, list[int]]:
    """Return {filepath: sorted_line_numbers} for files referencing changed symbols."""
    file_hits: dict[str, list[int]] = {}

    for symbol in symbols:
        try:
            result = subprocess.run(
                ["git", "-C", repo_dir_path, "grep", "-wn", tree_ish, "--", symbol],
                capture_output=True, text=True,
                timeout=grep_timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning("git grep timed out for symbol %r — skipping.", symbol)
            continue

        if result.returncode not in (0, 1):  # 1 = no matches, not an error
            log.warning("git grep failed for symbol %r — skipping.", symbol)
            continue

        for line in result.stdout.splitlines():
            # Format with tree-ish: <hash>:path:lineno:content
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            path, lineno_str = parts[1], parts[2]
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue
            if path in diff_files:
                continue
            file_hits.setdefault(path, []).append(lineno)

    def _is_test(p: str) -> bool:
        return any(seg in p.lower() for seg in ("test", "tests", "spec", "_test."))

    ordered = sorted(file_hits, key=lambda p: (_is_test(p), p))
    return {f: sorted(set(file_hits[f])) for f in ordered[:max_files]}


def build_related_context(
    diff: str,
    repo_dir_path: str,
    tree_ish: str,
    review_cfg: dict,
) -> str:
    """Build the Related files context section; returns '' if disabled or nothing found."""
    if not review_cfg.get("codebase_aware", True):
        return ""

    max_files = review_cfg.get("max_related_files", 10)
    max_lines_per_file = review_cfg.get("max_related_lines_per_file", 30)
    max_lines_total = review_cfg.get("max_related_lines", 300)

    diff_files = {line[6:] for line in diff.splitlines() if line.startswith("+++ b/")}

    symbols = extract_changed_symbols(diff)
    if not symbols:
        log.debug("Cross-file: no changed public symbols found.")
        return ""

    log.info("Cross-file analysis: %d symbol(s): %s", len(symbols), symbols)

    file_hits = find_related_files(
        symbols, repo_dir_path, tree_ish, diff_files, max_files,
    )
    if not file_hits:
        log.debug("Cross-file: no usages found outside the diff.")
        return ""

    sections: list[str] = []
    total_lines = 0

    for path, match_lines in file_hits.items():
        remaining = max_lines_total - total_lines
        if remaining <= 0:
            break
        file_lines = _read_file_from_tree(repo_dir_path, tree_ish, path)
        if not file_lines:
            continue
        ranges, snippet = _extract_snippets(
            file_lines, match_lines,
            context=10,
            max_lines=min(max_lines_per_file, remaining),
        )
        if not ranges:
            continue
        range_str = ", ".join(f"{s}–{e}" for s, e in ranges)
        sections.append(f"\n### {path} (lines {range_str})\n```\n{snippet}\n```")
        total_lines += sum(e - s + 1 for s, e in ranges)

    if not sections:
        return ""

    shown = len(sections)
    dropped = len(file_hits) - shown
    summary = f"{shown} file(s) shown."
    if dropped > 0:
        summary += f" {dropped} file(s) dropped (global line cap reached)."

    header = (
        "## Related files (usages of changed symbols)\n\n"
        "The following files reference symbols that were removed or renamed in this diff "
        "but are not themselves part of the diff. They may contain broken callers. Review "
        f"them to assess cross-file impact.\n\n{summary}"
    )
    return header + "".join(sections)


# ── Review orchestration ──────────────────────────────────────────────────────


def render_instructions(template_path: str, org: str, repo: str,
                         pr_number: int, pr_title: str) -> str:
    with open(template_path) as f:
        t = f.read()
    return (
        t.replace("{{ORG}}", org)
         .replace("{{REPO}}", repo)
         .replace("{{PR_NUMBER}}", str(pr_number))
         .replace("{{PR_TITLE}}", pr_title)
    )


def build_context(meta: dict, repo_dir: str, prior_reviews: list[dict],
                  max_prior_reviews: int = 3, related_context: str = "") -> str:
    parts = ["## PR Description\n\n", meta.get("body") or "(no description)", "\n"]

    contributing = os.path.join(repo_dir, "CONTRIBUTING.md")
    if os.path.exists(contributing):
        parts.append("\n## CONTRIBUTING.md\n\n")
        with open(contributing) as f:
            parts.append("".join(f.readlines()[:100]))

    capped = prior_reviews[-max_prior_reviews:] if max_prior_reviews > 0 else []
    if capped:
        parts.append(
            f"\n## Prior Reviews\n\n"
            f"The following reviews were posted on earlier commits of this PR "
            f"(most recent {len(capped)}, oldest first). Cross-reference with the diff to "
            f"determine what has been addressed.\n"
        )
        for r in capped:
            author = r.get("user", {}).get("login", "unknown")
            submitted = r.get("submitted_at", "")
            commit = r.get("commit_id", "")[:8]
            parts.append(f"\n### Review by @{author} — {submitted} (commit {commit})\n\n")
            parts.append(r.get("body", "").strip())
            parts.append("\n")

    if related_context:
        parts.append("\n" + related_context + "\n")

    return "".join(parts)


_FINDINGS_SENTINEL = "<!-- FINDINGS"
_FINDINGS_END = "-->"


def _process_diff(diff: str) -> tuple[str, set[tuple[str, int]]]:
    """Return (annotated_diff, valid_right_lines) in a single pass.

    annotated_diff prefixes each hunk line with its new-file line number so the
    model can reference lines precisely.  valid_right_lines is the set of
    (path, new_file_line) pairs eligible for RIGHT-side inline comments.
    """
    result: list[str] = []
    valid: set[tuple[str, int]] = set()
    current_file: str | None = None
    new_line = 0

    for raw in diff.splitlines(keepends=True):
        s = raw.rstrip("\n")
        if s.startswith("+++ b/"):
            current_file = s[6:]
            new_line = 0
            result.append(raw)
        elif s.startswith(("diff ", "index ", "--- ")):
            result.append(raw)
        elif s.startswith("@@ "):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", s)
            if m:
                new_line = int(m.group(1)) - 1
            result.append(raw)
        elif s.startswith("\\"):
            result.append(raw)
        elif s.startswith("-"):
            result.append(f"[---]{raw}")
        else:  # "+" addition or " " context
            new_line += 1
            result.append(f"[{new_line:>4}]{raw}")
            if current_file is not None:
                valid.add((current_file, new_line))

    return "".join(result), valid


def _parse_output(raw: str) -> tuple[str, list[dict]]:
    """Split model output into (prose_body, findings_list)."""
    if _FINDINGS_SENTINEL not in raw:
        log.warning("No FINDINGS block in model output — posting as prose.")
        return raw.strip(), []
    prose, remainder = raw.split(_FINDINGS_SENTINEL, 1)
    end = remainder.find(_FINDINGS_END)
    if end == -1:
        log.warning("FINDINGS block not closed — posting as prose.")
        return raw.strip(), []
    try:
        findings = json.loads(remainder[:end].strip())
        if not isinstance(findings, list):
            raise ValueError("FINDINGS must be a JSON array")
        return prose.strip(), findings
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse FINDINGS JSON: %s — posting as prose.", e)
        return raw.strip(), []


def _split_findings(
    findings: list[dict], valid: set[tuple[str, int]]
) -> tuple[list[dict], list[dict]]:
    """Partition findings into (inline_comments, fallback_prose_findings).

    When a finding's exact line isn't in the diff, snap to the nearest valid
    line in the same file rather than falling back to prose. Only falls back
    to prose when the file itself has no valid lines in this diff.
    """
    valid_by_file: dict[str, set[int]] = {}
    for path, ln in valid:
        valid_by_file.setdefault(path, set()).add(ln)

    inline, fallback = [], []
    for f in findings:
        path, line = f.get("file", ""), f.get("line")
        if not isinstance(line, int) or not path:
            fallback.append(f)
            continue
        if (path, line) in valid:
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": f.get("body", "")})
        elif path in valid_by_file:
            snap = min(valid_by_file[path], key=lambda ln: abs(ln - line))
            log.warning("Finding at %s:%d not in diff — snapping to line %d.", path, line, snap)
            inline.append({"path": path, "line": snap, "side": "RIGHT", "body": f.get("body", "")})
        else:
            fallback.append(f)
    return inline, fallback


def _killpg(pid: int) -> None:
    """Send SIGTERM to an entire process group; ignore 'no such process'."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def run_review(org: str, repo: str, pr_number: int, head_sha: str,
               payload_dir: str, config: dict) -> bool:
    review_cfg = config.get("review_settings", {})
    model = review_cfg.get("model", "anthropic/claude-sonnet-4-6")
    max_diff_lines = review_cfg.get("max_diff_lines", 8000)
    max_files = review_cfg.get("max_files_changed", 50)
    timeout = review_cfg.get("review_timeout_seconds", 600)
    comment_prefix = review_cfg.get("comment_prefix", "**AI PR Review**")
    max_prior_reviews = review_cfg.get("max_prior_reviews", 3)
    repos_base = os.environ.get("REPOS_BASE", "/sandbox/pr-review-agent/repos")

    log.info("Starting review: %s/%s#%s (%s)", org, repo, pr_number, head_sha[:8])

    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", f"{org}/{repo}",
         "--json", "title,body,additions,deletions,changedFiles"],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(result.stdout)
    total_lines = meta["additions"] + meta["deletions"]
    log.info("PR: \"%s\" | +%d/-%d lines | %d files",
             meta["title"], meta["additions"], meta["deletions"], meta["changedFiles"])

    if total_lines > max_diff_lines:
        log.warning("SKIP: diff too large (%d lines > %d)", total_lines, max_diff_lines)
        post_review(org, repo, pr_number,
                    f"{comment_prefix}\n\nThis PR is too large to review automatically "
                    f"({total_lines} changed lines, limit {max_diff_lines}). "
                    "Please break it into smaller PRs.")
        return True

    if meta["changedFiles"] > max_files:
        log.warning("SKIP: too many files (%d > %d)", meta["changedFiles"], max_files)
        post_review(org, repo, pr_number,
                    f"{comment_prefix}\n\nThis PR touches too many files to review "
                    f"automatically ({meta['changedFiles']} files, limit {max_files}).")
        return True

    log.info("Fetching diff...")
    diff = get_pr_diff(org, repo, pr_number)
    if not diff.strip():
        log.info("SKIP: empty diff.")
        return True
    log.info("Diff: %d lines across %d file(s).",
             len(diff.splitlines()),
             sum(1 for ln in diff.splitlines() if ln.startswith("+++ b/")))

    prior_reviews = get_prior_reviews(org, repo, pr_number)
    log.info("Found %d prior review(s).", len(prior_reviews))

    repo_dir_path = _repo_dir(org, repo, repos_base)
    template = os.path.join(payload_dir, "prompts", "review-system.md")

    # Per-repo codebase_aware override takes precedence over the global setting.
    repo_cfg = next(
        (r for r in config.get("repos", []) if r.get("org") == org and r.get("repo") == repo),
        {}
    )
    effective_review_cfg = {**review_cfg}
    if "codebase_aware" in repo_cfg:
        effective_review_cfg["codebase_aware"] = repo_cfg["codebase_aware"]

    # Fetch the PR branch head so cross-file analysis greps the post-PR tree,
    # not the default branch.  A shallow fetch by PR ref is fast and avoids
    # grepping a stale tree that may pre-date the PR or miss branch-only files.
    tree_ish = ""
    try:
        subprocess.run(
            ["git", "-C", repo_dir_path, "fetch", "--depth=1", "origin",
             f"refs/pull/{pr_number}/head"],
            capture_output=True, text=True, check=True,
        )
        tree_ish = "FETCH_HEAD"
    except Exception as e:
        log.warning("Could not fetch PR head for cross-file analysis: %s — skipping.", e)

    related_context = ""
    if tree_ish:
        try:
            related_context = build_related_context(
                diff, repo_dir_path, tree_ish, effective_review_cfg,
            )
        except Exception as e:
            log.warning("Cross-file analysis failed: %s — continuing without it.", e)

    instructions = render_instructions(template, org, repo, pr_number, meta["title"])
    context_text = build_context(meta, repo_dir_path, prior_reviews, max_prior_reviews,
                                 related_context=related_context)
    annotated_diff, valid_lines = _process_diff(diff)
    full_prompt = (
        f"{instructions}\n\n"
        f"---\n\n"
        f"{context_text}\n\n"
        f"---\n\n"
        f"## Diff\n\n"
        f"{annotated_diff}"
    )

    log.info("Running OpenCode (timeout %ds)...", timeout)

    with tempfile.TemporaryDirectory() as tmp:
        opencode_out = os.path.join(tmp, "opencode.out")
        opencode_err = os.path.join(tmp, "opencode.err")

        # Write stdout/stderr to files rather than pipes so that proc.wait()
        # returns when OpenCode itself exits — not when every child process it
        # may have spawned (language servers, file watchers, etc.) also closes
        # its copy of the inherited pipe fd.  start_new_session=True puts
        # OpenCode in its own process group (severs /dev/tty access too) so we
        # can reap lingering children with killpg after wait() returns.
        try:
            with open(opencode_out, "w") as out_fh, open(opencode_err, "w") as err_fh:
                proc = subprocess.Popen(
                    ["opencode", "run", "--model", model],
                    env={**os.environ,
                         "ANTHROPIC_BASE_URL": "https://inference.local/v1",
                         "ANTHROPIC_API_KEY": "unused"},
                    stdin=subprocess.PIPE,
                    stdout=out_fh,
                    stderr=err_fh,
                    start_new_session=True,
                )
        except Exception as e:
            log.error("OpenCode launch failed: %s", e)
            return False

        # Write the full prompt to stdin in a daemon thread — the pipe buffer
        # (~64 KB) is smaller than large diffs, so writing in the main thread
        # before OpenCode drains it would deadlock.
        def _write_stdin():
            try:
                proc.stdin.write(full_prompt.encode())
            finally:
                proc.stdin.close()

        threading.Thread(target=_write_stdin, daemon=True).start()

        stop_heartbeat = threading.Event()
        start_time = time.monotonic()

        def _heartbeat():
            while not stop_heartbeat.wait(30):
                elapsed = int(time.monotonic() - start_time)
                log.debug("OpenCode still running... %ds elapsed.", elapsed)

        threading.Thread(target=_heartbeat, daemon=True).start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid)
            proc.wait()
            log.error("OpenCode timed out after %ds.", timeout)
            return False
        finally:
            _killpg(proc.pid)  # reap any children that outlived OpenCode
            stop_heartbeat.set()

        with open(opencode_err) as f:
            stderr = f.read()
        if stderr:
            log.debug("OpenCode stderr:\n%s", stderr)

        with open(opencode_out) as f:
            stdout = f.read()

        log.info("OpenCode exited %d, output: %d bytes.", proc.returncode, len(stdout))
        if proc.returncode != 0:
            log.error("OpenCode failed (exit %d). Run with LOG_LEVEL=DEBUG for stderr.", proc.returncode)
            return False

        if not stdout.strip():
            log.error("OpenCode produced empty output. Run with LOG_LEVEL=DEBUG for stderr.")
            return False

        prose, findings = _parse_output(stdout)
        inline_comments, fallback = _split_findings(findings, valid_lines)

        if fallback:
            log.warning("%d finding(s) not in diff — appending to prose.", len(fallback))
            prose += "\n\n**Additional findings (line not in diff):**"
            for f in fallback:
                sev = f.get("severity", "note").title()
                loc = f"`{f.get('file')}:{f.get('line')}`" if f.get("file") else ""
                prose += f"\n- **{sev}** {loc} — {f.get('body', '')}"

        log.info("Posting review: %d inline comment(s).", len(inline_comments))
        review_body = f"{comment_prefix}\n\n{prose}"
        post_review(org, repo, pr_number, review_body, inline_comments or None)
        log.info("Review posted for %s/%s#%s.", org, repo, pr_number)
        return True


if __name__ == "__main__":
    _log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout, level=_log_level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    if len(sys.argv) != 5:
        log.error("Usage: review.py <org> <repo> <pr_number> <head_sha>")
        sys.exit(1)

    org = sys.argv[1]
    repo = sys.argv[2]
    pr_number = int(sys.argv[3])
    head_sha = sys.argv[4]
    try:
        config = load_config("/app/pr-review-agent/config.yaml")
    except Exception as e:
        log.error("Failed to load config: %s", e)
        sys.exit(1)

    success = run_review(org, repo, pr_number, head_sha, PAYLOAD_DIR, config)
    sys.exit(0 if success else 1)
