"""Static Router and Harness adapter evidence. No host inspection occurs here."""

import re

_VERSIONS = {"claude": "2.1.296", "codex": "0.156.1"}
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+", re.ASCII)
_IDS = ("role_discovery", "model_pins_inheritance", "hook_payload_fields",
        "trusted_hook_lifecycle", "tool_rewrites", "completion_events",
        "tool_visibility", "read_only_enforcement", "usage_formats")
_FIELDS = {
    ("router", "claude"): ("hook_event_name", "tool_name", "tool_input.prompt",
        "tool_input.subagent_type", "tool_input.model", "tool_input.description", "tool_input.message",
        "session_id", "cwd", "transcript_path", "agent_id", "agent_type", "harness_host",
        "agent_transcript_path", "last_assistant_message", "stop_hook_active", "source"),
    ("router", "codex"): ("tool_name", "tool_input.agent_type", "tool_input.model",
        "tool_input.task_name", "session_id", "cwd", "turn_id", "source", "harness_host"),
    ("harness", "claude"): ("agent_id", "harness_host", "session_id", "sessionId",
        "conversation_id", "thread_id", "cwd", "project_dir",
        "workspace.current_dir", "source", "tool_response", "transcript_path", "context_window",
        "context_window.context_window_size", "context_window.current_usage", "compact_summary",
        "prompt_cache_likely_expired", "context_tokens"),
    ("harness", "codex"): ("agent_id", "harness_host", "session_id", "sessionId",
        "conversation_id", "thread_id", "cwd", "project_dir", "workspace.current_dir",
        "source", "tool_response", "transcript_path", "context_window",
        "context_window.context_window_size", "context_window.current_usage", "compact_summary",
        "turn_id", "type", "model"),
}
_DETAIL = {
    "role_discovery": "Installed or generated role file fixtures; host loading unverified.",
    "model_pins_inheritance": "Role settings and routing fixtures; actual model selection and inheritance unverified.",
    "hook_payload_fields": "Fields consumed by source, not guaranteed supplied by a host.",
    "trusted_hook_lifecycle": "Registration presence does not establish host trust.",
    "tool_rewrites": "Adapter rewrite behavior only; host application unverified.",
    "completion_events": "Adapter event handling only; worker completion unverified on this host.",
    "tool_visibility": "Parent, role and connector visibility unverified.",
    "read_only_enforcement": "Role prose or profile presence does not establish enforcement.",
    "usage_formats": "B2 offline host-format parsing; no automatic collection, billing proof or live support.",
}
_ACTION = {
    "role_discovery": "Check role loading in a disposable host home.",
    "model_pins_inheritance": "Check effective role model in a disposable host run.",
    "hook_payload_fields": "Capture field names and types in a disposable host test.",
    "trusted_hook_lifecycle": "Check hook trust in a disposable host setup.",
    "tool_rewrites": "Check a harmless rewrite in a disposable host run.",
    "completion_events": "Check completion event delivery in a disposable host run.",
    "tool_visibility": "Check benign connector visibility in a disposable host setup.",
    "read_only_enforcement": "Check a harmless write denial in a disposable repository.",
    "usage_formats": "Import a synthetic observation explicitly; verify live collection separately.",
}


def describe(host, host_version=None, package="router") -> dict:
    """Return independent static evidence for one package and host."""
    if (not isinstance(host, str) or host not in _VERSIONS
            or not isinstance(package, str) or package not in ("router", "harness")):
        raise ValueError("invalid host or package")
    valid = (isinstance(host_version, str) and len(host_version) <= 32
             and _VERSION.fullmatch(host_version) is not None)
    version = host_version if valid else None
    source = "unavailable" if host_version is None else ("caller-supplied" if valid else "invalid")
    features = []
    for feature_id in _IDS:
        support = "fixture-supported"
        if feature_id in ("trusted_hook_lifecycle", "tool_visibility", "read_only_enforcement"):
            support = "unavailable"
        elif feature_id == "tool_rewrites":
            support = "fixture-supported" if (package, host) == ("router", "claude") else "not-used"
        elif feature_id == "completion_events":
            support = "fixture-supported" if (package, host) == ("router", "claude") else "unavailable"
        elif feature_id == "model_pins_inheritance" and package == "harness":
            support = "not-used"
        detail = _DETAIL[feature_id]
        if feature_id == "model_pins_inheritance":
            detail = ("Shipped shared role settings; no Harness routing policy adapter; inheritance unverified."
                      if package == "harness" else
                      "Claude policy or Codex generated role config fixture; execution and inheritance unverified.")
        elif feature_id == "hook_payload_fields" and (package, host) == ("router", "codex"):
            detail = "Consumes spawn_agent/collaborationspawn_agent fields; message stays encrypted and uninspected."
        elif feature_id == "tool_rewrites" and package == "harness":
            detail = "Harness guards inspect or deny; they do not rewrite tool inputs."
        elif feature_id == "tool_rewrites" and host == "codex":
            detail = "Codex adapter does not use updatedInput; host application unverified."
        elif feature_id == "completion_events" and package == "harness":
            detail = "Stop inspection and spend helpers do not verify worker completion."
        elif feature_id == "completion_events" and host == "codex":
            detail = "Codex SubagentStop is not registered; lane completion is unavailable."
        elif feature_id == "completion_events":
            detail = "Claude SubagentStop lane-link fixture; host delivery unverified."
        features.append({"id": feature_id, "adapter_support": support,
                         "evidence": "source-fixture" if support == "fixture-supported" else "unverified",
                         "host_verified": False, "detail": detail, "action": _ACTION[feature_id],
                         "fields": list(_FIELDS[package, host]) if feature_id == "hook_payload_fields" else []})
    return {"schema_version": 1, "package": package, "host": host, "host_version": version,
            "version_source": source, "reference_version": _VERSIONS[host],
            "reference_match": version == _VERSIONS[host], "host_verified": False, "features": features}


def render_rows(report) -> list[tuple[str, str]]:
    """Render ten status rows without changing the caller's report."""
    version = report["host_version"] or "unknown"
    rows = [("capability version", f"{version} ({report['version_source']}); reference "
             f"{report['reference_version']}; match={str(report['reference_match']).lower()}; host unverified")]
    for feature in report["features"]:
        fields = ""
        if feature["fields"]:
            fields = " Consumed fields: " + ", ".join(feature["fields"][:3])
            if len(feature["fields"]) > 3:
                fields += f" (+{len(feature['fields']) - 3} in describe())."
        rows.append(("capability " + feature["id"],
                     f"{feature['adapter_support']}/{feature['evidence']}; host unverified; "
                     f"{feature['detail']}{fields} Action: {feature['action']}"))
    return rows
