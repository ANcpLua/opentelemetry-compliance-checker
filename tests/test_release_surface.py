"""Arithmetic and classification of the release-surface commands, without a network.

Every network call is mocked. What these tests pin is the reasoning: that an
unfinished run is left unmeasured, that an annotated tag is dereferenced before
it is compared, and that "never published" is not "wrong version".
"""

import io
import json
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import otel_check  # noqa: E402
import release_surface as rs  # noqa: E402


def moment(text):
    return None if not text else datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def job(name, label, start, end):
    return {"name": name, "labels": [label], "conclusion": "success",
            "started_at": moment(start), "completed_at": moment(end)}


def qyl_verify():
    """Run 34157154158: 28.2 minutes wall clock of which 23 were queue. Reporting
    its 5:11 longest job as the workflow duration is the false statement this
    arithmetic exists to prevent."""
    return {"id": 34157154158, "name": "qyl-verify", "status": "completed", "conclusion": "success",
            "created_at": moment("2026-09-07T19:51:11Z"), "updated_at": moment("2026-09-07T20:19:24Z"),
            "jobs": [
                job("verify 2/4 (ubuntu-24.04-arm)", "ubuntu-24.04-arm", "2026-09-07T20:14:12Z", "2026-09-07T20:15:38Z"),
                job("verify 2/4 (macos-latest)", "macos-latest", "2026-09-07T20:14:16Z", "2026-09-07T20:15:26Z"),
                job("verify 1/4 (ubuntu-24.04-arm)", "ubuntu-24.04-arm", "2026-09-07T20:14:12Z", "2026-09-07T20:19:23Z"),
                job("verify 3/4 (ubuntu-24.04-arm)", "ubuntu-24.04-arm", "2026-09-07T20:14:12Z", "2026-09-07T20:15:22Z"),
                job("verify 3/4 (macos-latest)", "macos-latest", "2026-09-07T20:14:14Z", "2026-09-07T20:15:01Z"),
                job("verify 4/4 (ubuntu-24.04-arm)", "ubuntu-24.04-arm", "2026-09-07T20:14:12Z", "2026-09-07T20:15:36Z"),
                job("verify 1/4 (macos-latest)", "macos-latest", "2026-09-07T20:14:15Z", "2026-09-07T20:18:21Z"),
                job("verify 4/4 (macos-latest)", "macos-latest", "2026-09-07T20:14:14Z", "2026-09-07T20:15:30Z"),
            ]}


