#!/usr/bin/env python3
"""Count the always-on description tokens and per-spawn role bodies against the C8 ceilings.

Estimate only: tokens = ceil(bytes / 4), with no tokenizer, no model, and no network.
This file is identical in Router and Harness.
"""
import argparse
import itertools
import json
from pathlib import Path
import re
import sys

CEILINGS = {"router": 650, "harness": 600, "together": 900}
# Role body sizes after the closing --- line; the limit is the size plus 5%, rounded down.
BODY_SIZES = {"builder-in-place": 2141, "builder": 2101, "docs-writer": 1717, "judge": 2884,
              "planner": 1674, "researcher": 1682, "sweeper": 1603, "test-writer": 1785,
              "worker": 1737}
FRONT = re.compile(r"---\n(.*?)\n---\n(.*)", re.DOTALL)
FIRST_LINE = "budget (estimate: bytes/4, no tokenizer)"


def tokens(size):
    return -(-size // 4)


def body_limit(role):
    return BODY_SIZES[role] * 105 // 100


def front(text):
    """Front matter fields (indented lines continue the previous field) and the body."""
    match = FRONT.fullmatch(text)
    if not match:
        return {}, text
    fields, key = {}, None
    for line in match[1].splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            fields[key] = value.strip()
    return fields, match[2]


def toml_field(text, key):
    match = re.search(rf'^{key} = (".*")$', text, re.MULTILINE)
    if not match:
        return ""
    try:
        return json.loads(match[1])
    except ValueError:
        return match[1][1:-1]


def read_entry(kind, key, path):
    data = path.read_bytes()
    text = data.decode("utf-8", "replace")
    if path.suffix == ".toml":
        name, description = toml_field(text, "name"), toml_field(text, "description")
    else:
        fields = front(text)[0]
        name, description = fields.get("name", ""), fields.get("description", "")
    name = name or key
    return dict(kind=kind, key=key, description=description, data=data,
                bytes=len(name.encode()) + len(description.encode()))


def label_of(path):
    router = (path / "bin/router").is_file() or (path / "router/install-manifest.json").is_file()
    harness = (path / "hooks/checkpoint").exists() or (path / "harness/install-manifest.json").is_file()
    return "together" if router and harness else "router" if router else "harness" if harness else "tree"


def load_tree(path, host="claude"):
    path = Path(path)
    codex = host == "codex"
    agent_dir = path / "codex/agents" if codex and (path / "codex/agents").is_dir() else path / "agents"
    entries = {}
    for item in sorted(agent_dir.glob("*.toml" if codex else "*.md")):
        entries[("agent", item.stem)] = read_entry("agent", item.stem, item)
    for item in sorted(path.glob("skills/*/SKILL.md")):
        entries[("skill", item.parent.name)] = read_entry("skill", item.parent.name, item)
    return dict(path=path, host=host, label=label_of(path), entries=entries, agent_dir=agent_dir)


def summary(label, entries):
    agents = sum(e["bytes"] for e in entries if e["kind"] == "agent")
    skills = sum(e["bytes"] for e in entries if e["kind"] == "skill")
    return dict(label=label, bytes=agents + skills, tokens=tokens(agents + skills), agents=agents, skills=skills)


def doubled(entries):
    groups = {}
    for entry in entries:
        if entry["description"] and not entry["description"].startswith("Tier of "):
            groups.setdefault(entry["description"], set()).add(entry["key"])
    flags = []
    for names in groups.values():
        for first, second in itertools.combinations(sorted(names), 2):
            flags.append(f"doubled description: {first} and {second}")
    return sorted(flags)


def body_problems(tree):
    """Body limits and Codex mentions for Claude Markdown roles in one tree."""
    bodies, problems = {}, []
    if tree["host"] != "claude":
        return bodies, problems
    for item in sorted(tree["agent_dir"].glob("*.md")):
        body = front(item.read_text(encoding="utf-8", errors="replace"))[1]
        size = len(body.encode())
        if item.stem in BODY_SIZES:
            bodies[item.stem] = dict(bytes=size, limit=body_limit(item.stem))
            if size > body_limit(item.stem):
                problems.append(f"body {item.stem} is {size} B, limit {body_limit(item.stem)} B")
        if "Codex" in body:
            problems.append(f"agents/{item.name} mentions Codex outside the TOMLs")
    return bodies, problems


def report(trees):
    trees = list(trees)
    merged, flags = [], []
    seen = {}
    for tree in trees:
        for key, entry in tree["entries"].items():
            if key not in seen:
                seen[key] = entry
                merged.append(entry)
            elif seen[key]["data"] != entry["data"]:
                merged.append(entry)
                flags.append(f"{key[1]} differs between trees")
    flags = doubled(merged) + sorted(set(flags))
    rows = [summary(tree["label"], tree["entries"].values()) for tree in trees]
    union = summary("together", merged) if len(trees) > 1 else None
    problems, bodies = [], {}
    for row in rows + ([union] if union else []):
        ceiling = CEILINGS.get(row["label"])
        if ceiling is not None and row["tokens"] > ceiling:
            problems.append(f"{row['label']} always-on {row['tokens']} tokens is over the ceiling {ceiling}")
    for tree in trees:
        found, extra = body_problems(tree)
        for role, value in found.items():
            bodies.setdefault(role, value)
        problems += extra
    problems += flags
    unique = list(dict.fromkeys("budget: " + line for line in problems))
    return dict(estimate=True, trees=rows, union=union, bodies=bodies, flags=flags, problems=unique)


def line(row):
    return (f"{row['label']}: {row['bytes']} B always-on = {row['tokens']} tokens "
            f"(agents {row['agents']} B, skills {row['skills']} B)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="measure an installed host home instead of this checkout")
    parser.add_argument("--with", dest="sibling", help="add a second tree; identical roles count once")
    parser.add_argument("--host", choices=("claude", "codex"), default="claude")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    parser.add_argument("--check", action="store_true", help="exit 1 and print each problem to stderr")
    args = parser.parse_args(argv)
    first = Path(args.home) if args.home else Path(__file__).resolve().parents[1]
    trees = [load_tree(first, args.host)]
    if args.sibling:
        trees.append(load_tree(args.sibling, args.host))
    result = report(trees)
    if args.json:
        print(json.dumps({k: v for k, v in result.items()}, sort_keys=True))
    else:
        print(FIRST_LINE)
        for row in result["trees"] + ([result["union"]] if result["union"] else []):
            print(line(row))
        for flag in result["flags"]:
            print("flag: " + flag)
        print("hooks: 0 B always-on (default config)")
        print("injection caps: checked by tests/together.sh")
    failed = args.check and bool(result["problems"])
    for problem in result["problems"] if failed else ():
        print(problem, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
