# Plan V4 — Codebase-Aware Reviews

## Goal

Make the agent aware of code outside the diff. Currently it can only see what changed — it cannot tell if a renamed function breaks a caller in another file, a changed interface has other implementors, or a deleted export is still imported elsewhere. V4 feeds relevant cross-file context into the review prompt using the local base-branch clone that is already kept fresh by the polling loop.

## Why the local clone is the right source

The agent already clones each watched repo at `--depth=1` and fetches it every poll cycle (every ~120s). This gives a full file tree at the current base branch tip — enough to grep for usages, importers, and implementors without fetching PR branches or doing any extra network work.

The diff shows what changed. The local clone shows what those changes affect beyond the diff. Together they give the model a complete picture.

## Approach

### Step 1 — Extract changed symbols from the diff

Parse the annotated diff already produced by `_annotate_diff()` to pull out symbols that **disappeared** — i.e. functions, methods, or classes that were removed or renamed. Only scan `-` diff lines (lines removed from the old file). Symbols that appear only on `+` lines are new additions and have no callers yet, so grepping for them is pointless.

Extract:
- `def <name>` and `async def <name>` — functions and methods
- `class <name>` — class definitions

Exclude private symbols (names starting with `_`). Private names are not importable across module boundaries, so cross-file callers do not exist by convention.

**Do not extract `__all__` in V4.** Detecting changes to `__all__` requires parsing the list of names it contains, not just capturing the keyword — this is a qualitatively different extraction problem. Defer to a later iteration.

A regex pass is sufficient:

```python
SYMBOL_RE = re.compile(r'^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z][A-Za-z0-9_]*)')
```

Only match non-private names (`[A-Za-z]` start excludes `_`).

### Step 2 — Determine the repo tree-ish to search

Read the current `HEAD` sha of the local clone at the start of the review subprocess (before any grep):

```python
result = subprocess.run(
    ["git", "-C", repo_dir, "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
)
tree_ish = result.stdout.strip()
```

Pass `tree_ish` to all subsequent `git grep` calls instead of searching the working tree. This avoids a race condition: the entrypoint's next poll cycle can `git fetch` + `git checkout FETCH_HEAD` concurrently while the review subprocess is running. Pinning to a sha ensures the grep sees a consistent snapshot.

### Step 3 — Grep the repo for usages

For each extracted symbol, grep the local clone using word-boundary matching (`-w`) to avoid matching longer identifiers that merely contain the symbol as a substring:

```python
result = subprocess.run(
    ["git", "-C", repo_dir, "grep", "-wn", tree_ish, "--", symbol],
    capture_output=True, text=True,
    timeout=10,
)
if result.returncode not in (0, 1):  # 1 = no matches, not an error
    log.warning("git grep failed for symbol %r — skipping.", symbol)
    return []
```

A 10-second timeout is enforced. If it expires, log a warning and return empty results — the review still proceeds without cross-file context rather than failing.

Filter out any matches in files that are already part of the diff — those are covered by the diff itself.

### Step 4 — Language detection and per-file dispatch

Detect languages from file extensions present in the diff. Process each language separately so the extractor and search patterns are appropriate.

| Language | Extensions | Symbol patterns extracted |
|---|---|---|
| Python | `.py`, `.pyi` | `def`, `async def`, `class` |
| Go | `.go` | `func` (top-level only, not methods), `type` |
| TypeScript/JS | `.ts`, `.tsx`, `.js`, `.jsx` | `export function`, `export class`, `export const` |
| Others | any | Skip cross-file analysis, log a note |

For mixed-language PRs (e.g. a PR touching both `.py` and `.go` files), run the extractor for each language against its own file subset and aggregate the symbol list. Each symbol is grepped across the entire repo regardless of language — a Python function could theoretically be called from a shell script — so do not filter grep results by extension.

### Step 5 — Cap and prioritise context

Raw grep results could be enormous. Apply limits in this order:

