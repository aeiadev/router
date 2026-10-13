"""Pure offline Router evaluation. Coordinator attestations are never host proof."""
import json
import re

import usage

HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
CONTROL_KEYS = ("host_version", "model", "effort", "hook_digest", "skill_digest",
                "permission_digest", "config_digest", "cache_condition", "cache_provenance",
                "rubric_hash", "revision_hash")
DIGEST_CONTROLS = {"hook_digest", "skill_digest", "permission_digest", "config_digest",
                   "rubric_hash", "revision_hash"}
FAMILIES = {"tinyanswer", "singleedit", "retryfix", "fakeoperations"}
FAILURES = {"none", "missing_connector", "permission", "interruption", "substantive", "unknown"}


class EvaluationError(ValueError):
    """A bounded schema or replay error; message never contains input."""


def _bad(reason="invalid evaluation input"):
    raise EvaluationError(reason)


def _keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _bad("invalid evaluation fields")


def _text(value, pattern=IDENT):
    if type(value) is not str or pattern.fullmatch(value) is None:
        _bad("invalid evaluation value")
    return value


def _number(value):
    if type(value) is not int or not 0 <= value <= usage.MAX_COUNT:
        _bad("invalid evaluation count")
    return value


def _boolean(value):
    if type(value) is not bool:
        _bad("invalid evaluation flag")
    return value


def _list(value, limit=10000):
    if type(value) is not list or len(value) > limit:
        _bad("invalid evaluation list")
    return value


def _control(value):
    _keys(value, CONTROL_KEYS)
    for key in CONTROL_KEYS:
        if key in DIGEST_CONTROLS:
            _text(value[key], HASH)
        elif key == "cache_condition":
            if value[key] not in ("cold", "warm", "unknown"):
                _bad("invalid cache condition")
        else:
            _text(value[key], LABEL)
    return value


