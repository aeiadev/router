"""router report [--since N] [--json]: what Router did lately, with advice. Nothing is changed.

N is like 36h or 7d (default 7d, at most 90d). Sections: spawns by role and tier, judge coverage,
first pass by tier, context guard counts, shadow hits, all-time persisted usage and advice.
Privacy: no label, brief, key or finding text is printed. Codex lanes have no verdicts.
"""
import argparse
import calendar
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import common
import ledger

HELP = "report [--since N] [--json]: routing window and all-time opt-in persisted usage"
DEFAULT_SINCE = "7d"
MAX_HOURS = 90 * 24
LOG_TAIL_BYTES = 4 * 1024 * 1024
HOSTS = {"c:": "claude", "x:": "codex"}
NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,60}")
WINDOW = re.compile(r"([1-9][0-9]{0,5})([hd])")


def parse_since(text):
    """Hours in a window like 36h or 7d, or None when the text is not one."""
    match = WINDOW.fullmatch(text or "")
    return int(match.group(1)) * (24 if match.group(2) == "d" else 1) if match else None


def percent(part, whole) -> str:
    return f"{int(100 * part / whole + 0.5)}%" if whole else "-"


def host_of(key) -> str:
    return next((name for prefix, name in HOSTS.items() if isinstance(key, str) and key.startswith(prefix)), "other")


def safe(value, pattern=NAME, default="-") -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else default


