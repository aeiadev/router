"""Offline, synthetic evaluation contract tests."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from _platform import home_env, run, skip_unless_posix_modes
from test_bytecode import interpreter_prefix, written_bytecode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/router"))
import evaluation

H = "a" * 64
J = "b" * 64
K = "c" * 64


def fixture(hosts=("claude", "codex")):
    controls = dict(host_version="v1", model="m1", effort="low", hook_digest=H,
                    skill_digest=H, permission_digest=H, config_digest=H,
                    cache_condition="cold", cache_provenance="declared", rubric_hash=J,
                    revision_hash=H)
    manifest = dict(schema_version=1, experiment_id="experiment", candidate_id="candidate",
                    seed=7, treatment={"revision_hash": {"baseline": H, "candidate": K}},
                    controls={"claude": copy.deepcopy(controls), "codex": copy.deepcopy(controls)},
                    pairs=[dict(pair_id="p1", task_id="task", family="tinyanswer",
                                prompt_digest=H, rubric_hash=J, order="baseline-first", seed=7)])
    runs, evidence = [], []
    for host in hosts:
        for arm in ("baseline", "candidate"):
            run_id = host + "_" + arm
            revision = H if arm == "baseline" else K
            run = dict(run_id=run_id, pair_id="p1", task_id="task", family="tinyanswer",
                       prompt_digest=H, host=host, arm=arm,
                       controls={**controls, "revision_hash": revision},
                       owner_prompt_count=1, intervention_count=0,
                       expected_stages=["s1"], outcome="returned", critical_false_negative=False,
                       unauthorized_effect=False, stages=[dict(stage_id="s1", attempt_id="a1",
                           stage="worker", outcome="returned", failure_category="none", duration_ms=10,
                           adapter="claude-message" if host == "claude" else "codex-turn",
                           stream_id=None, sequence=None, from_start=False,
                           usage=dict(input_tokens=10 if arm == "baseline" else 5,
                                      output_tokens=2, cached_input_tokens=0,
                                      cache_creation_input_tokens=0,
                                      reasoning_output_tokens=0, total_tokens=12 if arm == "baseline" else 7),
                           missing={})])
            runs.append(run)
            evidence.append(dict(task_id="task", run_id=run_id, revision_hash=revision,
                                 rubric_hash=J, verifier_id="verifier", author_id="author",
                                 proof_digest=H, method="deterministic_bar", check_id=run_id,
                                 result_digest=K, check_exit=0, check_status="PASS",
                                 provenance="coordinator_attestation"))
    return dict(manifest=manifest, runs=runs, acceptance=evidence)


def four_family_fixture():
    data = fixture()
    for index, family in enumerate(("singleedit", "retryfix", "fakeoperations"), 2):
        extra = fixture()
        extra["manifest"]["pairs"][0].update(pair_id="p" + str(index),
                                               task_id="task" + str(index), family=family)
        for run in extra["runs"]:
            run["pair_id"] = "p" + str(index)
            run["task_id"] = "task" + str(index)
            run["family"] = family
            run["run_id"] += str(index)
        for evidence in extra["acceptance"]:
            evidence["task_id"] = "task" + str(index)
            evidence["run_id"] += str(index)
            evidence["check_id"] += str(index)
        data["manifest"]["pairs"].extend(extra["manifest"]["pairs"])
        data["runs"].extend(extra["runs"])
        data["acceptance"].extend(extra["acceptance"])
    return data


class EvaluationTests(unittest.TestCase):
    def test_cli_argument_errors_are_bounded_without_input_echo(self):
        marker = "PRIVATE_ARGUMENT_" + "x" * 12000
        command = [sys.executable, "-B", str(Path(evaluation.__file__).with_name("cmd_evaluate.py")), "--file", "-", marker]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertLess(len(result.stderr), 512)
        self.assertNotIn(b"PRIVATE_ARGUMENT", result.stderr)

    def test_unobserved_stages_keep_subset_totals_unknown(self):
        for host in ("claude", "codex"):
            for empty in (False, True):
                with self.subTest(host=host, empty=empty):
                    data = fixture()
                    run = next(r for r in data["runs"] if r["host"] == host and r["arm"] == "baseline")
                    if empty:
                        run["stages"] = []
                    else:
                        run["expected_stages"].append("unobserved")
                    report = evaluation.evaluate(data)["by_host"][host]["baseline"]
                    self.assertIsNone(report["cached_input_tokens"])
                    self.assertIsNone(report["reasoning_output_tokens"])
                    self.assertEqual(report["missing_counts"]["cached_input_tokens"], 1)
                    self.assertEqual(report["missing_counts"]["reasoning_output_tokens"], 1)
                    self.assertIsNone(report["tokens_per_accepted"])
                    self.assertEqual(report["accepted"], 1)

    def assert_unknown_attribution(self, field):
        data = four_family_fixture()
        for record in data["runs"]:
            if record["arm"] == "candidate":
                record["stages"][0][field] = "unknown"
        # Exact replay must not multiply missing attributes or acceptance.
        data["runs"].append(copy.deepcopy(data["runs"][1]))
        report = evaluation.evaluate(data)
        self.assertEqual(report["decision"], "inconclusive")
        for host in ("claude", "codex"):
            candidate = report["by_host"][host]["candidate"]
            self.assertEqual(candidate["accepted"], 4)
            self.assertEqual(candidate["attempted"], 4)
            self.assertEqual(candidate["known_subtotal"], 28)
            self.assertEqual(candidate["complete_usage"], 0)
            self.assertEqual(candidate["incomplete_failure_attribution"], 4)
            self.assertEqual(candidate["failure_attribution_missing_counts"], {
                "unknown_stage_outcome": 4 if field == "outcome" else 0,
                "unknown_failure_category": 4 if field == "failure_category" else 0})
            for metric in ("all_attempt_tokens", "tokens_per_attempt", "tokens_per_accepted"):
                self.assertIsNone(candidate[metric])
            self.assertEqual(report["pairs"][host]["matched"], 0)
            self.assertEqual(report["by_host"][host]["baseline"]["all_attempt_tokens"], 48)
        self.assertFalse(report["adopted"])
        self.assertFalse(report["host_verified"])

    def test_unknown_stage_outcome_blocks_efficiency(self):
        self.assert_unknown_attribution("outcome")

    def test_unknown_failure_category_blocks_efficiency(self):
        self.assert_unknown_attribution("failure_category")

    def test_failure_attribution_boundaries(self):
        data = four_family_fixture()
        data["runs"][0]["stages"][0].update(outcome="unknown", failure_category="unknown")
        report = evaluation.evaluate(data)
        baseline = report["by_host"]["claude"]["baseline"]
        self.assertEqual(report["decision"], "inconclusive")
        self.assertEqual(baseline["incomplete_failure_attribution"], 1)
        self.assertEqual(baseline["failure_attribution_missing_counts"], {
            "unknown_stage_outcome": 1, "unknown_failure_category": 1})
        self.assertEqual(baseline["known_subtotal"], 48)
        self.assertEqual(baseline["accepted"], 4)
        self.assertEqual(report["pairs"]["claude"]["matched"], 3)
        self.assertEqual(report["pairs"]["codex"]["matched"], 4)
        for field in ("outcome", "failure_category"):
            for value in (None, [], "private/input"):
                broken = fixture()
                broken["runs"][0]["stages"][0][field] = value
                with self.assertRaises(evaluation.EvaluationError):
                    evaluation.evaluate(broken)
            broken = fixture()
            del broken["runs"][0]["stages"][0][field]
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.evaluate(broken)
        for outcome in ("failed", "abandoned"):
            known = four_family_fixture()
            known["runs"][1]["stages"][0].update(outcome=outcome, failure_category="substantive")
            report = evaluation.evaluate(known)
            self.assertEqual(report["decision"], "screening_only")
            self.assertEqual(report["by_host"]["claude"]["candidate"]["incomplete_failure_attribution"], 0)

    def test_codex_missing_subsets_stay_unknown(self):
        data = fixture()
        stage = data["runs"][2]["stages"][0]
        for metric in ("cached_input_tokens", "reasoning_output_tokens"):
            stage["usage"][metric] = None
            stage["missing"][metric] = "not_exposed"
        report = evaluation.evaluate(data)["by_host"]["codex"]["baseline"]
        self.assertIsNone(report["cached_input_tokens"])
        self.assertIsNone(report["reasoning_output_tokens"])
        self.assertEqual(report["known_cached_input_tokens"], 0)
        self.assertEqual(report["missing_counts"]["cached_input_tokens"], 1)
        self.assertEqual(report["missing_counts"]["reasoning_output_tokens"], 1)

    def test_cumulative_delta_gap_and_context(self):
        data = fixture()
        run = data["runs"][2]
        first = run["stages"][0]
        first.update(adapter="codex-cumulative", stream_id="stream", sequence=0, from_start=True)
        second = copy.deepcopy(first)
        second.update(stage_id="s2", sequence=1, from_start=False)
        second["usage"].update(input_tokens=13, output_tokens=3, total_tokens=16)
        run["stages"].append(second)
        run["expected_stages"].append("s2")
        report = evaluation.evaluate(data)["by_host"]["codex"]["baseline"]
        self.assertEqual(report["all_attempt_tokens"], 16)
        data["runs"][2]["stages"][1]["sequence"] = 2
        report = evaluation.evaluate(data)["by_host"]["codex"]["baseline"]
        self.assertIsNone(report["all_attempt_tokens"])
        data["runs"][2]["stages"][0]["sequence"] = 4
        data["runs"][2]["stages"][0]["from_start"] = False
        data["runs"][2]["stages"][1]["sequence"] = 5
        report = evaluation.evaluate(data)["by_host"]["codex"]["baseline"]
        self.assertIsNone(report["all_attempt_tokens"])
        data["runs"][2]["stages"][0]["sequence"] = 0
        data["runs"][2]["stages"][0]["from_start"] = True
        data["runs"][2]["stages"][1]["stream_id"] = "new_stream"
        data["runs"][2]["stages"][1]["sequence"] = 0
        data["runs"][2]["stages"][1]["from_start"] = True
        report = evaluation.evaluate(data)["by_host"]["codex"]["baseline"]
        self.assertEqual(report["all_attempt_tokens"], 28)
        data["runs"][2]["stages"][1]["stream_id"] = "stream"
        data["runs"][2]["stages"][1]["from_start"] = False
        data["runs"][2]["stages"][1]["sequence"] = 1
        data["runs"][2]["stages"][1]["usage"]["input_tokens"] = 9
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(data)
        data = fixture()
        del data["runs"][2]["stages"][0]["adapter"]
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(data)

    def test_acceptance_check_id_is_unique_to_invocation(self):
        data = four_family_fixture()
        for item in data["acceptance"]:
            item["check_id"] = "reused"
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(data)
        data = fixture()
        data["acceptance"].append(copy.deepcopy(data["acceptance"][0]))
        self.assertEqual(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["accepted"], 1)

    def test_per_host_and_pairs(self):
        report = evaluation.evaluate(four_family_fixture())
        self.assertEqual(report["decision"], "screening_only")
        self.assertEqual(report["by_host"]["claude"]["baseline"]["all_attempt_tokens"], 48)
        self.assertEqual(report["by_host"]["codex"]["candidate"]["tokens_per_accepted"], 7)
        self.assertEqual(report["by_host"]["claude"]["candidate"]["cached_input_tokens"], 0)
        self.assertEqual(report["pairs"]["claude"]["matched"], 4)
        self.assertEqual(evaluation.evaluate(fixture())["decision"], "inconclusive")

    def test_missing_and_zero_acceptance_are_inconclusive(self):
        data = fixture()
        data["runs"][0]["expected_stages"].append("s2")
        data["runs"][1]["stages"][0]["usage"]["input_tokens"] = None
        data["runs"][1]["stages"][0]["missing"] = {"input_tokens": "not_collected"}
        data["runs"][1]["stages"][0]["usage"]["total_tokens"] = None
        data["runs"][1]["stages"][0]["missing"]["total_tokens"] = "not_collected"
        report = evaluation.evaluate(data)
        self.assertEqual(report["decision"], "inconclusive")
        self.assertIsNone(report["by_host"]["claude"]["baseline"]["tokens_per_accepted"])
        self.assertGreater(report["by_host"]["claude"]["candidate"]["missing_counts"]["input_tokens"], 0)
        data = fixture()
        data["acceptance"] = []
        self.assertEqual(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["accepted"], 0)
        self.assertIsNone(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["tokens_per_accepted"])
        self.assertEqual(evaluation.evaluate(fixture(("claude",)))["decision"], "inconclusive")

    def test_failure_abandonment_and_replay(self):
        data = fixture()
        run = copy.deepcopy(data["runs"][0])
        run["run_id"] = "failure"
        run["outcome"] = "abandoned"
        run["stages"][0]["outcome"] = "failed"
        run["stages"][0]["failure_category"] = "substantive"
        data["runs"].extend([run, copy.deepcopy(run)])
        report = evaluation.evaluate(data)
        self.assertEqual(report["by_host"]["claude"]["baseline"]["attempted"], 2)
        self.assertEqual(report["by_host"]["claude"]["baseline"]["all_attempt_tokens"], 24)
        self.assertEqual(report["by_host"]["claude"]["baseline"]["outcome_counts"]["abandoned"], 1)
        self.assertEqual(report["by_host"]["claude"]["baseline"]["failure_category_counts"]["substantive"], 1)
        data["runs"][-1]["stages"][0]["duration_ms"] = 11
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(data)

    def test_acceptance_is_independent_and_bound(self):
        for change in ({"check_status": "FAIL"}, {"check_exit": 1}, {"revision_hash": K},
                       {"rubric_hash": H}, {"result_digest": None}, {"verifier_id": "author"}):
            data = fixture()
            data["acceptance"][0].update(change)
            report = evaluation.evaluate(data)
            self.assertEqual(report["by_host"]["claude"]["baseline"]["accepted"], 0)
        data = fixture()
        data["acceptance"][0]["provenance"] = "host_observed"
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(data)

    def test_controls_prompt_and_owner(self):
        for field, value in (("model", "m2"), ("cache_condition", "unknown")):
            data = fixture()
            data["runs"][1]["controls"][field] = value
            self.assertEqual(evaluation.evaluate(data)["decision"], "inconclusive")
        data = fixture()
        data["runs"][1]["prompt_digest"] = K
        self.assertEqual(evaluation.evaluate(data)["decision"], "inconclusive")
        data = fixture()
        data["runs"][1]["owner_prompt_count"] = 2
        self.assertEqual(evaluation.evaluate(data)["decision"], "reject")
        data = fixture()
        data["runs"][1]["critical_false_negative"] = True
        self.assertEqual(evaluation.evaluate(data)["decision"], "reject")
        data = fixture()
        data["runs"][1]["quality_regression"] = True
        self.assertEqual(evaluation.evaluate(data)["decision"], "reject")

    def test_exact_treatment_external_state_and_null_usage(self):
        data = four_family_fixture()
        data["manifest"]["treatment"]["model"] = {"baseline": "m1", "candidate": "m2"}
        for run in data["runs"]:
            if run["arm"] == "candidate":
                run["controls"]["model"] = "m2"
        self.assertEqual(evaluation.evaluate(data)["decision"], "screening_only")
        data = four_family_fixture()
        data["runs"][1]["controls"]["revision_hash"] = H
        self.assertEqual(evaluation.evaluate(data)["decision"], "reject")
        data = fixture()
        data["runs"][0]["external_state"] = True
        self.assertEqual(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["accepted"], 0)
        data["acceptance"][0].update(state_digest=H, state_ref="readback",
                                     observer_permission_ref="permission")
        self.assertEqual(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["accepted"], 1)
        data = fixture()
        data["runs"][0]["stages"][0]["usage"] = {key: None for key in data["runs"][0]["stages"][0]["usage"]}
        data["runs"][0]["stages"][0]["missing"] = {key: "not_collected" for key in data["runs"][0]["stages"][0]["usage"]}
        report = evaluation.evaluate(data)
        self.assertEqual(report["decision"], "inconclusive")
        self.assertIsNone(report["by_host"]["claude"]["baseline"]["all_attempt_tokens"])
        data = fixture()
        stage = data["runs"][0]["stages"][0]
        stage["usage"]["input_tokens"] = None
        stage["missing"] = {"input_tokens": "not_collected"}
        self.assertIsNone(evaluation.evaluate(data)["by_host"]["claude"]["baseline"]["all_attempt_tokens"])

    def test_cli_and_bad_inputs(self):
        cli = Path(evaluation.__file__).with_name("cmd_evaluate.py")
        good = run([sys.executable, "-B", str(cli), "--file", "-"], timeout=15,
                              input=json.dumps(four_family_fixture()).encode(), capture_output=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)["decision"], "screening_only")
        bad = run([sys.executable, "-B", str(cli), "--file", "-"], timeout=15,
                             input=b'{"secret":"private"}', capture_output=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn(b"private", bad.stderr)

    def test_cli_help_discovery(self):
        router = Path(evaluation.__file__).resolve().parents[2] / "bin/router"
        result = run([sys.executable, "-B", str(router), "--help"], timeout=15,
                     stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"evaluate --file PATH|-", result.stdout)

    def test_cli_disables_bytecode_without_environment_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("cmd_evaluate.py", "evaluation.py", "usage.py", "compat.py"):
                shutil.copyfile(Path(evaluation.__file__).with_name(name), root / name)
            env = {**os.environ, **home_env(root / "home")}
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            prefix = interpreter_prefix(env, root)
            before = written_bytecode(root, prefix)
            result = run([sys.executable, str(root / "cmd_evaluate.py"), "--file", "-"],
                         timeout=15, input=json.dumps(fixture()).encode(), capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(written_bytecode(root, prefix), before)
            self.assertEqual(before, set())

    def test_cli_input_boundaries(self):
        cli = Path(evaluation.__file__).with_name("cmd_evaluate.py")
        for payload in (b"\xff", b"[" * 2000, b" " * (4 * 1024 * 1024 + 1), b"[]"):
            result = run([sys.executable, "-B", str(cli), "--file", "-"], timeout=15,
                         input=payload, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(result.stderr, (b"router evaluate: rejected input\n",
                                          b"router evaluate: rejected input\r\n"))
            self.assertEqual(result.stdout, b"")

    @skip_unless_posix_modes("FIFO input rejection")
    def test_cli_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / "input"
            os.mkfifo(fifo)
            result = run([sys.executable, "-B", str(Path(evaluation.__file__).with_name("cmd_evaluate.py")),
                          "--file", str(fifo)], timeout=15,
                         stdin=subprocess.DEVNULL, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(result.stderr, (b"router evaluate: rejected input\n",
                                          b"router evaluate: rejected input\r\n"))
            self.assertEqual(result.stdout, b"")

    def test_nested_type_errors_are_bounded(self):
        for edit in (lambda d: d["runs"][0].update(family=[]),
                     lambda d: d["runs"][0].update(host=[]),
                     lambda d: d["runs"][0]["stages"][0].update(stage=[]),
                     lambda d: d["manifest"]["pairs"][0].update(family=[])):
            data = fixture()
            edit(data)
            with self.assertRaises(evaluation.EvaluationError) as raised:
                evaluation.evaluate(data)
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
