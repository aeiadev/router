#!/usr/bin/env python3
"""Standalone probes for the bounded context pressure reader."""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks/router"))
import pressure


def host_choice():
    table = [
        ({"harness_host": "claude", "turn_id": "t"}, ["hook", "--host=codex"], "claude"),
        ({"harness_host": "codex"}, ["hook", "--host=claude"], "codex"),
        ({"harness_host": "other", "turn_id": "t"}, ["hook", "--host=claude"], "claude"),
        ({}, ["hook", "--host=codex"], "codex"),
        ({"turn_id": "t"}, ["hook", "--host=invalid"], "codex"),
        ({}, ["hook"], "claude"),
    ]
    for data, argv, expected in table:
        assert pressure.host_of(data, argv) == expected, (data, argv, expected)


def derived():
    def table(first, size=24000, **extra):
        return {"context": dict({"chain": {"first": first}, "large_read_bytes": size}, **extra)}
    def at(routes, level):
        return pressure.tightened(routes, {"level": level})
    assert at(table(5), "remind") == (3, 24000) and at(table(5), "urgent") == (2, 12000)
    assert at(table(2), "remind") == (2, 24000) and at(table(2), "urgent") == (1, 12000)
    assert at(table(1), "remind") == (1, 24000) and at(table(1), "urgent") == (1, 12000)
    assert at(table(5), "ok") == (5, 24000) and at(table(5), None) == (5, 24000)
    assert at(table(5, 1), "urgent") == (2, 1)
    assert at(table(5, 24000), "urgent")[1] == 12000
    over = table(5, pressure={"urgent": {"chain_first": 4}})
    assert at(over, "urgent") == (4, 12000) and at(over, "remind") == (3, 24000)
    over = table(5, pressure={"remind": {"large_read_bytes": 7000}})
    assert at(over, "remind") == (3, 7000)
    assert pressure.limits(table(5, pressure={"remind": {"chain_first": 9, "large_read_bytes": 99999}}), "remind") == (5, 24000)
    assert pressure.limits(table(2), "remind")[0] <= 2 and pressure.limits(table(3), "remind")[0] == 2


def main():
    host_choice()
    derived()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = dict(os.environ)
        try:
            os.environ.update(HOME=str(root / "home"), XDG_STATE_HOME=str(root / "xdg"),
                              ROUTER_STATE=str(root / "router"), XDG_CONFIG_HOME=str(root / "config"),
                              ROUTER_LOCAL="off")
            session = "pressure-fixture"
            path = root / "xdg/claude-harness/pressure" / (hashlib.sha256(session.encode()).hexdigest()[:16] + ".json")
            path.parent.mkdir(parents=True)
            record = {"schema": 1, "host": "claude", "ts": 1000, "used": 750,
                      "window": 1000, "window_source": "statusline", "percent": 75.0, "level": "remind"}
            marker = root / "router/pressure-warned"
            log = root / "router/errors.jsonl"
            def expect_warning(label):
                marker.unlink(missing_ok=True)
                before = len(log.read_text().splitlines()) if log.exists() else 0
                for _ in range(3):
                    assert pressure.read(session, "claude", now=1000) is None, label
                after = len(log.read_text().splitlines())
                assert after - before == 1, (label, before, after)
                assert json.loads(log.read_text().splitlines()[-1])["file"] == str(path)
            path.write_text(json.dumps(record))
            assert pressure.path_for(session) == path
            assert pressure.read(session, "claude", now=1000) == record
            assert pressure.read(session, "codex", now=1000) is None
            codex_record = dict(record, host="codex")
            path.write_text(json.dumps(codex_record))
            assert pressure.read(session, "codex", now=1000) == codex_record
            path.write_text(json.dumps(record))
            assert pressure.read(session, "claude", now=1601) is None
            record["ts"] = 1060
            path.write_text(json.dumps(record))
            assert pressure.read(session, "claude", now=1000) == record
            record["ts"] = 1061
            path.write_text(json.dumps(record))
            assert pressure.read(session, "claude", now=1000) is None, "future timestamp was accepted"
            for key, value in (("schema", True), ("ts", float("nan")), ("percent", True),
                               ("percent", 1001), ("used", True), ("window", 0), ("level", "bad")):
                bad = dict(record, ts=1000, **{key: value}) if key != "ts" else dict(record, ts=value)
                path.write_text(json.dumps(bad))
                assert pressure.read(session, "claude", now=1000) is None, (key, value)
            for label, raw in (("malformed", "{"), ("deep", "[" * 5000 + "]" * 5000),
                               ("oversized", "x" * 4097)):
                path.write_text(raw)
                expect_warning(label)
            path.unlink()
            target = root / "target.json"
            target.write_text(json.dumps(record))
            path.symlink_to(target)
            expect_warning("symlink")
            path.unlink()
            path.mkdir()
            expect_warning("directory")
            path.rmdir()
            path.write_text(json.dumps(dict(record, ts=1000)))
            path.chmod(0)
            expect_warning("unreadable")
            path.chmod(0o600)
            path.parent.chmod(0)
            expect_warning("unsearchable")
            path.parent.chmod(0o700)
            routes = {"context": {"chain": {"first": 2}, "large_read_bytes": 10000,
                                  "pressure": {"remind": {"chain_first": 3, "large_read_bytes": 12000}}}}
            assert pressure.tightened(routes, {"level": "remind"}) == (2, 10000)
            os.environ["XDG_STATE_HOME"] = "relative-state"
            assert pressure.path_for(session) == root / "home/.local/state/claude-harness/pressure" / path.name
            print("PASS pressure reader and tightening")
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    main()