def _manifest(value):
    _keys(value, ("schema_version", "experiment_id", "candidate_id", "seed", "treatment", "controls", "pairs"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _bad("unsupported evaluation schema")
    _text(value["experiment_id"])
    _text(value["candidate_id"])
    _number(value["seed"])
    treatment = value["treatment"]
    if type(treatment) is not dict or not treatment or treatment.keys() - set(CONTROL_KEYS):
        _bad("invalid treatment")
    for field, arms in treatment.items():
        _keys(arms, ("baseline", "candidate"))
        for arm in ("baseline", "candidate"):
            if field in DIGEST_CONTROLS:
                _text(arms[arm], HASH)
            else:
                _text(arms[arm], LABEL)
        if arms["baseline"] == arms["candidate"]:
            _bad("unchanged treatment")
    _keys(value["controls"], usage.HOSTS)
    for controls in value["controls"].values():
        _control(controls)
        if any(controls[field] != arms["baseline"] for field, arms in treatment.items()):
            _bad("treatment baseline mismatch")
    pairs = {}
    for pair in _list(value["pairs"]):
        _keys(pair, ("pair_id", "task_id", "family", "prompt_digest", "rubric_hash", "order", "seed"))
        for key in ("pair_id", "task_id"):
            _text(pair[key])
        for key in ("prompt_digest", "rubric_hash"):
            _text(pair[key], HASH)
        if type(pair["family"]) is not str or pair["family"] not in FAMILIES or pair["order"] not in ("baseline-first", "candidate-first"):
            _bad("invalid frozen pair")
        _number(pair["seed"])
        if pair["pair_id"] in pairs:
            _bad("conflicting frozen pair")
        pairs[pair["pair_id"]] = pair
    return pairs


def _stage(value, host):
    _keys(value, ("stage_id", "attempt_id", "stage", "outcome", "failure_category", "duration_ms",
                  "adapter", "stream_id", "sequence", "from_start", "usage", "missing"))
    _text(value["stage_id"])
    _text(value["attempt_id"])
    if any(type(value[key]) is not str for key in ("stage", "outcome", "failure_category")) or value["stage"] not in usage.STAGES or value["outcome"] not in usage.OUTCOMES or value["failure_category"] not in FAILURES:
        _bad("invalid stage outcome")
    _number(value["duration_ms"])
    if type(value["usage"]) is not dict:
        _bad("invalid normalized usage")
    event = dict(event_id=value["stage_id"], attempt_id=value["attempt_id"],
                 stage=value["stage"], outcome=value["outcome"], context_bytes=None,
                 adapter=value["adapter"], host=host,
                 usage=value["usage"], missing=value["missing"], stream_id=None,
                 sequence=value["sequence"], from_start=value["from_start"])
    event["stream_id"] = value["stream_id"]
    try:
        event = usage._event(event)
    except (usage.UsageError, TypeError, AttributeError, ValueError):
        _bad("invalid normalized usage")
    return event


def _run(value):
    _keys(value, ("run_id", "pair_id", "task_id", "family", "prompt_digest", "host", "arm", "controls",
                  "owner_prompt_count", "intervention_count", "expected_stages", "outcome",
                  "critical_false_negative", "unauthorized_effect", "stages"), ("external_state", "quality_regression"))
    for key in ("run_id", "pair_id", "task_id"):
        _text(value[key])
    _text(value["prompt_digest"], HASH)
    if type(value["family"]) is not str or type(value["host"]) is not str or value["family"] not in FAMILIES or value["host"] not in usage.HOSTS or value["arm"] not in ("baseline", "candidate"):
        _bad("invalid run identity")
    if type(value["outcome"]) is not str or value["outcome"] not in usage.OUTCOMES:
        _bad("invalid run outcome")
    _control(value["controls"])
    for key in ("owner_prompt_count", "intervention_count"):
        _number(value[key])
    for key in ("critical_false_negative", "unauthorized_effect"):
        _boolean(value[key])
    if "quality_regression" in value:
        _boolean(value["quality_regression"])
    if "external_state" in value:
        _boolean(value["external_state"])
    expected = [_text(x) for x in _list(value["expected_stages"])]
    if not expected or len(set(expected)) != len(expected):
        _bad("invalid stage inventory")
    seen = set()
    for stage in _list(value["stages"]):
        _stage(stage, value["host"])
        if stage["stage_id"] in seen:
            _bad("conflicting stage replay")
        seen.add(stage["stage_id"])
    return value


def _evidence(value):
    required = ("task_id", "run_id", "revision_hash", "rubric_hash", "verifier_id", "author_id",
                "proof_digest", "method", "check_id", "result_digest", "check_exit", "check_status", "provenance")
    _keys(value, required, ("state_digest", "state_ref", "observer_permission_ref"))
    for key in ("task_id", "run_id", "verifier_id", "author_id", "method", "check_id"):
        _text(value[key])
    for key in ("revision_hash", "rubric_hash", "proof_digest"):
        _text(value[key], HASH)
    if value["result_digest"] is not None:
        _text(value["result_digest"], HASH)
    _number(value["check_exit"])
    if value["check_status"] not in ("PASS", "FAIL", "UNKNOWN") or value["provenance"] != "coordinator_attestation":
        _bad("invalid acceptance provenance")
    for key in ("state_digest",):
        if key in value:
            _text(value[key], HASH)
    for key in ("state_ref", "observer_permission_ref"):
        if key in value:
            _text(value[key])
    return value


def verification_method(family, external_state=False, material=False):
    """Rubric advice only; never changes dispatch or permissions."""
    if family not in FAMILIES or type(external_state) is not bool or type(material) is not bool:
        _bad("invalid verification request")
    return "observer" if external_state else "independent_review" if material or family != "tinyanswer" else "direct_inspection"


def _accepted(run, evidence):
    if evidence is None:
        return False
    return (evidence["task_id"] == run["task_id"] and evidence["run_id"] == run["run_id"]
            and run["outcome"] == "returned"
            and evidence["revision_hash"] == run["controls"]["revision_hash"]
            and evidence["rubric_hash"] == run["controls"]["rubric_hash"]
            and evidence["verifier_id"] != evidence["author_id"]
            and evidence["result_digest"] is not None and evidence["check_exit"] == 0
            and evidence["check_status"] == "PASS"
            and (not run.get("external_state") or all(evidence.get(k) for k in
                 ("state_digest", "state_ref", "observer_permission_ref"))))


def evaluate(data):
    """Evaluate strict schema-1 synthetic records; no I/O, adoption, or host observation."""
    try:
        if len(json.dumps(data, ensure_ascii=True, allow_nan=False)) > 4 * 1024 * 1024:
            _bad("evaluation input too large")
    except (TypeError, ValueError, OverflowError, RecursionError):
        _bad("invalid evaluation JSON")
    _keys(data, ("manifest", "runs", "acceptance"))
    pairs = _manifest(data["manifest"])
    runs = {}
    for run in _list(data["runs"]):
        _run(run)
        old = runs.get(run["run_id"])
        if old is not None and old != run:
            _bad("conflicting run replay")
        runs[run["run_id"]] = run
    evidence = {}
    checks = {}
    for item in _list(data["acceptance"]):
        _evidence(item)
        check = checks.get(item["check_id"])
        if check is not None and check != item:
            _bad("conflicting acceptance check invocation")
        checks[item["check_id"]] = item
        old = evidence.get(item["run_id"])
        if old is not None and old != item:
            _bad("conflicting acceptance replay")
        evidence[item["run_id"]] = item
    if set(evidence) - set(runs):
        _bad("orphan acceptance evidence")
    by_host = {host: {arm: dict(attempted=0, accepted=0, rejected=0, unscorable=0,
                                 complete_usage=0, complete_stage_coverage=0, missing_expected_stages=0,
                                 known_subtotal=0, all_attempt_tokens=None,
                                 tokens_per_attempt=None, tokens_per_accepted=None,
                                 cached_input_tokens=None, reasoning_output_tokens=None,
                                 known_cached_input_tokens=0, known_reasoning_output_tokens=0,
                                 stage_attempts=0,
                                 incomplete_failure_attribution=0,
                                 failure_attribution_missing_counts={
                                     "unknown_stage_outcome": 0, "unknown_failure_category": 0},
                                 outcome_counts={key: 0 for key in usage.OUTCOMES},
                                 failure_category_counts={key: 0 for key in FAILURES},
                                 missing_counts={key: 0 for key in usage.METRICS})
                      for arm in ("baseline", "candidate")} for host in sorted(usage.HOSTS)}
    run_info = {}
    for run in runs.values():
        out = by_host[run["host"]][run["arm"]]
        out["attempted"] += 1
        out["outcome_counts"][run["outcome"]] += 1
        accepted = _accepted(run, evidence.get(run["run_id"]))
        out["accepted" if accepted else "rejected" if run["run_id"] in evidence else "unscorable"] += 1
        inventory = {s["stage_id"] for s in run["stages"]} == set(run["expected_stages"])
        absent = len(set(run["expected_stages"]) - {s["stage_id"] for s in run["stages"]})
        out["missing_expected_stages"] += absent
        for metric in usage.METRICS:
            out["missing_counts"][metric] += absent
        totals = []
        events = []
        attribution_complete = True
        for stage in run["stages"]:
            event = _stage(stage, run["host"])
            try:
                counted, coverage = usage._counted(event, events, run["host"])
            except (usage.UsageError, TypeError, AttributeError, ValueError):
                _bad("invalid cumulative usage")
            events.append({**event, "counted_usage": counted, "coverage_complete": coverage})
            out["stage_attempts"] += 1
            out["failure_category_counts"][stage["failure_category"]] += 1
            for field, reason in (("outcome", "unknown_stage_outcome"),
                                  ("failure_category", "unknown_failure_category")):
                if stage[field] == "unknown":
                    attribution_complete = False
                    out["failure_attribution_missing_counts"][reason] += 1
            values = counted
            for metric in usage.METRICS:
                if values[metric] is None:
                    out["missing_counts"][metric] += 1
            for metric in ("cached_input_tokens", "reasoning_output_tokens"):
                if values[metric] is not None:
                    out["known_" + metric] += values[metric]
            total = usage._total(values, run["host"])
            totals.append(total)
            if total is not None:
                out["known_subtotal"] += total
            else:
                out["known_subtotal"] += sum(values[k] or 0 for k in usage._billable(values, run["host"]))
        if inventory and events and all(e["coverage_complete"] for e in events):
            out["complete_stage_coverage"] += 1
        if not attribution_complete:
            out["incomplete_failure_attribution"] += 1
        # Independent acceptance survives incomplete accounting; efficiency does not.
        complete = (attribution_complete and inventory and bool(totals) and all(t is not None for t in totals)
                    and all(e["coverage_complete"] and e["attempt_id"] is not None and e["stage"] != "unknown"
                            and all(e["counted_usage"][key] is not None for key in usage._billable(e["counted_usage"], run["host"]))
                            for e in events))
        if complete:
            out["complete_usage"] += 1
        run_info[run["run_id"]] = (accepted, complete, sum(totals) if complete else None)
    for arms in by_host.values():
        for out in arms.values():
            for metric in ("cached_input_tokens", "reasoning_output_tokens"):
                if (out["attempted"] and out["complete_stage_coverage"] == out["attempted"]
                        and out["missing_counts"][metric] == 0):
                    out[metric] = out["known_" + metric]
            if out["attempted"] and out["complete_usage"] == out["attempted"]:
                out["all_attempt_tokens"] = out["known_subtotal"]
                out["tokens_per_attempt"] = out["known_subtotal"] / out["attempted"]
                if out["accepted"]:
                    out["tokens_per_accepted"] = out["known_subtotal"] / out["accepted"]
    pair_reports = {}
    rejection = any(r["critical_false_negative"] or r["unauthorized_effect"] or r.get("quality_regression", False)
                    for r in runs.values())
    complete_pilot = (len(pairs) >= 4 and {p["family"] for p in pairs.values()} == FAMILIES
                      and all(r["pair_id"] in pairs for r in runs.values()))
    for host in sorted(usage.HOSTS):
        matched, differences, strata = 0, [], {"cold": 0, "warm": 0}
        for pair_id, pair in pairs.items():
            sides = {arm: [r for r in runs.values() if r["host"] == host and r["pair_id"] == pair_id and r["arm"] == arm]
                     for arm in ("baseline", "candidate")}
            if any(len(sides[a]) != 1 for a in sides):
                complete_pilot = False
                continue
            base, candidate = sides["baseline"][0], sides["candidate"][0]
            if any(r["task_id"] != pair["task_id"] or r["family"] != pair["family"]
                   or r["prompt_digest"] != pair["prompt_digest"] or r["controls"]["rubric_hash"] != pair["rubric_hash"]
                   for r in (base, candidate)):
                complete_pilot = False
                continue
            treatment = data["manifest"]["treatment"]
            frozen = data["manifest"]["controls"][host]
            if any(r["controls"][field] != (treatment[field][arm] if field in treatment else frozen[field])
                   for arm, r in (("baseline", base), ("candidate", candidate)) for field in CONTROL_KEYS):
                complete_pilot = False
                continue
            if any(base["controls"][field] != candidate["controls"][field] for field in CONTROL_KEYS if field not in treatment):
                complete_pilot = False
                continue
            if any(r["controls"][field] != values[arm] for field, values in treatment.items()
                   for arm, r in (("baseline", base), ("candidate", candidate))):
                complete_pilot = False
                continue
            cache = base["controls"]["cache_condition"]
            if cache == "unknown" or candidate["controls"]["cache_condition"] == "unknown":
                complete_pilot = False
                continue
            b, c = run_info[base["run_id"]], run_info[candidate["run_id"]]
            if not all((b[0], b[1], c[0], c[1])):
                complete_pilot = False
                continue
            matched += 1
            strata[cache] += 1
            differences.append(dict(pair_id=pair_id, candidate_minus_baseline=c[2] - b[2]))
            if pair["family"] == "tinyanswer" and (candidate["owner_prompt_count"] > base["owner_prompt_count"]
                                                    or candidate["intervention_count"] > base["intervention_count"]):
                rejection = True
        pair_reports[host] = dict(matched=matched, differences=differences, cache_strata=strata)
        if matched < len(pairs) or not matched:
            complete_pilot = False
        arms = by_host[host]
        if any(arms[a]["tokens_per_accepted"] is None for a in arms):
            complete_pilot = False
        if arms["candidate"]["accepted"] < arms["baseline"]["accepted"]:
            rejection = True
    if rejection:
        decision = "reject"
    elif not complete_pilot:
        decision = "inconclusive"
    elif all(by_host[h]["candidate"]["tokens_per_accepted"] < by_host[h]["baseline"]["tokens_per_accepted"]
             and all(d["candidate_minus_baseline"] < 0 for d in pair_reports[h]["differences"]) for h in usage.HOSTS):
        decision = "screening_only"
    else:
        decision = "reject"
    return dict(schema_version=1, decision=decision, adopted=False, host_verified=False,
                evidence_kind="coordinator_attestation", scope="four_family_pilot_screening_only",
                by_host=by_host, pairs=pair_reports)
