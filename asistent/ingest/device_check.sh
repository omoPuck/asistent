#!/bin/sh
# MVP-1 device verification on /usr/bin/python3 (3.10.12).
# Runs the full unittest suite and the demo script; prints raw output.
set -u
cd "$HOME/asistent-spike/mvp1-check" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONDONTWRITEBYTECODE=1

echo "=== 0) environment ==="
/usr/bin/python3 -c 'import sys, sqlite3, locale
print("python        :", sys.version.replace("\n", " "))
print("sqlite        :", sqlite3.sqlite_version)
print("fs encoding   :", sys.getfilesystemencoding())
print("preferred enc :", locale.getpreferredencoding(False))
con = sqlite3.connect(":memory:")
con.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
print("fts5          : available")
'

echo
echo "=== 1) contract + MVP suite: python3 -m unittest discover -s tests -t . ==="
/usr/bin/python3 -m unittest discover -s tests -t . 2>&1 | grep -E '^(Ran |OK|FAILED|FAIL:|ERROR:)'

echo
echo "=== 2) contract suite only: python3 -m unittest discover -s tests/contracts -t . ==="
/usr/bin/python3 -m unittest discover -s tests/contracts -t . 2>&1 | grep -E '^(Ran |OK|FAILED)'

echo
echo "=== 3) MVP-1 suite only: python3 -m unittest discover -s tests/ingest -t . ==="
/usr/bin/python3 -m unittest discover -s tests/ingest -t . 2>&1 | grep -E '^(Ran |OK|FAILED)'

echo
echo "=== 4) demo: python3 -m asistent.ingest.demo_mvp1 ==="
/usr/bin/python3 -m asistent.ingest.demo_mvp1
demo_status=$?
echo "demo exit code: $demo_status"
echo "(自检汇总见上方 demo 输出的最后几行)"

echo
echo "=== 5) py_compile all sources ==="
find asistent tests -name '*.py' -print0 | xargs -0 /usr/bin/python3 -m py_compile && echo "py_compile: OK"
find . -name '__pycache__' -prune -exec rm -rf {} +

exit "$demo_status"
