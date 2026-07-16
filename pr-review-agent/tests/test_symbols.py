"""Tests for symbol extraction and snippet helpers in review.py."""
import pytest

from review import extract_changed_symbols, _merge_ranges, _extract_snippets


# ── extract_changed_symbols ───────────────────────────────────────────────────

def _diff_for(filename: str, *lines: str) -> str:
    body = "\n".join(lines)
    return (
        f"diff --git a/{filename} b/{filename}\n"
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        "@@ -1,1 +1,1 @@\n"
        f"{body}\n"
    )


# Python
def test_python_def_on_minus_line():
    diff = _diff_for("mod.py", "-def my_function():", "+def renamed():")
    assert "my_function" in extract_changed_symbols(diff)


def test_python_class_on_minus_line():
    diff = _diff_for("mod.py", "-class MyClass:", "+class NewClass:")
    assert "MyClass" in extract_changed_symbols(diff)


def test_python_async_def_on_minus_line():
    diff = _diff_for("mod.py", "-async def fetch():", "+async def get():")
    assert "fetch" in extract_changed_symbols(diff)


def test_python_private_function_not_matched():
    diff = _diff_for("mod.py", "-def _internal():", "+def _renamed():")
    assert "_internal" not in extract_changed_symbols(diff)


def test_python_addition_line_not_matched():
    diff = _diff_for("mod.py", "+def added_fn():")
    assert "added_fn" not in extract_changed_symbols(diff)


def test_python_context_line_not_matched():
    diff = _diff_for("mod.py", " def context_fn():")
    assert "context_fn" not in extract_changed_symbols(diff)


# Go
def test_go_func_on_minus_line():
    diff = _diff_for("main.go", "-func OldHandler() {}", "+func NewHandler() {}")
    assert "OldHandler" in extract_changed_symbols(diff)


def test_go_type_on_minus_line():
    diff = _diff_for("types.go", "-type OldType struct {}", "+type NewType struct {}")
    assert "OldType" in extract_changed_symbols(diff)


def test_go_lowercase_func_not_matched():
    diff = _diff_for("main.go", "-func privateFunc() {}")
    assert "privateFunc" not in extract_changed_symbols(diff)


# TypeScript / JavaScript
def test_ts_export_function_on_minus_line():
    diff = _diff_for("api.ts", "-export function fetchData() {}", "+export function getData() {}")
    assert "fetchData" in extract_changed_symbols(diff)


def test_ts_export_class_on_minus_line():
    diff = _diff_for("model.ts", "-export class UserModel {}", "+export class AccountModel {}")
    assert "UserModel" in extract_changed_symbols(diff)


def test_ts_export_const_on_minus_line():
    diff = _diff_for("config.ts", "-export const DEFAULT_TIMEOUT = 30", "+export const TIMEOUT = 60")
    assert "DEFAULT_TIMEOUT" in extract_changed_symbols(diff)


def test_js_file_uses_ts_pattern():
    diff = _diff_for("util.js", "-export function helper() {}")
    assert "helper" in extract_changed_symbols(diff)


def test_tsx_file_uses_ts_pattern():
    diff = _diff_for("Button.tsx", "-export function Button() {}")
    assert "Button" in extract_changed_symbols(diff)


# Rust
def test_rust_pub_fn_on_minus_line():
    diff = _diff_for("lib.rs", "-pub fn process() {}", "+pub fn handle() {}")
    assert "process" in extract_changed_symbols(diff)


def test_rust_pub_struct_on_minus_line():
    diff = _diff_for("types.rs", "-pub struct Config {}", "+pub struct Settings {}")
    assert "Config" in extract_changed_symbols(diff)


def test_rust_pub_enum_on_minus_line():
    diff = _diff_for("errors.rs", "-pub enum Error {}", "+pub enum AppError {}")
    assert "Error" in extract_changed_symbols(diff)


def test_rust_pub_trait_on_minus_line():
    diff = _diff_for("traits.rs", "-pub trait Handler {}", "+pub trait Processor {}")
    assert "Handler" in extract_changed_symbols(diff)


def test_rust_pub_crate_fn_on_minus_line():
    diff = _diff_for("internal.rs", "-pub(crate) fn helper() {}")
    assert "helper" in extract_changed_symbols(diff)