1. **Per-symbol file cap:** take at most `max_related_files` unique files across all symbols combined (not per symbol). Deprioritise test files — sort production files first.
2. **Context window per match:** for each match line, extract ±10 lines of context from the file on disk. Clamp to file boundaries.
3. **Deduplication:** multiple matches in the same file may produce overlapping ranges. Merge overlapping or adjacent windows (gap ≤ 3 lines) into a single range before counting lines.
4. **Per-file line cap:** after merging, cap each file's total contribution at `max_related_lines_per_file` lines. Truncate from the bottom with a note.
5. **Global line cap:** if total lines across all files would exceed `max_related_lines` (configurable, default 300), drop files from the bottom of the list until it fits, with a note indicating how many were dropped. 300 lines keeps the supplementary context to approximately 2,500–3,000 tokens — a small fraction of the model's effective context window.

### Step 6 — Inject into `build_context()`

Add a new section to the context file, produced by a new `build_related_context()` function:

```
## Related files (usages of changed symbols)

The following files reference symbols that were removed or renamed in this diff
but are not themselves part of the diff. They may contain broken callers. Review
them to assess cross-file impact.

N file(s) shown. M file(s) dropped (global line cap reached).

### path/to/caller.py (lines 45–75)
[snippet]

### path/to/other.py (lines 12–40)
[snippet]
```

If `build_related_context()` returns an empty string (no symbols found, grep returned nothing, or feature disabled), `build_context()` omits the section entirely — no heading, no placeholder.

### Step 7 — Update the system prompt

Add a new section to `review-system.md` with concrete instructions:

```markdown
## Cross-file context

If a **Related files** section is present in the context, it lists files that
call or reference symbols removed or renamed in this diff.

- Check each listed file for broken callers: wrong argument count, missing
  attributes, or references to a name that no longer exists.
- Report any broken caller as a **Critical** or **Warning** finding in the
  **prose section only** (not in the FINDINGS block). These lines are not in
  the diff and cannot be posted as inline comments.
- Use this format in prose:
  `**Critical (cross-file):** \`path/to/caller.py:42\` — calls \`old_name()\` which was removed.`
- Do not report a cross-file finding unless you are confident the caller is
  actually broken. A reference that merely imports the symbol and re-exports
  it under the same name is not a breakage.
- If no cross-file issues are found, do not mention the Related files section.
```

### Step 8 — New helper in `repos.py`

Add `repo_dir()` as a proper exported function so callers do not reconstruct the path themselves:

```python
def repo_dir(org: str, repo: str, repos_base: str) -> str:
    return os.path.join(repos_base, org, repo)
```

Update `review.py` to call `repo_dir()` rather than inlining `os.path.join(repos_base, org, repo)`.

## Config additions

`codebase_aware` is supported both globally under `review_settings` and per-repo. The per-repo value takes precedence when present.

```yaml
review_settings:
  codebase_aware: true          # global default; can be overridden per-repo
  max_related_files: 10         # cap on unique files pulled in
  max_related_lines_per_file: 30  # cap per file after range merging
  max_related_lines: 300        # hard global line cap (~2500 tokens); raise with caution

repos:
  - org: my-org
    repo: large-monorepo
    codebase_aware: false       # disable for this repo specifically
```

## Files changed

| File | Change |
|---|---|
| `payload/review.py` | `extract_changed_symbols()`, `find_related_files()`, `build_related_context()`; update `build_context()` and `run_review()` to read repo HEAD sha and pass it through |
| `payload/lib/repos.py` | Add `repo_dir(org, repo, repos_base) -> str` helper |
| `payload/prompts/review-system.md` | Add **Cross-file context** section with concrete format and reporting instructions |
| `config.yaml` | `codebase_aware`, `max_related_files`, `max_related_lines_per_file`, `max_related_lines` |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Noisy matches from common symbol names | `git grep -w` (word-boundary); filter `_`-prefixed private symbols |
| Slow grep on large repos | 10s subprocess timeout; graceful skip on timeout |
| Context explosion | `max_related_lines` cap (configurable, default 300); file count cap; truncation note to model |
| Fetch/grep race condition | Pin to `HEAD` sha at subprocess start; pass tree-ish to `git grep` |
| Mixed-language PRs | Per-language extractors run independently and aggregate |
| `__all__` changes missed | Deferred: requires parsing list contents, not just the keyword |
| Wrong language detection | Graceful fallback: skip cross-file analysis, log a warning |
| Model puts cross-file findings in FINDINGS block | System prompt explicitly directs cross-file issues to prose only |
