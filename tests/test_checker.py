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


@unittest.skipUnless(shutil.which("weaver"), "Install the pinned Weaver to run integration tests.")
class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
        self.assertGreater(report["entities"], 0)
        bad, report = self.invoke("check", str(ROOT / "examples/invalid.json"))
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        details = json.loads(Path(report["report_path"]).read_text())
        self.assertGreater(details["statistics"]["advice_type_counts"]["type_mismatch"], 0)

    def test_missing_empty_and_wrong_format_fail_closed(self):
        for sample in (None, [], {}, [{"nonsense": {}}], [{"span": {}}]):
            with self.subTest(sample=sample):
                source = str(self.cwd / "absent.json") if sample is None else self.sample(sample)
                result, report = self.invoke("check", source)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(report["state"], "passed")

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

    def test_status_needs_no_engine(self):
        result = subprocess.run([sys.executable, str(ROOT / "otel-check"), "status", "--json"],
                                text=True, capture_output=True, check=True,
                                env=dict(os.environ, WEAVER="missing"))
        meta = json.loads(result.stdout)
        self.assertEqual(len(meta["specifications"]), 5)
        self.assertEqual(meta["signals"][1]["sdk"], "mixed")


if __name__ == "__main__":
    unittest.main()
