# Offline evaluation

This explicit command scores supplied, privacy-safe experiment records. It does
not run a model, import private transcripts, change routing policy, or accept
work automatically. An evaluator implementation and synthetic tests establish
accounting behavior; they do not establish token savings.

Fix a rubric and one logical treatment before collecting paired runs. Retain
failed and unscorable attempts. Account for coordinator, worker, judge/observer,
retry, restore and continuation stages. Record actual exposed usage and explicit
missing reasons; never substitute byte estimates. Independently execute the
required checks, then supply their bound evidence references. Self-verdicts
cannot create independent acceptance.

A four-family paired pilot is screening evidence only. Smaller judges, reduced
context, extra same-tier retries and hook consolidation remain experiments.
Preserve user pins, attempt caps and safety preflights. Unknown telemetry,
missing attribution or unmatched controls prevents an efficiency conclusion.
No optimization is adopted automatically, even when screening succeeds.

# Offline evaluator API (schema 1)

From the 0.4.1 checkout, import `hooks/router/evaluation.py` with Router directory on `sys.path`; call `evaluate(data) -> dict`, raising `EvaluationError` for bounded invalid input. `verification_method(family, external_state=False, material=False) -> str` returns rubric advice only. No persistence, model call, host verification, or adoption side effect.

Top level: exactly `manifest`, `runs`, `acceptance`. Manifest: `schema_version:1`, opaque `experiment_id`, `candidate_id`, nonnegative integer `seed`, `treatment` mapping changed control name to exact `{baseline,candidate}` values, `controls` containing exact `claude` and `codex` frozen baseline control objects, and `pairs` list. Each pair: opaque `pair_id`, `task_id`, family (`tinyanswer|singleedit|retryfix|fakeoperations`), SHA256 `prompt_digest`, SHA256 `rubric_hash`, `order` (`baseline-first|candidate-first`), integer `seed`.

Each control object has exactly: `host_version`, `model`, `effort`, `hook_digest`, `skill_digest`, `permission_digest`, `config_digest`, `cache_condition` (`cold|warm|unknown`), `cache_provenance`, `rubric_hash`, `revision_hash`. Digest/hash fields are lowercase SHA256. Other labels are bounded ASCII. Treatment values are frozen and must differ; each arm must match all manifest controls except precisely declared treatment fields.

Each run: `run_id`, `pair_id`, `task_id`, `family`, SHA256 `prompt_digest`, `host` (`claude|codex`), `arm` (`baseline|candidate`), `controls`, nonnegative `owner_prompt_count` and `intervention_count`, `expected_stages` list of unique stage IDs, `outcome` (`returned|failed|abandoned|unknown`), boolean `critical_false_negative`, `unauthorized_effect`, `stages`; optional boolean `external_state`, `quality_regression`. Exact run replay dedups; conflicting run ID rejects.

All objects reject unknown keys. Opaque IDs match `[A-Za-z0-9_-]{1,64}`, labels `[A-Za-z0-9_.-]{1,64}`, hashes lowercase 64-hex, counts 0..2^63-1 (no booleans), lists at most 10000; serialized evaluation input at most 4 MiB.

Each stage: unique per-run `stage_id`, `attempt_id`, `stage` (`coordinator|worker|judge|observer|hook|restore|continuation|unknown`) and `outcome` (`returned|failed|abandoned|unknown`), `failure_category` (`none|missing_connector|permission|interruption|substantive|unknown`), nonnegative `duration_ms`, explicit `adapter` (`claude-message|codex-turn|codex-cumulative` matching host), `stream_id`, `sequence`, `from_start`, `usage`, `missing`. For noncumulative adapters stream and sequence are null, from_start false. For cumulative events stream ID and sequence are required, from_start true only at sequence 0. `usage` has exactly these metrics: `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`, `reasoning_output_tokens`, `total_tokens`, each nonnegative integer or null. `missing` maps exactly null metric keys to `not_exposed|not_collected`. The evaluator normalizes events, validates replay before counting cumulative usage in run order, and marks unknown prefixes or gaps incomplete. A new stream requires its own baseline. Decreasing counters or conflicting stream attribution reject. Unknown counted metrics remain unknown in full subset totals.

Each acceptance registry item: opaque `task_id`, `run_id`, distinct `verifier_id` and `author_id`, `method`, `check_id`, SHA256 `revision_hash`, `rubric_hash`, `proof_digest`, nullable SHA256 `result_digest`, nonnegative `check_exit`, `check_status` (`PASS|FAIL|UNKNOWN`), literal `provenance:coordinator_attestation`; optional SHA256 `state_digest`, opaque `state_ref`, `observer_permission_ref` for external state. Successful independently executed acceptance requires exact run/revision/rubric binding, distinct verifier/author, result digest, exit 0 and PASS; external state additionally requires all three observer fields. One `check_id` belongs to one exact invocation item; exact replay dedups, conflicting reuse rejects even across runs. Repeated result digests across distinct invocation IDs are permitted. Registry entries are coordinator attestations, never host proof.

Output schema: `schema_version:1`, `decision:reject|inconclusive|screening_only`, `adopted:false`, `host_verified:false`, `evidence_kind:coordinator_attestation`, `scope:four_family_pilot_screening_only`, `by_host`, `pairs`. Each host/arm has attempted/accepted/rejected/unscorable, complete_usage, known_subtotal, nullable all_attempt_tokens/tokens_per_attempt/tokens_per_accepted, nullable cached_input_tokens/reasoning_output_tokens full subset totals, numeric known_cached_input_tokens/known_reasoning_output_tokens partial subtotals, stage_attempts, outcome_counts, failure_category_counts, missing_counts, incomplete_failure_attribution, failure_attribution_missing_counts. `pairs[host]` has matched, per-pair candidate-minus-baseline differences, cold/warm cache strata. Unknown stage `outcome` or `failure_category` makes the run incomplete for efficiency without changing independent acceptance. `incomplete_failure_attribution` counts affected deduplicated runs once each. `failure_attribution_missing_counts` has exactly `unknown_stage_outcome` and `unknown_failure_category`, counting observed stages with each unknown attribute. Both reasons can count on one stage. These counts do not invent missing stages; expected inventory still independently gates completeness. Absent required keys, null or invalid attribution values reject input. Known failed/abandoned stage usage stays in denominators. `complete_usage` now requires attribution as well as inventory and billable coverage. An affected arm retains known token/subset subtotals and acceptance counts but all_attempt_tokens/tokens_per_attempt/tokens_per_accepted stay null, affected pairs are excluded, and screening cannot pass. Known critical/quality regressions still reject. tokens_per_accepted is also null with zero acceptance; critical failures reject. `screening_only` is bounded pilot evidence, never adoption.

CLI: `python3 -B bin/router evaluate --file PATH`, or directly `python3 -B hooks/router/cmd_evaluate.py --file PATH` or `--file -` for stdin. Valid input emits one sorted JSON report and exits 0. Invalid input emits fixed `router evaluate: rejected input` on stderr and exits 1 without echoing input. Argument syntax errors exit 2. Only regular files or bounded stdin are read; invalid UTF-8, deep JSON, oversize input and FIFO reject. `sys.dont_write_bytecode=True` is set before imports, including direct execution without -B or its environment equivalent.


Missing expected stages increment missing metric counts even when no event was
observed. `missing_expected_stages` counts those absent inventory entries;
`complete_stage_coverage` counts attempted runs with complete expected inventory
and cumulative stream coverage. Full cache/reasoning subset totals require that
coverage across every attempted run; the separately labelled known subtotals
remain available.