class TimingTests(unittest.TestCase):
    def test_wall_clock_compute_and_queue_are_three_numbers(self):
        split = rs.split_run(qyl_verify())
        self.assertEqual(rs.minutes(split["wall"]), "28.2")
        self.assertEqual(rs.mmss(split["compute"]), "5:11")
        self.assertEqual(rs.minutes(split["queue"]), "23.0")
        self.assertEqual(split["longest_job"], "verify 1/4 (ubuntu-24.04-arm)")
        self.assertEqual(split["cause"], "queue")
        self.assertEqual(rs.percent(rs.queue_share(split)), "82%")
        self.assertEqual(rs.percent(None), "unknown", "a share of an unmeasured wall clock is not a number")

    def test_compute_bound_run_names_compute_as_the_cause(self):
        # nuget-publish 34157155952: 26.9 minutes wall around a single 21:23 job.
        split = rs.split_run({"id": 1, "name": "nuget-publish", "status": "completed",
                              "created_at": moment("2026-09-07T19:51:13Z"),
                              "updated_at": moment("2026-09-07T20:18:06Z"),
                              "jobs": [job("pack", "ubuntu-latest", "2026-09-07T19:51:24Z", "2026-09-07T19:52:07Z"),
                                       job("verify 8/8", "ubuntu-latest", "2026-09-07T19:51:22Z", "2026-09-07T20:12:45Z"),
                                       job("release", "ubuntu-latest", "2026-09-07T20:18:01Z", "2026-09-07T20:18:05Z")]})
        self.assertEqual((rs.minutes(split["wall"]), rs.minutes(split["compute"]), rs.minutes(split["queue"])),
                         ("26.9", "21.4", "5.5"))
        self.assertEqual(split["cause"], "compute")

    def test_unfinished_run_keeps_no_wall_clock(self):
        # updated_at on a running workflow is the last touch, not the end: it yields a
        # wall clock shorter than the compute already spent. Reporting that number once
        # told a reader "no run over ten minutes" about a run still growing past it.
        split = rs.split_run({"id": 1, "status": "in_progress",
                              "created_at": moment("2026-09-08T02:02:50Z"),
                              "updated_at": moment("2026-09-08T02:03:02Z"),
                              "jobs": [job("pack", "ubuntu-latest", "2026-09-08T02:02:55Z", "2026-09-08T02:12:43Z")]})
        self.assertIsNone(split["wall"])
        self.assertIsNotNone(split["compute"], "a finished job is measurable while the run is not")
        self.assertIsNone(split["queue"], "no wall clock to subtract from")
        self.assertEqual(split["cause"], "unknown")
        self.assertEqual(rs.minutes_or(split["wall"]), "unknown")

    def test_nothing_is_guessed_and_a_negative_queue_is_clamped(self):
        unfinished_jobs = rs.split_run({"id": 1, "status": "completed",
                                        "created_at": moment("2026-09-07T19:51:11Z"),
                                        "updated_at": moment("2026-09-07T20:19:24Z"),
                                        "jobs": [{"name": "hung", "labels": ["ubuntu-latest"],
                                                  "started_at": moment("2026-09-07T19:51:16Z"),
                                                  "completed_at": None}]})
        self.assertIsNotNone(unfinished_jobs["wall"])
        self.assertIsNone(unfinished_jobs["compute"])
        self.assertIsNone(unfinished_jobs["queue"])
        clamped = rs.split_run({"id": 1, "status": "completed",
                                "created_at": moment("2026-09-07T19:51:11Z"),
                                "updated_at": moment("2026-09-07T19:52:11Z"),
                                "jobs": [job("long", "ubuntu-latest", "2026-09-07T19:51:11Z", "2026-09-07T19:55:11Z")]})
        self.assertEqual(clamped["queue"], 0.0)

    def test_delays_separate_runner_classes(self):
        # qyl-smoketest 34157154176: ubuntu started after 5 seconds, macOS after ten
        # minutes. One number for "the run" would hide that.
        delays = rs.delays_by_label({"id": 1, "status": "completed",
                                     "created_at": moment("2026-09-07T19:51:11Z"),
                                     "updated_at": moment("2026-09-07T20:02:30Z"),
                                     "jobs": [job("smoke (ubuntu-24.04-arm)", "ubuntu-24.04-arm",
                                                  "2026-09-07T19:51:16Z", "2026-09-07T19:52:57Z"),
                                              job("smoke (macos-latest)", "macos-latest",
                                                  "2026-09-07T20:01:18Z", "2026-09-07T20:02:30Z")]})
        self.assertEqual([(d["label"], rs.minutes(d["min_delay"])) for d in delays],
                         [("macos-latest", "10.1"), ("ubuntu-24.04-arm", "0.1")])
        self.assertEqual((rs.worst(delays) or {}).get("label"), "macos-latest")
        self.assertEqual(rs.clock(delays[0]["first_start"]), "20:01:18Z")

    def test_a_job_that_never_started_reports_no_delay(self):
        delays = rs.delays_by_label({"id": 1, "status": "completed",
                                     "created_at": moment("2026-09-07T19:51:11Z"),
                                     "jobs": [{"name": "queued", "labels": ["macos-latest"],
                                               "started_at": None, "completed_at": None}]})
        self.assertEqual(len(delays), 1)
        self.assertIsNone(delays[0]["min_delay"])
        self.assertIsNone(rs.worst(delays))

    def test_labels_rank_by_first_start_not_last_start(self):
        # nuget-publish's ubuntu jobs run in a chain, so its LAST job starts 26.8
        # minutes in without any queue. Ranking by that would name ubuntu the slow
        # runner class, which is false.
        chained = {"id": 1, "name": "nuget-publish", "status": "completed",
                   "created_at": moment("2026-09-07T19:51:13Z"), "updated_at": moment("2026-09-07T20:18:06Z"),
                   "jobs": [job("version", "ubuntu-latest", "2026-09-07T19:51:15Z", "2026-09-07T19:51:20Z"),
                            job("release", "ubuntu-latest", "2026-09-07T20:18:01Z", "2026-09-07T20:18:05Z")]}
        queued = {"id": 2, "name": "qyl-smoketest", "status": "completed",
                  "created_at": moment("2026-09-07T19:51:11Z"),
                  "jobs": [job("smoke (macos-latest)", "macos-latest", "2026-09-07T20:01:18Z", "2026-09-07T20:02:30Z")]}
        spans = rs.spans_by_label([chained, queued])
        self.assertEqual(spans[0]["label"], "macos-latest")
        ubuntu = next(span for span in spans if span["label"] == "ubuntu-latest")
        self.assertEqual(rs.minutes(ubuntu["max_delay"]), "26.8", "the chained job")
        self.assertEqual(rs.minutes(ubuntu["min_delay"]), "0.0", "no queue at all")

    def test_runner_class_and_formatting(self):
        self.assertEqual({label: rs.runner_class(label) for label in
                          ("ubuntu-24.04-arm", "macos-latest", "windows-2022", "self-hosted", "")},
                         {"ubuntu-24.04-arm": "ubuntu", "macos-latest": "macos", "windows-2022": "windows",
                          "self-hosted": "self-hosted", "": "unknown"})
        self.assertEqual((rs.mmss(311), rs.minutes(1693), rs.clock(None)), ("5:11", "28.2", "unknown"))


