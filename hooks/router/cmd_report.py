"""router report [--since N] [--json]: what Router did lately, with advice. Nothing is changed.

N is like 36h or 7d (default 7d, at most 90d). Sections: spawns by role and tier, judge coverage,
first pass by tier, context guard counts, shadow hits, tokens by model and a few advice lines.
Privacy: no label, brief, key or finding text is printed. Numbers only, never dollars.
Codex: spawns and lanes are counted, there are no verdicts, and rollouts are not read for tokens.
"""
import argparse
import calendar
import json
import re
import sqlite3
import sys
import time

import common
import ledger

HELP = "report [--since N] [--json]: spawns, judge coverage, first-pass rate, over-cap returns, shadow hits, tokens"
DEFAULT_SINCE = "7d"
MAX_HOURS = 90 * 24
LOG_TAIL_BYTES = 4 * 1024 * 1024
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPTS = 200
HOSTS = {"c:": "claude", "x:": "codex"}
USAGE_FIELDS = (("input", "input_tokens"), ("output", "output_tokens"),
                ("cache_read", "cache_read_input_tokens"), ("cache_creation", "cache_creation_input_tokens"))
NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,60}")
MODEL = re.compile(r"[A-Za-z0-9_.:/\-\[\]]{1,80}")
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


def read_tokens(since):
    """Token sums per model over Claude transcripts modified since; None when none is readable."""
    root = common.claude_home() / "projects"
    found = []
    try:
        for path in root.rglob("*.jsonl"):
            try:
                if not path.is_symlink() and path.is_file() and path.stat().st_mtime >= since:
                    found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return {"root": str(root), "sampled": 0, "models": {}}
    found.sort(key=lambda item: item[0], reverse=True)
    models, sampled = {}, 0
    for _, path in found[:MAX_TRANSCRIPTS]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                start = max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES)
                handle.seek(start)
                raw = handle.read(TRANSCRIPT_TAIL_BYTES)
        except OSError:
            continue
        sampled += 1
        lines = raw.split(b"\n")
        for line in lines[1:] if start > 0 else lines:
            if b'"assistant"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
            model = message.get("model") if isinstance(message, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(model, str) or model.startswith("<") or not isinstance(usage, dict):
                continue
            model = safe(model.strip(), MODEL, "")
            if not model:
                continue
            total = models.setdefault(model, dict.fromkeys((name for name, _ in USAGE_FIELDS), 0))
            for name, field in USAGE_FIELDS:
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    total[name] += value
    return {"root": str(root), "sampled": sampled, "models": models}


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
        "tokens": read_tokens(since), "advice": advice[:3] or ["advice: none"],
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
    tokens = data["tokens"]
    out += ["", "tokens by model"]
    if not tokens["sampled"]:
        out.append(f"tokens: no readable transcripts under {tokens['root']}")
    else:
        out.append(f"sampled {tokens['sampled']} transcripts")
        out += table(("model", "input", "output", "cache_read", "cache_creation"),
                     [(model, *(total[name] for name, _ in USAGE_FIELDS))
                      for model, total in sorted(tokens["models"].items())]) if tokens["models"] else ["none"]
    out += ["tokens: Codex rollouts are not read", "", "advice", *data["advice"]]
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
    path = ledger.ledger_path()
    try:
        data = collect(now - hours * 3600, now)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"router report: {path}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, sort_keys=True, indent=2) if args.json else render(data, args.since))
    return 0
