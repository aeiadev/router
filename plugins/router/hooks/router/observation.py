"""Pure, report-only assessment of independently supplied readback digests."""

import re


class ObservationError(ValueError):
    """Invalid observation contract input; messages never contain supplied values."""


_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_EXPECTED = {"schema_version", "task_id", "target_id", "revision_id", "requirements"}
_OBSERVED = _EXPECTED | {"read_status", "evidence_id", "verifier_id"}
_REFERENCES = ("task_id", "target_id", "revision_id", "evidence_id", "verifier_id")


def _reference(value):
    if type(value) is not str or _REF.fullmatch(value) is None:
        raise ObservationError("invalid opaque reference")


def _requirements(value, allow_empty):
    if type(value) is not dict or len(value) > 128 or (not allow_empty and not value):
        raise ObservationError("invalid requirements")
    for key, digest in value.items():
        _reference(key)
        if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
            raise ObservationError("invalid requirement digest")


def _validate(expected, observed):
    if type(expected) is not dict or set(expected) != _EXPECTED:
        raise ObservationError("invalid expected schema")
    if type(expected["schema_version"]) is not int or expected["schema_version"] != 1:
        raise ObservationError("invalid schema version")
    for key in ("task_id", "target_id", "revision_id"):
        _reference(expected[key])
    _requirements(expected["requirements"], False)

    if type(observed) is not dict or not {"schema_version", "read_status"} <= set(observed):
        raise ObservationError("invalid observed schema")
    if set(observed) - _OBSERVED:
        raise ObservationError("invalid observed schema")
    if type(observed["schema_version"]) is not int or observed["schema_version"] != 1:
        raise ObservationError("invalid schema version")
    if type(observed["read_status"]) is not str or observed["read_status"] not in (
            "ok", "denied", "unavailable"):
        raise ObservationError("invalid read status")
    for key in _REFERENCES:
        if key in observed:
            _reference(observed[key])
    if "requirements" in observed:
        _requirements(observed["requirements"], True)


def assess(expected, observed):
    """Assess supplied proof; never read, authorize, replay, or gate a mutation."""
    _validate(expected, observed)
    status = observed["read_status"]
    if status != "ok":
        verdict, reason = "UNKNOWN", "read_" + status
    elif any(key not in observed for key in _REFERENCES) or "requirements" not in observed or any(
            key not in observed["requirements"] for key in expected["requirements"]):
        verdict, reason = "UNKNOWN", "incomplete_proof"
    else:
        mismatch = next((key for key in ("task_id", "target_id", "revision_id")
                         if observed[key] != expected[key]), None)
        if mismatch is not None:
            verdict, reason = "FAIL", mismatch + "_mismatch"
        elif any(observed["requirements"][key] != digest
                 for key, digest in expected["requirements"].items()):
            verdict, reason = "FAIL", "digest_mismatch"
        else:
            verdict, reason = "PASS", "matched"
    return {"reported_verdict": verdict, "reason_code": reason,
            "evidence_id": observed.get("evidence_id"),
            "verifier_id": observed.get("verifier_id"),
            "mode": "report-only", "gates_mutation": False, "host_verified": False,
            "independent_acceptance": "unverified"}