# The real answer of repos/.../git/ref/tags/v16.0.0. The sha in it is the TAG
# OBJECT; comparing it against the branch head would say the tag misses main.
ANNOTATED_REF = json.dumps({"ref": "refs/tags/v16.0.0", "object": {
    "sha": "a089d4524dba2e7e3de83e552c986e2c09a3d1e1", "type": "tag"}})
ANNOTATED_TAG = json.dumps({"sha": "a089d4524dba2e7e3de83e552c986e2c09a3d1e1", "tag": "v16.0.0",
                            "message": "16.0.0: a second AddQyl with options is an error\n",
                            "tagger": {"name": "ancplua", "date": "2026-09-07T19:51:09Z"},
                            "object": {"sha": "bbf82ce6a02197104f221d03f6cae84bb580dff0", "type": "commit"}})


class TagTests(unittest.TestCase):
    def test_annotated_tag_is_dereferenced_to_its_commit(self):
        fetched = []
        resolved = rs.resolve_tag(ANNOTATED_REF, lambda sha: fetched.append(sha) or ANNOTATED_TAG)
        self.assertEqual(fetched, ["a089d4524dba2e7e3de83e552c986e2c09a3d1e1"])
        self.assertTrue(resolved["annotated"])
        self.assertEqual(resolved["commit"], "bbf82ce6a02197104f221d03f6cae84bb580dff0")
        self.assertNotEqual(resolved["commit"], "a089d4524dba2e7e3de83e552c986e2c09a3d1e1",
                            "the tag object was reported as the commit")
        self.assertEqual((resolved["tagger"], resolved["tagged_at"]), ("ancplua", "2026-09-07T19:51:09Z"))
        self.assertEqual(len(resolved["hops"]), 2)
        self.assertIn("bbf82ce6", resolved["hops"][1])

    def test_lightweight_tag_fetches_no_tag_object(self):
        def refuse(sha):
            self.fail("a lightweight tag must not fetch a tag object")
        ref = json.dumps({"ref": "refs/tags/v1.0.0",
                          "object": {"sha": "bbf82ce6a02197104f221d03f6cae84bb580dff0", "type": "commit"}})
        resolved = rs.resolve_tag(ref, refuse)
        self.assertFalse(resolved["annotated"])
        self.assertEqual(resolved["commit"], "bbf82ce6a02197104f221d03f6cae84bb580dff0")

    def test_chained_tag_objects_are_followed(self):
        bodies = {"aaa": json.dumps({"sha": "aaa", "object": {"sha": "bbb", "type": "tag"}}),
                  "bbb": json.dumps({"sha": "bbb", "object": {"sha": "ccc", "type": "commit"}})}
        resolved = rs.resolve_tag(json.dumps({"ref": "refs/tags/v2", "object": {"sha": "aaa", "type": "tag"}}),
                                  bodies.__getitem__)
        self.assertEqual(resolved["commit"], "ccc")
        self.assertEqual(len(resolved["hops"]), 3)

    def test_unusable_answers_are_errors_not_guesses(self):
        for body in (json.dumps({"ref": "refs/tags/tree", "object": {"sha": "aaa", "type": "tree"}}),
                     json.dumps({"ref": "x"}), "not json"):
            with self.subTest(body=body[:20]):
                with self.assertRaises(rs.SurfaceError):
                    rs.resolve_tag(body, None)

    def test_a_fetch_error_is_propagated(self):
        def fail(sha):
            raise rs.SurfaceError("HTTP 404")
        with self.assertRaises(rs.SurfaceError) as caught:
            rs.resolve_tag(ANNOTATED_REF, fail)
        self.assertIn("404", str(caught.exception))


