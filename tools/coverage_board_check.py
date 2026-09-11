"""Fail-loud consistency check: SRC coverage vs BOARD ticket status.

A source whose every actionable requirement is VERIFIED while its work
ticket still sits in TODO/DOING is a silent contradiction (the exact T-18
stale state). This tool exits non-zero on it so no gate can pass silently.
"""
import json
import re
import sys
from pathlib import Path

TICKET_RE = re.compile(r"^-\s\[(?P<checkbox>[ x/])\]\s+(?P<id>T-\d+)\b.*?\|\s*source_receipts:\s*(?P<receipts>[^|]+)", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s+(DOING|TODO|DONE|BLOCKED)\s*$", re.MULTILINE)


def board_sections(board_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(board_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(board_text)
        out[m.group(1)] = board_text[m.end():end]
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    coverage_dir = root / ".saipen" / "intake" / "coverage"
    board = (root / ".saipen" / "BOARD.md").read_text(encoding="utf-8")
    sections = board_sections(board)

    problems: list[str] = []
    for cov_file in sorted(coverage_dir.glob("SRC-*.json")):
        source_id = cov_file.stem
        data = json.loads(cov_file.read_text(encoding="utf-8"))
        reqs = data.get("requirements", {})
        actionable = [r for r in reqs.values() if r.get("actionable")]
        if not actionable:
            continue
        all_verified = all(r.get("disposition") == "VERIFIED" for r in actionable)
        if not all_verified:
            continue  # source still open: its tickets must stay open, no contradiction
        tickets = {m.group("id"): m for m in TICKET_RE.finditer(board)
                   if source_id in m.group("receipts")}
        for tid, m in tickets.items():
            section = next((s for s, body in sections.items() if m.group(0) in body), "?")
            if section != "DONE":
                problems.append(
                    f"{source_id}: all requirements VERIFIED but ticket {tid} sits in ## {section} "
                    f"(coverage={cov_file.name})"
                )
    if problems:
        print("CONSISTENCY FAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("CONSISTENCY PASS: no coverage-vs-board contradiction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
