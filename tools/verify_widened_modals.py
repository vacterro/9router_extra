"""T-71 check: the installed 9Router build really carries the widened modals.

The patch widens three Modal call sites from the default size="md" (max-w-md,
448px) to a viewport-filling modal so provider model names stop being truncated:
the shared ComboFormModal, the page-local copy on /dashboard/combos, and the
ModelSelectModal picker.

T-82 later moved the two combo modals from size="full" (max-w-4xl) to size="max",
so the accepted literal is per call site rather than global. Both are wider than
the default; what this gate must prove is that the widening survived the pack,
not which of the two widths was chosen.

Run: python tools/verify_widened_modals.py
Exit 0 = the installed build and the running server both serve the patch.
"""

import glob
import os
import sys
import urllib.request

PORT = 20128
# Chunk ids change on every build, so the check is by shape, not by filename.
# Each entry lists every literal that counts as "widened" for that call site.
BUILD_GLOBS = {
    "shared combo modal": ("static/chunks/5497-*.js", (b'size:"max"', b'size:"full"')),
    "combos page modal": (
        "static/chunks/app/(dashboard)/dashboard/combos/page-*.js",
        (b'size:"max"', b'size:"full"'),
    ),
    "model picker (ssr)": ("server/chunks/412.js", (b'size:"full"', b'size:"max"')),
}


def installed_build():
    for root in (r"C:\nodejs\node_modules\9router\app\.next-cli-build",):
        if os.path.isdir(root):
            return root
    raise SystemExit("FAIL: no installed 9Router build at C:\\nodejs\\node_modules\\9router")


def main():
    build = installed_build()
    failures = []

    found = []
    for label, (pattern, literals) in BUILD_GLOBS.items():
        hits = glob.glob(os.path.join(build, *pattern.split("/")))
        if not hits:
            failures.append(f"{label}: no chunk matched {pattern}")
            continue
        bodies = [(p, open(p, "rb").read()) for p in hits]
        matched = [(p, body) for p, body in bodies if any(lit in body for lit in literals)]
        if not matched:
            wanted = " or ".join(repr(lit) for lit in literals)
            failures.append(f"{label}: {os.path.basename(hits[0])} lacks {wanted}")
            continue
        path, body = matched[0]
        found.append((label, path, body, literals))

    # The server must serve the patched client chunks, not just hold them on disk.
    for label, path, body, literals in found:
        rel = os.path.relpath(path, build).replace("\\", "/")
        if not rel.startswith("static/"):
            continue
        url = f"http://127.0.0.1:{PORT}/_next/{rel}"
        try:
            served = urllib.request.urlopen(url, timeout=10).read()
        except Exception as exc:  # server down, chunk not routed
            failures.append(f"{label}: GET {url} failed: {exc}")
            continue
        if not any(lit in served for lit in literals):
            failures.append(f"{label}: served {url} without the patch")

    if failures:
        print("FAIL")
        for line in failures:
            print(" -", line)
        return 1
    print("PASS: widened modals present in the installed build and served on port", PORT)
    for label, path, _body, _literals in found:
        print(f" - {label}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