class RegistryTests(unittest.TestCase):
    def test_version_order(self):
        for left, right, want in (("16.0.0", "16.0.0", 0), ("16.0.0", "15.0.0", 1), ("9.0.0", "10.0.0", -1),
                                  ("16.0.0", "16.0.0-preview.1", 1), ("16.0.0-preview.1", "16.0.0-preview.2", -1),
                                  ("16.0", "16.0.0", 0), ("16.0.0+build7", "16.0.0", 0)):
            with self.subTest(left=left, right=right):
                self.assertEqual(rs.compare_versions(left, right), want)
        self.assertEqual(rs.highest(["1.0.0", "9.0.0", "10.0.0", "16.0.0", "16.0.0-preview.3", "2.1.0"]), "16.0.0")
        self.assertIsNone(rs.highest([]))

    def test_never_published_is_not_the_same_state_as_wrong_version(self):
        cases = (({"versions": ["20.0.0"], "current": "20.0.0"}, rs.PRESENT),
                 ({"versions": ["20.0.0"], "current": "20.0.0"}, rs.WRONG_VERSION),
                 ({"versions": [], "current": None}, rs.NOT_PUBLISHED),
                 ({"versions": None, "current": None}, rs.UNREACHABLE))
        expected = ("20.0.0", "18.0.0", "18.0.0", "18.0.0")
        for (index, want), version in zip(cases, expected):
            with self.subTest(want=want):
                self.assertEqual(rs.classify_index(index, version), want)
        self.assertNotEqual(rs.WRONG_VERSION, rs.NOT_PUBLISHED)

    def test_wrong_version_distinguishes_a_published_version_from_an_absent_one(self):
        index = {"versions": ["18.0.0", "20.0.0"], "current": "20.0.0"}
        self.assertTrue(rs.has_exact(index, "18.0.0"), "18.0.0 is published but is not current")
        self.assertFalse(rs.has_exact(index, "19.0.0"))

    def test_a_404_is_not_published_while_a_500_stays_unknown(self):
        for status, versions in ((404, []), (500, None)):
            with mock.patch.object(rs, "http_get", return_value=(status, b"", {})):
                self.assertEqual(rs.nuget_index("Some.Package")["versions"], versions)

    def test_nuget_takes_the_highest_version_and_npm_takes_the_latest_tag(self):
        with mock.patch.object(rs, "http_get", return_value=(200, json.dumps({"versions": ["1.0.0", "10.0.0"]}).encode(), {})):
            index = rs.nuget_index("Some.Package")
        self.assertEqual((index["current"], index["source"]), ("10.0.0", "highest version in the flatcontainer index"))
        body = json.dumps({"versions": {"9.0.0": {}, "10.0.0": {}}, "dist-tags": {"latest": "9.0.0"}}).encode()
        with mock.patch.object(rs, "http_get", return_value=(200, body, {})):
            index = rs.npm_index("some-package")
        self.assertEqual(index["current"], "9.0.0", "an install resolves dist-tags.latest, not the highest number")


