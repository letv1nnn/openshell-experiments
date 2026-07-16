"""Shared fixtures and sys.path setup for all test modules."""
import sys
import os

import pytest

# Make payload/ and payload/lib/ importable without installing the package.
_PAYLOAD = os.path.join(os.path.dirname(__file__), "..", "payload")
_LIB = os.path.join(_PAYLOAD, "lib")
for _p in (_PAYLOAD, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def state_dir(tmp_path):
    """A fresh temporary directory acting as STATE_DIR for each test."""
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Sample diffs used across test_diff.py and test_symbols.py
# ---------------------------------------------------------------------------

SIMPLE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
index 0000001..0000002 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 import os
-def old_fn():
+def new_fn():
     pass
+    return 1
"""

MULTI_HUNK_DIFF = """\
diff --git a/lib/utils.py b/lib/utils.py
index aaa..bbb 100644
--- a/lib/utils.py
+++ b/lib/utils.py
@@ -1,4 +1,3 @@
 line1
-line2
 line3
 line4
@@ -10,3 +9,4 @@
 lineA
 lineB
+lineC
 lineD
"""

MULTI_FILE_DIFF = """\
diff --git a/pkg/a.go b/pkg/a.go
index 000..111 100644
--- a/pkg/a.go
+++ b/pkg/a.go
@@ -1,2 +1,2 @@
-func OldName() {}
+func NewName() {}
diff --git a/pkg/b.go b/pkg/b.go
index 222..333 100644
--- a/pkg/b.go
+++ b/pkg/b.go
@@ -5,2 +5,2 @@
-func Helper() {}
+func Helper2() {}
"""