def test_rust_private_fn_not_matched():
    diff = _diff_for("lib.rs", "-fn private_fn() {}")
    assert "private_fn" not in extract_changed_symbols(diff)


# Language switching / multi-file
def test_language_switches_between_files():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def py_fn(): pass\n"
        "diff --git a/b.go b/b.go\n--- a/b.go\n+++ b/b.go\n"
        "@@ -1,1 +1,1 @@\n"
        "-func GoFn() {}\n"
    )
    symbols = extract_changed_symbols(diff)
    assert "py_fn" in symbols
    assert "GoFn" in symbols


def test_go_pattern_not_applied_to_py_file():
    diff = _diff_for("mod.py", "-func NotGoFunc() {}")
    assert "NotGoFunc" not in extract_changed_symbols(diff)


def test_deduplication_preserves_order():
    diff = _diff_for(
        "mod.py",
        "-def alpha(): pass",
        "-def beta(): pass",
        "-def alpha(): pass",  # duplicate
    )
    symbols = extract_changed_symbols(diff)
    assert symbols == ["alpha", "beta"]


def test_empty_diff_returns_empty():
    assert extract_changed_symbols("") == []


def test_unknown_extension_yields_no_symbols():
    diff = _diff_for("README.md", "-def not_a_function(): pass")
    assert extract_changed_symbols(diff) == []


# ── _merge_ranges ─────────────────────────────────────────────────────────────

def test_merge_ranges_empty():
    assert _merge_ranges([]) == []


def test_merge_ranges_single():
    assert _merge_ranges([(5, 10)]) == [(5, 10)]


def test_merge_ranges_no_overlap():
    result = _merge_ranges([(1, 3), (10, 15)])
    assert result == [(1, 3), (10, 15)]


def test_merge_ranges_adjacent_within_gap():
    # Gap default is 3; (1,5) and (7,10) → distance = 7-5=2 ≤ 3 → merged.
    result = _merge_ranges([(1, 5), (7, 10)])
    assert result == [(1, 10)]


def test_merge_ranges_beyond_gap_not_merged():
    # (1,5) and (10,15) → distance = 10-5=5 > 3 → not merged.
    result = _merge_ranges([(1, 5), (10, 15)])
    assert result == [(1, 5), (10, 15)]


def test_merge_ranges_overlapping():
    result = _merge_ranges([(1, 10), (5, 15)])
    assert result == [(1, 15)]


def test_merge_ranges_unsorted_input():
    result = _merge_ranges([(10, 20), (1, 5)])
    assert result == [(1, 5), (10, 20)]


def test_merge_ranges_multiple_merges():
    result = _merge_ranges([(1, 3), (4, 6), (7, 9)])
    assert result == [(1, 9)]


# ── _extract_snippets ─────────────────────────────────────────────────────────

_LINES = [f"line {i}" for i in range(1, 51)]  # 50 lines, 1-indexed content


def test_extract_snippets_basic():
    ranges, snippet = _extract_snippets(_LINES, [10], context=2, max_lines=30)
    assert (8, 12) in ranges
    assert "line 10" in snippet


def test_extract_snippets_line_numbers_in_output():
    _, snippet = _extract_snippets(_LINES, [5], context=0, max_lines=30)
    assert "   5 | line 5" in snippet


def test_extract_snippets_cap_respected():
    _, snippet = _extract_snippets(_LINES, [25], context=100, max_lines=10)
    lines_returned = [ln for ln in snippet.splitlines() if " | " in ln]
    assert len(lines_returned) <= 10


def test_extract_snippets_truncation_marker():
    _, snippet = _extract_snippets(_LINES, [25], context=100, max_lines=5)
    assert "truncated" in snippet


def test_extract_snippets_no_truncation_when_within_cap():
    _, snippet = _extract_snippets(_LINES, [5], context=2, max_lines=30)
    assert "truncated" not in snippet


def test_extract_snippets_clamps_to_file_bounds():
    # Match at line 1 with context=5 — start should not go below 1.
    ranges, snippet = _extract_snippets(_LINES, [1], context=5, max_lines=30)
    assert ranges[0][0] == 1


def test_extract_snippets_empty_match_lines():
    ranges, snippet = _extract_snippets(_LINES, [], context=5, max_lines=30)
    assert ranges == []
    assert snippet == ""