class SurfaceProbeTests(unittest.TestCase):
    def surfaces(self, kind):
        return [surface for surface in rs.SURFACES["surfaces"] if surface["kind"] == kind]

    def run_probe(self, probe, surface, http_get=None, getaddrinfo=None):
        report = rs.Report(out=io.StringIO())
        with ExitStack() as stack:
            if http_get is not None:
                stack.enter_context(mock.patch.object(rs, "http_get", http_get))
            if getaddrinfo is not None:
                stack.enter_context(mock.patch.object(rs.socket, "getaddrinfo", getaddrinfo))
            probe(surface, report, 5)
        return report.findings

    def test_a_published_npm_package_reports_its_measured_version(self):
        body = json.dumps({"versions": {"10.0.0": {}}, "dist-tags": {"latest": "10.0.0"}}).encode()
        findings = self.run_probe(rs.probe_npm, self.surfaces("npm")[0],
                                  http_get=lambda *a, **k: (200, body, {}))
        self.assertEqual([f["status"] for f in findings], [rs.OK])
        self.assertEqual(findings[0]["measured"], "10.0.0")

    def test_an_unpublished_npm_package_is_a_finding_and_an_unreachable_one_is_not(self):
        for answer, status in ((lambda *a, **k: (404, b"", {}), rs.FAIL),
                               (mock.Mock(side_effect=rs.SurfaceError("timed out")), rs.UNKNOWN)):
            with self.subTest(status=status):
                findings = self.run_probe(rs.probe_npm, self.surfaces("npm")[0], http_get=answer)
                self.assertEqual(findings[0]["status"], status)

    def test_a_host_is_measured_by_dns_and_its_http_answer(self):
        surface = next(s for s in self.surfaces("host") if s.get("health"))
        infos = [(2, 1, 6, "", ("203.0.113.7", 443))]
        findings = self.run_probe(rs.probe_host, surface,
                                  getaddrinfo=mock.Mock(return_value=infos),
                                  http_get=lambda *a, **k: (200, b'{"status":"ok"}', {"Server": "cloudflare"}))
        self.assertEqual([(f["check"], f["status"]) for f in findings],
                         [("dns", rs.OK), ("http", rs.OK), ("health", rs.OK)])
        self.assertIn("203.0.113.7", findings[0]["detail"])
        self.assertEqual(findings[2]["measured"], "HTTP 200")

    def test_an_unresolvable_host_never_reaches_http(self):
        findings = self.run_probe(rs.probe_host, self.surfaces("host")[0],
                                  getaddrinfo=mock.Mock(side_effect=OSError("no such host")))
        self.assertEqual([(f["check"], f["status"]) for f in findings], [("dns", rs.FAIL)])

    def test_a_bad_http_status_is_a_finding(self):
        findings = self.run_probe(rs.probe_host, self.surfaces("host")[0],
                                  getaddrinfo=mock.Mock(return_value=[(2, 1, 6, "", ("203.0.113.7", 443))]),
                                  http_get=lambda *a, **k: (503, b"", {}))
        self.assertEqual(findings[1]["status"], rs.FAIL)
        self.assertEqual(findings[1]["measured"], "HTTP 503")

    def test_the_oidc_issuer_must_be_the_one_the_tenant_returns(self):
        surface = self.surfaces("oidc")[0]
        issuer = surface["issuer"].rstrip("/")
        for returned, status in ((issuer + "/", rs.OK), ("https://someone-else.example/", rs.FAIL)):
            with self.subTest(returned=returned):
                body = json.dumps({"issuer": returned, "jwks_uri": "x", "scopes_supported": []}).encode()
                findings = self.run_probe(rs.probe_oidc, surface, http_get=lambda *a, **k: (200, body, {}))
                self.assertEqual(findings[0]["status"], status)

    def test_every_surface_has_a_probe_and_needs_no_credential(self):
        for surface in rs.SURFACES["surfaces"]:
            self.assertIn(surface["kind"], rs.PROBES)

    def test_targets_are_data_and_appear_nowhere_in_the_code(self):
        """The package names, hosts and the tenant live in metadata/surfaces.json
        only; a second copy in a function is the redundancy this guards against."""
        source = (ROOT / "release_surface.py").read_text()
        for surface in rs.SURFACES["surfaces"]:
            for key in ("id", "package", "url", "health", "issuer"):
                value = surface.get(key)
                if value:
                    self.assertNotIn(value, source, f"{value} is written in the code as well as in the data")


def gh_answers(runs, release=None, jobs=None):
    """A fake gh, so the release command can be driven without GitHub."""
    commit = "bbf82ce6a02197104f221d03f6cae84bb580dff0"
    bodies = {"git/ref/heads/main": json.dumps({"object": {"sha": commit, "type": "commit"}}),
              "git/ref/tags/v1.0.0": ANNOTATED_REF,
              "git/tags/a089d4524dba2e7e3de83e552c986e2c09a3d1e1": ANNOTATED_TAG,
              "actions/runs?head_sha": json.dumps({"workflow_runs": runs}),
              "/jobs": json.dumps({"jobs": jobs or []})}
    if release is not None:
        bodies["releases/tags/v1.0.0"] = json.dumps(release)

    def fetch(path, timeout=120):
        for fragment, body in bodies.items():
            if fragment in path:
                return body
        raise rs.SurfaceError(f"gh api {path}: gh: Not Found (HTTP 404)")
    return fetch