def read_log(name, since):
    """Records of state_dir()/<name>.jsonl newer than since: the last 4 MiB, bad and cut lines dropped."""
    path = common.state_path() / f"{name}.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - LOG_TAIL_BYTES)
            handle.seek(start)
            raw = handle.read(LOG_TAIL_BYTES)
    except OSError:
        return []
    lines = raw.split(b"\n")
    if start > 0:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            record = json.loads(line)
            stamp = calendar.timegm(time.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if stamp >= since:
            records.append(record)
    return records


def first_pass(verdicts, unknown) -> dict:
    """Per tier: briefs, first-pass count and rounds to PASS, from time-ordered (key, ts, verdict, round, tier)."""
    first, rounds = {}, {}
    for key, _, verdict, round_, tier in verdicts:
        if key is None:
            continue
        tier = tier or "?"
        first.setdefault(key, (verdict, tier))
        if verdict == "PASS" and isinstance(round_, int):
            rounds.setdefault(tier, []).append(round_)
    tiers = {}
    for verdict, tier in first.values():
        row = tiers.setdefault(tier, {"briefs": 0, "first_pass": 0})
        row["briefs"] += 1
        row["first_pass"] += verdict == "PASS"
    for tier in rounds:
        tiers.setdefault(tier, {"briefs": 0, "first_pass": 0})
    for tier, row in tiers.items():
        row["rate"] = percent(row["first_pass"], row["briefs"])
        row["rounds_to_pass"] = f"{sum(rounds[tier]) / len(rounds[tier]):.1f}" if rounds.get(tier) else "-"
    return {"tiers": tiers, "unknown": unknown}


def collect(since, now) -> dict:
    """The whole report as plain data. Raises sqlite3.Error, OSError or ValueError on a broken ledger."""
    import usage
    rows = ledger.report_rows(since)
    spawns = {}
    for key, role, tier, _ in rows["attempts"]:
        slot = (role or "-", tier or "-", host_of(key))
        spawns[slot] = spawns.get(slot, 0) + 1
    claude = [lane for lane in rows["lanes"] if host_of(lane[0]) == "claude"]
    judged = sum(1 for lane in claude if lane[3] == "judged")
    unjudged = sum(1 for lane in claude if lane[3] == "returned")
    returned = judged + unjudged
    passes = first_pass(rows["verdicts"], rows["unknown"])

    stops = [r for r in read_log("context", since) if r.get("event") == "stop_cap"
             and r.get("action") in ("block", "shadow", "second_pass")]
    by_class = {}
    for record in stops:
        name = safe(record.get("class"))
        by_class[name] = by_class.get(name, 0) + 1
    reads = [r for r in read_log("context", since) if r.get("event") == "large_read"]
    context = {"over_cap": len(stops), "over_cap_by_class": by_class,
               "large_read_blocks": sum(1 for r in reads if r.get("action") == "block"),
               "large_read_shadow": sum(1 for r in reads if r.get("action") == "shadow"),
               "auto_blocks": sum(1 for r in read_log("context", since) if r.get("event") == "auto_block")}
    hits = {}
    for record in read_log("spawns", since):
        if record.get("shadow") is True:
            name = "spawn." + safe(record.get("rule"), default="unknown")
            hits[name] = hits.get(name, 0) + 1
    for record in read_log("context", since):
        if record.get("action") == "shadow":
            name = "context." + safe(record.get("event"), default="unknown")
            hits[name] = hits.get(name, 0) + 1

    advice = []
    if unjudged >= 1:
        advice.append(f"advice: {unjudged} builder return(s) were never judged; spawn judge with the same TASK and FILES.")
    for tier, row in sorted(passes["tiers"].items()):
        if row["briefs"] >= 5 and row["first_pass"] * 2 < row["briefs"]:
            advice.append(f"advice: tier {tier} passed first time in {row['rate']} of {row['briefs']} briefs; "
                          "tighten TASK, FILES and BAR before spawning.")
    if context["over_cap"] >= 3:
        advice.append(f"advice: {context['over_cap']} returns went over the cap; ask for conclusions first and a file path.")
    return {
        "since": since, "now": now,
        "spawns": [{"role": role, "tier": tier, "host": host, "spawns": count}
                   for (role, tier, host), count in sorted(spawns.items())],
        "judge_coverage": {"returned": returned, "judged": judged, "unjudged": unjudged,
                           "judged_rate": percent(judged, returned),
                           "codex_lanes": sum(1 for lane in rows["lanes"] if host_of(lane[0]) == "codex")},
        "first_pass": passes, "context": context, "shadow_hits": hits,
        "usage": usage.get_report(state_dir=Path(common.state_path()) / "usage"),
        "advice": advice[:3] or ["advice: none"],
    }


def table(header, rows) -> list:
    rows = [tuple(str(cell) for cell in row) for row in rows]
    widths = [max(len(name), *(len(row[index]) for row in rows)) for index, name in enumerate(header)] if rows \
        else [len(name) for name in header]
    return ["  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip() for cells in (header, *rows)]


def render(data, window) -> str:
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(data["since"]))
    out = [f"router report: last {window} (since {since} UTC)", "", "spawns by role and tier"]
    out += table(("role", "tier", "host", "spawns"),
                 [(r["role"], r["tier"], r["host"], r["spawns"]) for r in data["spawns"]]) if data["spawns"] else ["none"]
    judge = data["judge_coverage"]
    out += ["", "judge coverage",
            f"returned {judge['returned']}  judged {judge['judged']}  unjudged {judge['unjudged']}"
            f"  judged-rate {judge['judged_rate']}",
            f"codex lanes: {judge['codex_lanes']} (no verdicts on Codex)"]
    passes = data["first_pass"]
    out += ["", "first pass by tier"]
    out += table(("tier", "briefs", "first-pass", "rate", "rounds-to-PASS"),
                 [(tier, row["briefs"], row["first_pass"], row["rate"], row["rounds_to_pass"])
                  for tier, row in sorted(passes["tiers"].items())]) if passes["tiers"] else ["none"]
    out.append(f"unknown verdicts: {passes['unknown']}")
    ctx = data["context"]
    out += ["", "context guard", f"over-cap returns: {ctx['over_cap']}"]
    out += [f"  {name}: {count}" for name, count in sorted(ctx["over_cap_by_class"].items())]
    out += [f"large-read blocks: {ctx['large_read_blocks']} (shadow: {ctx['large_read_shadow']})",
            f"auto blocks: {ctx['auto_blocks']}", "", "shadow hits"]
    out += [f"{name}  {count}" for name, count in sorted(data["shadow_hits"].items())] or ["none"]
    usage = data["usage"]
    tasks = usage["tasks"]
    known = [task["known_tokens"] for task in tasks if task["known_tokens"] is not None]
    out += ["", "persisted usage (all time, opt-in)",
            f"attempted tasks: {usage['attempted_tasks']}  independently accepted: 0 (unverified)",
            f"known tokens: {sum(known) if known else 'N/A'}  "
            f"complete tasks: {sum(task['tokens_complete'] for task in tasks)}  "
            f"incomplete tasks: {sum(not task['tokens_complete'] for task in tasks)}",
            "tokens per independently accepted task: N/A  observed cost: N/A",
            "", "advice", *data["advice"]]
    return "\n".join(out)


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="router report", description=HELP)
    parser.add_argument("--since", default=DEFAULT_SINCE, metavar="N", help="window like 36h or 7d (default 7d, at most 90d)")
    parser.add_argument("--json", action="store_true", help="print the data as JSON")
    args = parser.parse_args(argv)
    hours = parse_since(args.since)
    if hours is None:
        print("router report: --since must look like 36h or 7d", file=sys.stderr)
        return 2
    if hours > MAX_HOURS:
        print("router report: --since must be at most 90d", file=sys.stderr)
        return 2
    now = time.time()
    try:
        data = collect(now - hours * 3600, now)
    except (sqlite3.Error, OSError, ValueError):
        print("router report: request rejected by usage or ledger store", file=sys.stderr)
        return 1
    except ImportError:
        print("router report: usage store unavailable", file=sys.stderr)
        return 1
    print(json.dumps(data, sort_keys=True, indent=2) if args.json else render(data, args.since))
    return 0
