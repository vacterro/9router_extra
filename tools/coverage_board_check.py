"""Fail-loud consistency check: SRC coverage, closure state, and BOARD tickets.

Two contradiction classes, both silent in a normal gate:

1. A source whose every actionable requirement is VERIFIED while its work
   ticket still sits in TODO/DOING (the exact T-18 stale state).
2. A closed receipt whose closure was not actually terminal: a CLOSED
   tombstone that still has an ACTIVE index entry, still carries hot
   body/contract/coverage files, or records unresolved clauses. SOURCES.md
   closure requires every actionable clause terminal, and closure removes the
   hot surface for a compact tombstone (SOURCES.md "Closure and retention").

This tool exits non-zero so no gate can pass silently. It is deliberately not
a general protocol validator: only the coverage/board/closure contradictions.
"""
import json
import re
import sys
from pathlib import Path

TICKET_RE = re.compile(r"^-\s\[(?P<checkbox>[ x/])\]\s+(?P<id>T-\d+)\b.*?\|\s*source_receipts:\s*(?P<receipts>[^|]+)", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s+(DOING|TODO|DONE|BLOCKED)\s*$", re.MULTILINE)
RECEIPT_RE = re.compile(r"^SRC-\d+$")


def board_sections(board_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(board_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(board_text)
        out[m.group(1)] = board_text[m.end():end]
    return out


def closure_contradictions(intake: Path) -> list[str]:
    """Closed-receipt contradictions against the hot intake surface.

    A `CLOSED` tombstone is a compact replacement: the receipt must be gone
    from `index.active`, its body/contract/coverage must have left the hot
    `intake/` directories for `archive/source/`, and it must record zero
    unresolved clauses (SOURCES.md closure bar).
    """
    problems: list[str] = []
    index_path = intake / "index.json"
    if not index_path.is_file():
        return problems
    index = json.loads(index_path.read_text(encoding="utf-8"))
    active = index.get("active", {}) or {}
    tombstones = index.get("tombstones", {}) or {}
    for receipt_id, tomb in sorted(tombstones.items()):
        if tomb.get("status") != "CLOSED":
            continue
        if receipt_id in active:
            problems.append(
                f"{receipt_id}: CLOSED tombstone while still present in index.active "
                "(a closed receipt cannot remain ACTIVE)"
            )
        for hot in (
            intake / "active" / f"{receipt_id}.md",
            intake / "active" / f"{receipt_id}.meta.json",
            intake / "contracts" / f"{receipt_id}.json",
            intake / "coverage" / f"{receipt_id}.json",
        ):
            if hot.is_file():
                problems.append(
                    f"{receipt_id}: CLOSED tombstone but hot file still present "
                    f"({hot.name}) -- closure must move the receipt off the hot surface"
                )
        if int(tomb.get("unresolved", 0) or 0) != 0:
            problems.append(
                f"{receipt_id}: CLOSED tombstone records "
                f"{tomb.get('unresolved')} unresolved clause(s) -- closure requires every "
                "actionable clause terminal (SOURCES.md)"
            )
    return problems


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

    problems.extend(closure_contradictions(root / ".saipen" / "intake"))

    if problems:
        print("CONSISTENCY FAIL:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("CONSISTENCY PASS: no coverage-vs-board contradiction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