class ReleaseCommandTests(unittest.TestCase):
    def release_run(self, *argv, **answers):
        fetch = gh_answers(**answers)
        out = io.StringIO()
        with mock.patch.object(rs, "gh_blocked", return_value=None), \
             mock.patch.object(rs, "gh_api", fetch), \
             mock.patch.object(rs, "gh_json", lambda path, timeout=120: json.loads(fetch(path, timeout))), \
             redirect_stdout(out):
            code = otel_check.main(["release", "owner/repo", "1.0.0", *argv])
        return code, out.getvalue()

    def finished_run(self):
        return {"id": 1, "name": "nuget-publish", "status": "completed", "conclusion": "success",
                "event": "push", "created_at": "2026-09-07T19:51:13Z", "updated_at": "2026-09-07T19:56:13Z"}

    def published_release(self):
        return {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "author": {"login": "ancplua"},
                "created_at": "2026-09-07T19:51:09Z", "published_at": "2026-09-07T19:51:20Z"}

    def test_a_clean_release_passes(self):
        code, output = self.release_run(runs=[self.finished_run()], release=self.published_release(),
                                        jobs=[{"name": "pack", "labels": ["ubuntu-latest"], "conclusion": "success",
                                               "started_at": "2026-09-07T19:51:20Z",
                                               "completed_at": "2026-09-07T19:55:20Z"}])
        self.assertEqual(code, 0, output)
        self.assertIn("tag v1.0.0 is ANNOTATED", output)
        self.assertIn("tag target == main", output)

    def test_a_missing_release_fails_while_the_tag_still_resolves(self):
        code, output = self.release_run(runs=[self.finished_run()], release=None)
        self.assertEqual(code, 1)
        self.assertIn("no release for v1.0.0", output)
        self.assertIn("tag v1.0.0 is ANNOTATED", output, "a tag without a release still resolves")

    def test_a_release_left_as_a_draft_is_a_finding(self):
        stale = dict(self.published_release(), published_at="2026-09-07T20:19:09Z")
        code, output = self.release_run(runs=[self.finished_run()], release=stale)
        self.assertEqual(code, 1)
        self.assertIn("published_at is 28.0 min", output)

    def test_an_unfinished_run_does_not_colour_the_budget_green(self):
        """It gets its own UNKNOWN line, so the exit code stays non-zero even
        though no completed run is over the budget."""
        running = dict(self.finished_run(), id=2, name="qyl-verify", status="in_progress", conclusion=None,
                       created_at="2026-09-08T02:02:50Z", updated_at="2026-09-08T02:03:02Z")
        code, output = self.release_run(runs=[running], release=self.published_release())
        self.assertEqual(code, 1)
        self.assertIn("1 of 1 runs not assessable", output)
        self.assertIn("no completed run over the wall clock budget", output)
        self.assertRegex(output, r"qyl-verify\s+in_progress\s+unknown\s+unknown\s+unknown",
                         "the three time columns stay unknown, and none of them grows a unit")

    def test_a_missing_gh_is_unknown_and_never_a_finding(self):
        out = io.StringIO()
        with mock.patch.object(rs, "gh_blocked", return_value="gh is not authenticated"), redirect_stdout(out):
            code = otel_check.main(["release", "owner/repo", "1.0.0"])
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN", out.getvalue())
        self.assertIn("not a finding", out.getvalue())
        self.assertIn("0 FAIL", out.getvalue(), "a missing prerequisite must not be counted as a finding")


class PackagesCommandTests(unittest.TestCase):
    def packages_run(self, *argv, answer):
        out = io.StringIO()
        with mock.patch.object(rs, "http_get", answer), redirect_stdout(out):
            code = otel_check.main(["packages", *argv])
        return code, out.getvalue()

    def test_the_expected_version_passes_and_a_higher_one_does_not(self):
        body = json.dumps({"versions": ["18.0.0", "20.0.0"]}).encode()
        code, output = self.packages_run("20.0.0", "Some.Package", answer=lambda *a, **k: (200, body, {}))
        self.assertEqual(code, 0, output)
        code, output = self.packages_run("18.0.0", "Some.Package", answer=lambda *a, **k: (200, body, {}))
        self.assertEqual(code, 1)
        self.assertIn("WRONG VERSION", output)
        self.assertIn("is published but is not the current version", output)

    def test_a_package_that_never_shipped_says_so_instead_of_wrong_version(self):
        code, output = self.packages_run("18.0.0", "Some.Package", answer=lambda *a, **k: (404, b"", {}))
        self.assertEqual(code, 1)
        self.assertIn("NOT PUBLISHED", output)
        self.assertIn("it was never published", output)
        self.assertNotIn("WRONG VERSION", output)

    def test_an_unreachable_registry_is_unknown_not_a_finding(self):
        code, output = self.packages_run("18.0.0", "Some.Package",
                                         answer=mock.Mock(side_effect=rs.SurfaceError("timed out")))
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN", output)
        self.assertIn("0 FAIL", output, "a registry that could not be asked says nothing about the package")


if __name__ == "__main__":
    unittest.main()
