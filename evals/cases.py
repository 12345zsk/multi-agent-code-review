"""Live fixtures with human-authored structured expected defects."""

CASES = [
    {
        "name": "sql_injection",
        "file_paths": ["src/api/users.py"],
        "diff": '''\
diff --git a/src/api/users.py b/src/api/users.py
--- a/src/api/users.py
+++ b/src/api/users.py
@@ -0,0 +1,3 @@
+def get_user(user_id):
+    query = "SELECT * FROM users WHERE id = " + user_id
+    return db.execute(query)
''',
        "labels": {"case_id": "sql_injection", "expected": [
            {"id": "sql_injection", "category": "security.sql_injection", "file_path": "src/api/users.py", "line_start": 2, "line_end": 3}
        ]},
    },
    {
        "name": "hardcoded_secret",
        "file_paths": ["src/config.py"],
        "diff": '''\
diff --git a/src/config.py b/src/config.py
--- a/src/config.py
+++ b/src/config.py
@@ -0,0 +1,2 @@
+SECRET_KEY = "hardcoded_secret_abc123"
+DB_PASSWORD = "admin123"
''',
        "labels": {"case_id": "hardcoded_secret", "expected": [
            {"id": "secret_key", "category": "security.hardcoded_credential", "file_path": "src/config.py", "line_start": 1},
            {"id": "database_password", "category": "security.hardcoded_credential", "file_path": "src/config.py", "line_start": 2}
        ]},
    },
    {
        "name": "off_by_one",
        "file_paths": ["src/utils.py"],
        "diff": '''\
diff --git a/src/utils.py b/src/utils.py
--- a/src/utils.py
+++ b/src/utils.py
@@ -0,0 +1,5 @@
+def get_last_items(items, n):
+    result = []
+    for i in range(len(items) - n, len(items) + 1):
+        result.append(items[i])
+    return result
''',
        "labels": {"case_id": "off_by_one", "expected": [
            {"id": "index_bounds", "category": "correctness.index_bounds", "file_path": "src/utils.py", "line_start": 3, "line_end": 4}
        ]},
    },
    {
        "name": "clean_code_approved",
        "file_paths": ["src/math_utils.py"],
        "diff": '''\
diff --git a/src/math_utils.py b/src/math_utils.py
--- a/src/math_utils.py
+++ b/src/math_utils.py
@@ -0,0 +1,3 @@
+def clamp(value: float, min_val: float, max_val: float) -> float:
+    """Return value constrained to [min_val, max_val]."""
+    return max(min_val, min(value, max_val))
''',
        "labels": {"case_id": "clean_code_approved", "expected": []},
    },
]
