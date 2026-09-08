import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "tests/registry"


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("weaver"):
            raise RuntimeError("Install the pinned Weaver; an unexecuted integration suite cannot pass.")

    def setUp(self):
        scratch = ROOT / ".test-output"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)

    def invoke(self, mode="check", *extra, env=None):
        command = [sys.executable, str(ROOT / "otel-check"), mode,
                   "--registry", str(REGISTRY), "--output", str(self.cwd / "report"),
                   "--startup-timeout", "5", "--timeout", "10", *extra]
        result = subprocess.run(command, cwd=self.cwd, text=True, capture_output=True,
                                timeout=45, env=env)
        summary = json.loads((self.cwd / "report/summary.json").read_text())
        self.assertEqual(result.returncode, summary["exit_code"], result.stderr)
        return result, summary

    def sample(self, value):
        file = self.cwd / "telemetry.json"
        file.write_text(json.dumps(value))
        return str(file)

    def test_valid_and_invalid_types(self):
        good, report = self.invoke("check", str(ROOT / "examples/valid.json"))
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertEqual(report["state"], "passed")
        self.assertEqual(report["layers"]["syntax"]["status"], "PASS")
        self.assertEqual(report["layers"]["behavior"]["status"], "NOT_CHECKED")
        self.assertEqual(report["layers"]["compatibility"]["status"], "NOT_CHECKED")
        self.assertFalse(report["fully_evaluated"])
        self.assertGreater(report["entities"], 0)
        bad, report = self.invoke("check", str(ROOT / "examples/invalid.json"))
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertEqual(report["layers"]["syntax"]["status"], "FAIL")
        details = json.loads(Path(report["report_path"]).read_text())
        self.assertGreater(details["statistics"]["advice_type_counts"]["type_mismatch"], 0)

    def test_missing_empty_and_wrong_format_fail_closed(self):
        for sample in (None, [], {}, [{"nonsense": {}}], [{"span": {}}]):
            with self.subTest(sample=sample):
                source = str(self.cwd / "absent.json") if sample is None else self.sample(sample)
                result, report = self.invoke("check", source)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(report["state"], "passed")
                self.assertEqual(report["layers"]["syntax"]["status"], "FAIL")
                self.assertEqual(report["layers"]["semantics"]["status"], "NOT_CHECKED")

    def test_invalid_json(self):
        path = self.cwd / "invalid.json"
        path.write_text("{broken")
        result, report = self.invoke("check", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(report["report_path"])

    def test_ambient_config_cannot_disable_checks(self):
        (self.cwd / ".weaver.toml").write_text('[live_check]\nfail_on = "none"\nno_stats = true\n')
        result, _ = self.invoke("check", str(ROOT / "examples/invalid.json"))
        self.assertEqual(result.returncode, 1)

    def test_unstable_attribute_threshold(self):
        path = self.sample([{"attribute": {"name": "test.experimental", "value": "value"}}])
        normal, _ = self.invoke("check", path)
        strict, report = self.invoke("check", "--fail-on", "information", path)
        self.assertEqual(normal.returncode, 0)
        self.assertEqual(strict.returncode, 1)
        self.assertTrue(report["findings"])
        self.assertEqual(report["layers"]["stability"]["status"], "WARN")
        self.assertEqual(report["layers"]["syntax"]["status"], "PASS")

    def test_declared_type_cannot_hide_wrong_value(self):
        for value in ("200", {"code": 200}, [200, "bad"]):
            with self.subTest(value=value):
                path = self.sample([{"attribute": {"name": "http.response.status_code", "type": "int", "value": value}}])
                result, report = self.invoke("check", path)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(report["layers"]["syntax"]["status"], "FAIL")
                self.assertTrue(any(f["id"] == "syntax.attribute_value" for f in report["layers"]["syntax"]["findings"]))

    def test_primitive_values_respect_nulls_and_integer_bounds(self):
        for value, expected in ((None, "PASS"), (9223372036854775807, "PASS"),
                                (9223372036854775808, "FAIL"), (-9223372036854775808, "PASS"),
                                ({"code": 200}, "FAIL")):
            path = self.sample([{"attribute": {"name": "http.response.status_code", "value": value}}])
            result, report = self.invoke("check", path)
            self.assertEqual(report["layers"]["syntax"]["status"], expected, result.stdout + result.stderr)
        # General AnyValue maps are legal; a primitive registry type is what constrains them.
        path = self.sample([{"attribute": {"name": "custom.payload", "value": {"nested": [1, "two", None]}}}])
        result, report = self.invoke("check", path)
        self.assertEqual(report["layers"]["syntax"]["status"], "PASS", result.stdout + result.stderr)
        path = self.sample([{"attribute": {"name": "custom.labels", "type": "string[]", "value": ["one", None]}}])
        result, report = self.invoke("check", path)
        self.assertEqual(report["layers"]["syntax"]["status"], "PASS", result.stdout + result.stderr)

    def http_span(self, status="unset", code=200, error_type=None):
        attrs = [{"name": "http.request.method", "value": "GET"},
                 {"name": "http.response.status_code", "value": code}]
        if error_type is not None:
            attrs.append({"name": "error.type", "value": error_type})
        return {"span": {"name": "GET", "kind": "server", "status": {"code": status, "message": ""}, "attributes": attrs}}

    def test_http_behavior_distinct_from_syntax(self):
        for status, code, error_type, expected in (
                ("unset", 200, None, "PASS"), ("ok", 200, None, "FAIL"),
                ("error", 500, None, "FAIL"), ("error", 500, "500", "PASS"),
                ("error", 200, "connection_reset", "PASS"), ("unset", 404, None, "PASS")):
            with self.subTest(status=status, code=code, error_type=error_type):
                result, report = self.invoke("check", self.sample([self.http_span(status, code, error_type)]))
                self.assertEqual(report["layers"]["syntax"]["status"], "PASS", result.stdout + result.stderr)
                self.assertEqual(report["layers"]["behavior"]["status"], expected)
                self.assertEqual(result.returncode, 1 if expected == "FAIL" else 0, result.stdout + result.stderr)

    def test_unresolved_route_leaves_the_framework_span_name(self):
        """A route that never resolved leaves the hosting layer's own operation name on the span."""
        for name, expected in (("Microsoft.AspNetCore.Hosting.HttpRequestIn", "FAIL"),
                               ("GET /healthz", "PASS")):
            with self.subTest(name=name):
                span = self.http_span()
                span["span"]["name"] = name
                result, report = self.invoke("check", self.sample([span]))
                self.assertEqual(report["layers"]["behavior"]["status"], expected,
                                 result.stdout + result.stderr)
                self.assertEqual([f["id"] for f in report["layers"]["behavior"]["findings"]],
                                 ["behavior.span_name.unresolved_route"] if expected == "FAIL" else [])

    def test_unredacted_query_reports_the_key_and_never_the_value(self):
        """Redaction on one copy of a request does not reach a copy emitted by another source."""
        for url, expected in (("https://example.test/api?token=s3cr3t", "FAIL"),
                              ("https://example.test/api", "PASS")):
            with self.subTest(url=url):
                span = self.http_span()
                span["span"]["kind"] = "client"
                span["span"]["attributes"].append({"name": "url.full", "value": url})
                result, report = self.invoke("check", self.sample([span]))
                findings = report["layers"]["behavior"]["findings"]
                self.assertEqual(report["layers"]["behavior"]["status"], expected,
                                 result.stdout + result.stderr)
                self.assertEqual([f["id"] for f in findings],
                                 ["behavior.attribute.unredacted_query"] if expected == "FAIL" else [])
                # A finding names the key it is about, never the value: the report travels, the
                # secret must not travel with it.
                self.assertNotIn("s3cr3t", json.dumps(findings))

    def test_required_layer_needs_evidence(self):
        for layer in ("behavior", "compatibility"):
            result, report = self.invoke("check", "--require", layer, str(ROOT / "examples/valid.json"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report["state"], "incomplete")
            self.assertIn(layer, report["unassessed_layers"])

    def test_layers_report_independent_results_with_finding_evidence(self):
        span = self.http_span()
        span["span"]["attributes"] = [{"name": "http.response.status_code", "value": 200},
                                       {"name": "test.experimental", "value": "value"}]
        result, report = self.invoke("check", "--baseline-registry", str(REGISTRY), self.sample([span]))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual({name: layer["status"] for name, layer in report["layers"].items()},
                         {"syntax": "PASS", "semantics": "FAIL", "behavior": "PASS",
                          "stability": "WARN", "compatibility": "PASS"})
        finding = report["layers"]["semantics"]["findings"][0]
        evidence = json.loads(Path(report["report_path"]).read_text())
        for part in finding["evidence_path"].split('/')[1:]:
            evidence = evidence[int(part)] if isinstance(evidence, list) else evidence[part]
        self.assertEqual(evidence["id"], finding["id"])
        self.assertEqual(evidence["message"], finding["message"])

    def test_stability_of_unknown_names_is_not_a_pass(self):
        result, report = self.invoke("check", self.sample([{"attribute": {"name": "unknown.attribute", "value": "v"}}]))
        self.assertEqual(report["layers"]["stability"]["status"], "NOT_CHECKED", result.stdout + result.stderr)

    def test_additive_custom_policy_and_invalid_policy_fail_closed(self):
        policy = self.cwd / "service.rego"
        policy.write_text('''package live_check_advice
import rego.v1
deny contains {"id": "semantics.service_name", "level": "violation", "message": "Rejected service name."} if {
    input.sample.attribute.name == "service.name"
    input.sample.attribute.value == "orders"
}
''')
        result, report = self.invoke("check", "--policy", str(policy), str(ROOT / "examples/valid.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(report["layers"]["semantics"]["status"], "FAIL")
        # Adding a policy cannot disable upstream naming checks or bundled type checks.
        path = self.sample([{"attribute": {"name": "BAD.Name", "value": "value"}},
                            {"attribute": {"name": "http.response.status_code", "type": "int", "value": "bad"}}])
        result, report = self.invoke("check", "--policy", str(policy), path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid_format", [f["id"] for f in report["layers"]["semantics"]["findings"]])
        self.assertEqual(report["layers"]["syntax"]["status"], "FAIL")
        policy.write_text('package invalid\nthis is not rego')
        result, report = self.invoke("check", "--policy", str(policy), str(ROOT / "examples/valid.json"))
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(report["layers"]["semantics"]["status"], "PASS")

    def test_registry_compatibility_real_comparison(self):
        candidate = self.cwd / "candidate"
        shutil.copytree(REGISTRY, candidate)
        source = (candidate / "attributes.yaml").read_text()
        for edit, expected, finding in (
                (source, "PASS", None),
                (source.replace('type: int', 'type: string').replace('examples: [200]', 'examples: ["200"]'), "FAIL", "compatibility.attribute_type"),
                (source.replace('id: service.name', 'id: service.renamed'), "FAIL", "compatibility.attribute_removed"),
                (source.replace('stability: stable', 'stability: development', 1), "FAIL", "compatibility.attribute_stability"),
                (source.replace('id: test.experimental', 'id: test.new_experimental'), "PASS", None)):
            with self.subTest(expected=expected, finding=finding):
                (candidate / "attributes.yaml").write_text(edit)
                path = self.sample([{"attribute": {"name": "telemetry.sdk.name", "value": "opentelemetry"}}])
                result, report = self.invoke("check", "--registry", str(candidate), "--baseline-registry", str(REGISTRY), path)
                self.assertEqual(report["layers"]["compatibility"]["status"], expected, result.stdout + result.stderr + str(report))
                self.assertEqual(result.returncode, 1 if expected == "FAIL" else 0)
                if finding:
                    self.assertIn(finding, [f["id"] for f in report["layers"]["compatibility"]["findings"]])

    def test_invalid_baseline_is_error(self):
        result, report = self.invoke("check", "--baseline-registry", str(self.cwd / "absent"), str(ROOT / "examples/valid.json"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(report["layers"]["compatibility"]["status"], "ERROR")

    def test_stable_metric_evolution(self):
        baseline = self.cwd / "baseline"
        candidate = self.cwd / "candidate"
        shutil.copytree(REGISTRY, baseline)
        metric = '''groups:
  - id: metric.test.duration
    type: metric
    metric_name: test.duration
    brief: Test metric.
    stability: stable
    instrument: histogram
    unit: s
'''
        (baseline / "metric.yaml").write_text(metric)
        shutil.copytree(baseline, candidate)
        for replacement, expected in ((metric, "PASS"), (metric.replace('unit: s', 'unit: ms'), "FAIL"),
                                      (metric.replace('instrument: histogram', 'instrument: counter'), "FAIL"),
                                      (metric.replace('metric_name: test.duration', 'metric_name: test.changed'), "FAIL")):
            (candidate / "metric.yaml").write_text(replacement)
            result, report = self.invoke("check", "--registry", str(candidate), "--baseline-registry", str(baseline),
                                         str(ROOT / "examples/valid.json"))
            self.assertEqual(report["layers"]["compatibility"]["status"], expected, result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0 if expected == "PASS" else 1)

    def test_enum_extension_is_compatible_but_removal_is_not(self):
        baseline = self.cwd / "baseline"
        candidate = self.cwd / "candidate"
        shutil.copytree(REGISTRY, baseline)
        enumeration = '''groups:
  - id: registry.enums
    type: attribute_group
    brief: Enum fixtures.
    attributes:
      - id: test.enum
        stability: stable
        brief: Enum fixture.
        type:
          members:
            - id: first
              value: one
              brief: First value.
'''
        (baseline / "enum.yaml").write_text(enumeration)
        shutil.copytree(baseline, candidate)
        for edited, expected in (
                (enumeration + '            - id: second\n              value: two\n              brief: Second value.\n', "PASS"),
                (enumeration.replace('First value.', 'Edited description.'), "PASS"),
                (enumeration.replace('value: one', 'value: changed'), "FAIL")):
            (candidate / "enum.yaml").write_text(edited)
            result, report = self.invoke("check", "--registry", str(candidate), "--baseline-registry", str(baseline),
                                         str(ROOT / "examples/valid.json"))
            self.assertEqual(report["layers"]["compatibility"]["status"], expected, result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0 if expected == "PASS" else 1)

    def test_custom_advice_data(self):
        for name, expected in (("orders", 0), ("unlisted", 1)):
            path = self.sample([{"attribute": {"name": "service.name", "value": name}}])
            result, report = self.invoke("check", "--policy", str(ROOT / "examples/policies/service.rego"),
                                         "--advice-data", str(ROOT / "examples/policy-data/*.json"), path)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertEqual(report["layers"]["semantics"]["status"], "FAIL" if expected else "PASS")

    def test_command_shorthand(self):
        result = subprocess.run([sys.executable, str(ROOT / "otel-check"), sys.executable, "-c", "raise SystemExit(0)"],
                                cwd=self.cwd, capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, WEAVER="missing"))
        report = json.loads((self.cwd / "otel-report/summary.json").read_text())
        self.assertEqual(report["mode"], "run")
        self.assertEqual(result.returncode, 2)

    def test_missing_engine(self):
        env = dict(os.environ, WEAVER="missing-weaver-for-test")
        result, report = self.invoke("check", str(ROOT / "examples/valid.json"), env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing", report["error"])

    @unittest.skipUnless(os.name == "posix", "Live mode is POSIX only.")
    def test_live_zero_telemetry_and_workload_failure(self):
        for code in (0, 7):
            result, report = self.invoke("run", "--", sys.executable, "-c", f"raise SystemExit({code})")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("No telemetry", report["error"])
            self.assertEqual(report["workload_exit_code"], code)

    @unittest.skipUnless(os.name == "posix", "Live mode is POSIX only.")
    def test_live_timeout(self):
        result, report = self.invoke("run", "--timeout", "0.2", "--",
                                     sys.executable, "-c", "import time; time.sleep(30)")
        self.assertEqual(result.returncode, 2)
        self.assertIn("timed out", report["error"])

    @unittest.skipUnless(os.name == "posix", "Live mode is POSIX only.")
    def test_live_otlp_and_failing_workload_with_telemetry(self):
        sender = (
            "import os,subprocess; "
            f"r=subprocess.call(['weaver','registry','emit','--registry',{str(REGISTRY)!r},"
            "'--endpoint',os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'],'--quiet']); "
            "raise SystemExit(r or EXIT_CODE)"
        )
        for exit_code in (0, 7):
            result, report = self.invoke("run", "--", sys.executable, "-c",
                                         sender.replace("EXIT_CODE", str(exit_code)))
            self.assertEqual(result.returncode, 1 if exit_code else 0, result.stdout + result.stderr)
            self.assertGreater(report["entities"], 0)
            self.assertEqual(report["workload_exit_code"], exit_code)

    def test_action_inputs_are_forwarded(self):
        github_output = self.cwd / "github-output"
        env = dict(os.environ, GITHUB_OUTPUT=str(github_output),
                   CHECK_FILE=str(ROOT / "examples/invalid.json"),
                   CHECK_REGISTRY=str(REGISTRY), CHECK_OUTPUT=str(self.cwd / "action-report"))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/action.py"), "check"],
                                cwd=self.cwd, env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("summary=", github_output.read_text())

    def test_action_layer_inputs_and_job_summary(self):
        env = dict(os.environ, GITHUB_OUTPUT=str(self.cwd / "outputs"),
                   GITHUB_STEP_SUMMARY=str(self.cwd / "step-summary"),
                   CHECK_FILE=str(ROOT / "examples/http-valid.json"), CHECK_REGISTRY=str(REGISTRY),
                   CHECK_BASELINE_REGISTRY=str(REGISTRY), CHECK_REQUIRE="behavior compatibility",
                   CHECK_POLICIES=str(ROOT / "examples/policies/service.rego"),
                   CHECK_ADVICE_DATA=str(ROOT / "examples/policy-data/*.json"),
                   CHECK_OUTPUT=str(self.cwd / "action-report"))
        command = [sys.executable, str(ROOT / "scripts/action.py"), "check"]
        result = subprocess.run(command, cwd=self.cwd, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((self.cwd / "action-report/summary.json").read_text())
        self.assertTrue(report["fully_evaluated"])
        self.assertIn('| compatibility | PASS |', (self.cwd / "step-summary").read_text())
        prior = (self.cwd / "step-summary").read_bytes()
        env["CHECK_REQUIRE"] = "invalid-layer"
        result = subprocess.run(command, cwd=self.cwd, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.cwd / "step-summary").read_bytes(), prior, "Do not display a stale passing report.")

    def test_status_needs_no_engine(self):
        result = subprocess.run([sys.executable, str(ROOT / "otel-check"), "status", "--json"],
                                text=True, capture_output=True, check=True,
                                env=dict(os.environ, WEAVER="missing"))
        meta = json.loads(result.stdout)
        self.assertEqual(len(meta["specifications"]), 5)
        self.assertEqual(meta["signals"][1]["sdk"], "mixed")


if __name__ == "__main__":
    unittest.main()
