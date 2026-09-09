"""Measure released surfaces: GitHub releases, package registries, and public hosts.

Every finding names the measured value and the source it came from, never only a
verdict. Anything that cannot be measured is UNKNOWN, and UNKNOWN never counts as
passed. No credential is handled here: GitHub is reached through the gh CLI,
which already holds its own token, and every other surface answers unauthenticated.
"""

import functools
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The one place that names what this product ships. Identities only: a version
# written into a file is a claim, the registry answer is the evidence.
SURFACES = json.loads((ROOT / "metadata/surfaces.json").read_text())

OK, WARN, FAIL, UNKNOWN, INFO = "OK", "WARN", "FAIL", "UNKNOWN", "INFO"


class SurfaceError(Exception):
    """A surface could not be asked. It says nothing about what the surface holds."""


class Report:
    """Findings of one command run, plus the output mode."""

    def __init__(self, as_json=False, out=None):
        self.findings, self.as_json, self.out = [], as_json, out or sys.stdout

    def say(self, text=""):
        if not self.as_json:
            print(text, file=self.out)

    def add(self, check, status, subject, measured="", source="", detail=""):
        finding = {"check": check, "status": status, "subject": subject,
                   "measured": measured, "source": source, "detail": detail}
        self.findings.append(finding)
        return finding

    def line(self, check, status, subject, measured="", source="", detail=""):
        finding = self.add(check, status, subject, measured, source, detail)
        if self.as_json:
            return finding
        print(f"{status:<7} {subject}", file=self.out)
        for label, value in (("measured:", measured), ("", detail), ("source:  ", source)):
            if value:
                print(f"        {label} {value}" if label else f"        {value}", file=self.out)
        return finding

    def counts(self):
        return {level: sum(f["status"] == level for f in self.findings)
                for level in (OK, WARN, FAIL, UNKNOWN, INFO)}

    def passed(self):
        """WARN, FAIL and UNKNOWN all fail the run; only OK and INFO pass."""
        return all(f["status"] in (OK, INFO) for f in self.findings)

    def finish(self):
        if self.as_json:
            for finding in self.findings:
                print(json.dumps(finding), file=self.out)
        else:
            counts = self.counts()
            print(f'\n{len(self.findings)} checks — {counts[OK]} OK, {counts[WARN]} WARN, '
                  f'{counts[FAIL]} FAIL, {counts[UNKNOWN]} unknown', file=self.out)
        return 0 if self.passed() else 1


# --- timing: wall clock, compute time and queue time are three numbers ----------
#
# A report that says "no workflow over ten minutes" while measuring job duration
# is false whenever runners queue. Nothing below reads the clock or the network.

def parse_time(value):
    """RFC3339 to an aware UTC datetime, or None. A missing stamp is never guessed."""
    if not value or value == "null":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def job_duration(job):
    if job.get("started_at") is None or job.get("completed_at") is None:
        return None
    return (job["completed_at"] - job["started_at"]).total_seconds()


def job_label(job):
    labels = job.get("labels") or []
    return labels[0] if labels else "unknown"


def runner_class(label):
    """Fold a runner label into ubuntu/macos/windows, or keep the label itself."""
    lowered = (label or "").lower()
    for prefix, name in (("ubuntu", "ubuntu"), ("macos", "macos"), ("mac-", "macos"), ("windows", "windows")):
        if lowered.startswith(prefix):
            return name
    return lowered or "unknown"


def split_run(run):
    """Wall clock, compute time and queue time of one run; None where unmeasurable.

        wall    = updated_at - created_at
        compute = max(completed_at - started_at) over the jobs
        queue   = wall - compute

    updated_at on a run that has not finished is the last time GitHub touched it,
    not its end: subtracting created_at from it yields a wall clock shorter than
    the compute already spent. So an unfinished run keeps no wall clock at all.
    """
    wall = None
    if run.get("status") == "completed" and run.get("created_at") and run.get("updated_at"):
        wall = (run["updated_at"] - run["created_at"]).total_seconds()
    compute, longest = None, ""
    for job in run.get("jobs") or []:
        duration = job_duration(job)
        if duration is None:
            continue
        if compute is None or duration > compute:
            compute, longest = duration, job.get("name", "")
    queue, cause = None, "unknown"
    if wall is not None and compute is not None:
        queue = max(wall - compute, 0.0)
        cause = "queue" if queue > compute else "compute"
    return {"wall": wall, "compute": compute, "queue": queue, "cause": cause, "longest_job": longest}


def queue_share(split):
    """Share of the wall clock spent waiting, 0..1, or None."""
    if split["queue"] is None or not split["wall"]:
        return None
    return split["queue"] / split["wall"]


def _rank(entries):
    """Measured labels first, then the largest start delay, then the label name."""
    return sorted(entries, key=lambda e: (e["min_delay"] is None, -(e["min_delay"] or 0.0), e["label"]))


def _entry(store, label):
    return store.setdefault(label, {"label": label, "class": runner_class(label), "jobs": 0,
                                    "first_start": None, "last_start": None,
                                    "min_delay": None, "max_delay": None})


def delays_by_label(run):
    """Start delay per runner label within one run, measured from its created_at.

    The label is kept verbatim ("ubuntu-24.04-arm") because the class alone hides
    which image queued. min_delay belongs to the FIRST start of a label: that is
    the queue signal, while a later start may have waited on an upstream job
    rather than on a runner.
    """
    created, store = run.get("created_at"), {}
    for job in run.get("jobs") or []:
        entry = _entry(store, job_label(job))
        entry["jobs"] += 1
        start = job.get("started_at")
        if start is None or created is None:
            continue
        delay = max((start - created).total_seconds(), 0.0)
        if entry["first_start"] is None or start < entry["first_start"]:
            entry["first_start"], entry["min_delay"] = start, delay
        if entry["last_start"] is None or start > entry["last_start"]:
            entry["last_start"], entry["max_delay"] = start, delay
    return _rank(store.values())


def spans_by_label(runs):
    """Aggregate the per-run delays of several runs per runner label.

    Each run has its own created_at, so the smallest delay is not necessarily the
    earliest start; both are tracked separately.
    """
    store = {}
    for run in runs:
        created = run.get("created_at")
        for job in run.get("jobs") or []:
            entry = _entry(store, job_label(job))
            entry["jobs"] += 1
            start = job.get("started_at")
            if start is None or created is None:
                continue
            delay = max((start - created).total_seconds(), 0.0)
            for key, pick in (("first_start", min), ("last_start", max)):
                entry[key] = start if entry[key] is None else pick(entry[key], start)
            for key, pick in (("min_delay", min), ("max_delay", max)):
                entry[key] = delay if entry[key] is None else pick(entry[key], delay)
    return _rank(store.values())


def worst(delays):
    """The label with the largest start delay, or None when no job reported a start."""
    return next((delay for delay in delays if delay["min_delay"] is not None), None)


def minutes(seconds):
    return f"{(seconds or 0.0) / 60:.1f}"


def minutes_or(seconds):
    return "unknown" if seconds is None else minutes(seconds)


def minutes_cell(seconds, width):
    """A table cell that says "unknown" rather than carrying a unit it has no
    number for; "unknownm" reads like a measurement and is not one."""
    return f'{"unknown" if seconds is None else minutes(seconds) + "m":>{width}}'


def percent(fraction):
    """A share as whole percent, or "unknown". The share of a wall clock that was
    queue is undefined when either half was never measured."""
    return "unknown" if fraction is None else f"{fraction * 100:.0f}%"


def mmss(seconds):
    """m:ss, for job durations where a tenth of a minute hides the seconds."""
    total = int(round(max(seconds or 0.0, 0.0)))
    return f"{total // 60}:{total % 60:02d}"


def clock(moment):
    return "unknown" if moment is None else moment.strftime("%H:%M:%SZ")


def short(sha):
    return sha[:7] if len(sha) > 7 else sha


def truncate(text, width):
    return text if len(text) <= width else text[:width - 1] + "~"


def plural(count):
    return "1 job" if count == 1 else f"{count} jobs"


# --- GitHub, through the already-authenticated gh CLI ---------------------------

def gh_blocked():
    """Why gh cannot answer, or None.

    gh holds its own token, so this tool never touches a credential. A missing or
    logged-out gh is a missing prerequisite, not a finding about a release, and
    the caller reports UNKNOWN rather than red.
    """
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"gh is not runnable ({error}); install the GitHub CLI"
    if result.returncode:
        return "gh is not authenticated; run `gh auth login`"
    return None


def gh_api(path, timeout=120):
    """One GitHub API path. A non-zero gh exit carries gh's own message, so a 404
    is never mistaken for empty data."""
    try:
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise SurfaceError(f"gh api {path}: {error}")
    if result.returncode:
        message = result.stderr.strip().splitlines()
        raise SurfaceError(f"gh api {path}: {message[0] if message else 'exit ' + str(result.returncode)}")
    return result.stdout


def gh_json(path, timeout=120):
    body = gh_api(path, timeout)
    try:
        return json.loads(body)
    except ValueError as error:
        raise SurfaceError(f"gh api {path}: cannot decode: {error}")


MAX_TAG_HOPS = 5


def resolve_tag(ref_body, fetch_tag):
    """Follow a tag ref to the commit it really names.

    An annotated tag answers with type "tag", and the sha in that answer is the
    tag OBJECT, not the commit. Comparing that sha against a branch head is the
    mistake this function exists to prevent. fetch_tag returns git/tags/<sha>.
    """
    try:
        ref = json.loads(ref_body) if isinstance(ref_body, (str, bytes, bytearray)) else ref_body
    except ValueError as error:
        raise SurfaceError(f"tag ref: cannot decode: {error}")
    obj = (ref or {}).get("object") or {}
    if not obj.get("sha"):
        raise SurfaceError("tag ref: no object sha in the answer")
    resolved = {"commit": "", "annotated": False, "tagger": "", "tagged_at": "", "message": "",
                "hops": [f'{ref.get("ref") or "ref"} -> {obj["sha"]} ({obj.get("type")})']}
    for hop in range(MAX_TAG_HOPS + 1):
        if obj.get("type") != "tag":
            break
        if hop == MAX_TAG_HOPS:
            raise SurfaceError(f"tag ref: more than {MAX_TAG_HOPS} chained tag objects")
        resolved["annotated"] = True
        try:
            tag = json.loads(fetch_tag(obj["sha"]))
        except ValueError as error:
            raise SurfaceError(f'tag object {obj["sha"]}: cannot decode: {error}')
        target = tag.get("object") or {}
        if not target.get("sha"):
            raise SurfaceError(f'tag object {obj["sha"]}: no target sha')
        tagger = tag.get("tagger") or {}
        resolved["tagger"], resolved["tagged_at"] = tagger.get("name", ""), tagger.get("date", "")
        resolved["message"] = next(iter((tag.get("message") or "").strip().splitlines()), "")
        resolved["hops"].append(f'tag object {short(obj["sha"])} -> {target["sha"]} ({target.get("type")})')
        obj = target
    if obj.get("type") != "commit":
        raise SurfaceError(f'tag ref: resolves to {obj.get("type")!r}, not a commit')
    resolved["commit"] = obj["sha"]
    return resolved


# --- package registries ---------------------------------------------------------

PRESENT, WRONG_VERSION, NOT_PUBLISHED, UNREACHABLE = "OK", "WRONG VERSION", "NOT PUBLISHED", "UNKNOWN"

NUGET_INDEX = "https://api.nuget.org/v3-flatcontainer/{name}/index.json"
NPM_INDEX = "https://registry.npmjs.org/{name}"


def version_parts(version):
    """Release numbers and the prerelease tag. Build metadata after '+' is
    ignored, as the registries ignore it."""
    text = str(version).strip().split("+", 1)[0]
    prerelease = ""
    if "-" in text:
        text, prerelease = text.split("-", 1)
    numbers = []
    for part in text.split("."):
        numbers.append(int(part) if part.isdigit() else 0)
    return numbers, prerelease


def compare_versions(left, right):
    """Order two versions: release numbers first, then prerelease, where a
    version without a prerelease tag outranks one with it."""
    left_numbers, left_pre = version_parts(left)
    right_numbers, right_pre = version_parts(right)
    for index in range(max(len(left_numbers), len(right_numbers))):
        first = left_numbers[index] if index < len(left_numbers) else 0
        second = right_numbers[index] if index < len(right_numbers) else 0
        if first != second:
            return -1 if first < second else 1
    if left_pre == right_pre == "":
        return 0
    if left_pre == "":
        return 1
    if right_pre == "":
        return -1
    left_pre, right_pre = left_pre.lower(), right_pre.lower()
    return (left_pre > right_pre) - (left_pre < right_pre)


def highest(versions):
    return max(versions, key=functools.cmp_to_key(compare_versions)) if versions else None


def http_get(url, timeout=30, limit=4 << 20):
    """GET a public URL. An HTTP error status is an answer, not an exception;
    only an unreachable host raises."""
    request = urllib.request.Request(url, headers={"User-Agent": "otel-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(limit), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(limit), dict(error.headers or {})
    except (urllib.error.URLError, OSError) as error:
        raise SurfaceError(str(getattr(error, "reason", error)))


def _index(url, timeout, extract, source):
    """Ask one registry index and normalise its answer.

    versions is [] for a package the registry does not know and None when the
    registry itself could not be asked; those are different states.
    """
    answer = {"url": url, "versions": None, "current": None, "source": source, "note": ""}
    try:
        status, body, _ = http_get(url, timeout)
    except SurfaceError as error:
        answer["note"] = str(error)
        return answer
    if status == 404:
        answer["versions"], answer["note"] = [], "HTTP 404 — the registry does not know this package id"
        return answer
    if status != 200:
        answer["note"] = f"HTTP {status}"
        return answer
    try:
        answer["versions"], answer["current"] = extract(json.loads(body))
    except (ValueError, KeyError, TypeError) as error:
        answer["note"] = f"answer not readable: {error}"
        return answer
    if not answer["versions"]:
        answer["versions"], answer["note"] = [], "index present but without versions"
    return answer


def nuget_index(package_id, timeout=30):
    """flatcontainer publishes no "latest" pointer, so the highest published
    version is the registry's answer for the current one."""
    return _index(NUGET_INDEX.format(name=package_id.lower()), timeout,
                  lambda doc: ((doc.get("versions") or []), highest(doc.get("versions") or [])),
                  "highest version in the flatcontainer index")


def npm_index(package_name, timeout=30):
    """npm publishes dist-tags.latest, which is what `npm install` resolves to;
    that pointer is the registry's answer, not the highest number in the list."""
    def extract(doc):
        versions = sorted((doc.get("versions") or {}), key=lambda v: v)
        latest = (doc.get("dist-tags") or {}).get("latest") or highest(versions)
        return versions, latest
    return _index(NPM_INDEX.format(name=urllib.parse.quote(package_name, safe="@/")), timeout,
                  extract, "dist-tags.latest")


def classify_index(index, expected):
    """"Never published" and "published at another version" are separate states.
    Collapsing them hides a package that never shipped at all."""
    if index["versions"] is None:
        return UNREACHABLE
    if not index["versions"]:
        return NOT_PUBLISHED
    if index["current"] and compare_versions(index["current"], expected) == 0:
        return PRESENT
    return WRONG_VERSION


def has_exact(index, expected):
    return any(compare_versions(version, expected) == 0 for version in index["versions"] or [])


# --- otel-check packages --------------------------------------------------------

def cmd_packages(args):
    fetch = npm_index if args.npm else nuget_index
    registry = "registry.npmjs.org" if args.npm else "nuget.org"
    report = Report(args.json)
    report.say(f"otel-check packages {args.version} — {len(args.package)} packages on {registry}\n")
    report.say(f"    {'Package':<54} {'State':<15} {'current':>17} {'versions':>10}")
    indexes = []
    for package in args.package:
        index = fetch(package, args.timeout)
        indexes.append((package, index, classify_index(index, args.version)))
        report.say(f'    {truncate(package, 54):<54} {indexes[-1][2]:<15} '
                   f'{index["current"] or "unknown":>17} {len(index["versions"] or []):>10}')
    report.say()
    for package, index, state in indexes:
        count = f'{len(index["versions"] or [])} versions in the index'
        if state is PRESENT:
            report.line("package", OK, f'{package} is on {registry} at {args.version}',
                        f'current {index["current"]}', index["url"], f'{count}; source: {index["source"]}')
        elif state is WRONG_VERSION:
            seen = (f'{args.version} is published but is not the current version' if has_exact(index, args.version)
                    else f'{args.version} is not in the index at all')
            report.line("package", FAIL, f"{package}: WRONG VERSION",
                        f'current {index["current"]}, expected {args.version}', index["url"],
                        f"{seen}; {count}")
        elif state is NOT_PUBLISHED:
            report.line("package", FAIL, f"{package}: NOT PUBLISHED",
                        "0 versions", index["url"],
                        f'this is not a version error: {registry} does not know the id, it was never '
                        f'published ({index["note"]})')
        else:
            report.line("package", UNKNOWN, f"{package}: registry could not be asked",
                        "unknown", index["url"], index["note"])
    return report.finish()


# --- otel-check release ---------------------------------------------------------

def _collect_runs(report, repo, sha, timeout):
    """Every workflow run on one commit, with its jobs."""
    runs_path = f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
    answer = gh_json(runs_path, timeout)
    runs = []
    for raw in answer.get("workflow_runs") or []:
        run = {"id": raw.get("id"), "name": raw.get("name", ""), "status": raw.get("status", ""),
               "conclusion": raw.get("conclusion") or "", "event": raw.get("event", ""),
               "created_at": parse_time(raw.get("created_at")), "updated_at": parse_time(raw.get("updated_at")),
               "jobs": []}
        jobs_path = f'repos/{repo}/actions/runs/{run["id"]}/jobs?per_page=100'
        try:
            jobs = gh_json(jobs_path, timeout).get("jobs") or []
        except SurfaceError as error:
            report.line("jobs", UNKNOWN, f'jobs of {run["name"]} ({run["id"]}) not retrievable',
                        "unknown", jobs_path, str(error))
            jobs = []
        for job in jobs:
            run["jobs"].append({"name": job.get("name", ""), "labels": job.get("labels") or [],
                                "conclusion": job.get("conclusion") or "",
                                "started_at": parse_time(job.get("started_at")),
                                "completed_at": parse_time(job.get("completed_at"))})
        runs.append(run)
    return runs_path, runs


def cmd_release(args):
    report = Report(args.json)
    blocked = gh_blocked()
    if blocked:
        report.line("github", UNKNOWN, "GitHub could not be asked", "unknown", "gh CLI",
                    f"{blocked}. Nothing about {args.repo} {args.version} is claimed here; a missing "
                    f"prerequisite is not a finding.")
        return report.finish()

    tag = "v" + args.version
    budget, draft_lag = args.budget * 60, args.draft_lag * 60
    report.say(f"otel-check release {args.repo} {args.version}")
    report.say(f"Budget {minutes(budget)} min wall clock per run, draft tolerance {minutes(draft_lag)} min\n")

    report.say(f"[1] resolve {args.branch}")
    head_path = f"repos/{args.repo}/git/ref/heads/{args.branch}"
    branch_sha = ""
    try:
        head = gh_json(head_path, args.timeout)
        branch_sha = (head.get("object") or {}).get("sha", "")
        report.line("branch", OK, f"{args.branch} resolved", branch_sha, head_path,
                    f'type {(head.get("object") or {}).get("type")}')
    except SurfaceError as error:
        report.line("branch", UNKNOWN, f"{args.branch} not resolvable", "unknown", head_path, str(error))

    report.say(f"\n[2] resolve tag {tag}")
    ref_path = f"repos/{args.repo}/git/ref/tags/{tag}"
    tag_sha = ""
    try:
        ref_body = gh_api(ref_path, args.timeout)
        resolved = resolve_tag(ref_body, lambda sha: gh_api(f"repos/{args.repo}/git/tags/{sha}", args.timeout))
        tag_sha = resolved["commit"]
        kind = ("ANNOTATED (tag object, dereferenced)" if resolved["annotated"]
                else "lightweight (points straight at the commit)")
        detail = " | ".join(resolved["hops"])
        if resolved["tagger"]:
            detail += f' | tagger {resolved["tagger"]} {resolved["tagged_at"]}'
        report.line("tag", OK, f"tag {tag} is {kind}", tag_sha, ref_path, detail)
    except SurfaceError as error:
        report.line("tag", FAIL, f"tag {tag} not resolvable", "unknown", ref_path, str(error))

    both = f"{head_path} + {ref_path}"
    if not branch_sha or not tag_sha:
        report.line("tag-vs-branch", UNKNOWN, f"tag target not comparable against {args.branch}",
                    "unknown", both, "one of the two SHAs is missing")
    elif branch_sha == tag_sha:
        report.line("tag-vs-branch", OK, f"tag target == {args.branch}",
                    f"{short(tag_sha)} == {short(branch_sha)}", both,
                    "compared was the dereferenced commit, not the tag object")
    else:
        report.line("tag-vs-branch", FAIL, f"tag target != {args.branch}",
                    f"{short(tag_sha)} != {short(branch_sha)}", both,
                    "the tag points at a different commit than the branch head")

    report.say(f"\n[3] release {tag}")
    release_path = f"repos/{args.repo}/releases/tags/{tag}"
    try:
        release = gh_json(release_path, args.timeout)
    except SurfaceError as error:
        report.line("release", FAIL, f"no release for {tag}", "unknown", release_path, str(error))
    else:
        author = (release.get("author") or {}).get("login", "")
        report.line("release", FAIL if release.get("draft") else OK, f"release {tag} exists",
                    f'draft={bool(release.get("draft"))} prerelease={bool(release.get("prerelease"))} author={author}',
                    release_path,
                    f'created_at {release.get("created_at")}, published_at {release.get("published_at")}')
        created, published = parse_time(release.get("created_at")), parse_time(release.get("published_at"))
        if created is None or published is None:
            report.line("release-lag", UNKNOWN, "gap created_at/published_at not measurable",
                        "unknown", release_path, "a timestamp is missing")
        else:
            lag = (published - created).total_seconds()
            measured = (f'{minutes(lag)} min ({mmss(lag)}, {release.get("created_at")} -> '
                        f'{release.get("published_at")})')
            if lag > draft_lag:
                report.line("release-lag", WARN,
                            f"published_at is {minutes(lag)} min ({mmss(lag)}) after created_at",
                            measured, release_path,
                            f"the release sat unpublished for {minutes(lag)} min (tolerance "
                            f"{minutes(draft_lag)} min) — in that window the tag was public and the release was not")
            else:
                report.line("release-lag", OK, "published_at follows created_at without delay",
                            measured, release_path)

    sha, sha_source = (tag_sha, f"tag {tag}") if tag_sha else (branch_sha, args.branch)
    if not sha:
        report.line("runs", UNKNOWN, "no runs checkable", "unknown", "",
                    "neither the tag nor the branch SHA could be resolved")
        return report.finish()
    try:
        runs_path, runs = _collect_runs(report, args.repo, sha, args.timeout)
    except SurfaceError as error:
        report.line("runs", UNKNOWN, "workflow runs not retrievable", "unknown",
                    f"repos/{args.repo}/actions/runs", str(error))
        return report.finish()

    splits = {run["id"]: split_run(run) for run in runs}
    runs.sort(key=lambda run: (splits[run["id"]]["wall"] is None,
                               -(splits[run["id"]]["wall"] or 0.0), run["name"]))

    report.say(f"\n[4] workflow runs on {short(sha)} ({sha_source}) — {len(runs)} runs")
    report.say(f'    {"Run":<24} {"Result":<11} {"wall":>8} {"compute":>11} {"queue":>11}  '
               f'{"Cause":<9} largest start delay')
    for run in runs:
        split, delay = splits[run["id"]], worst(delays_by_label(run))
        delay = f'{delay["label"]} +{minutes(delay["min_delay"])} min' if delay else "unknown"
        report.say(f'    {truncate(run["name"], 24):<24} {(run["conclusion"] or run["status"]):<11} '
                   f'{minutes_cell(split["wall"], 8)} {minutes_cell(split["compute"], 11)} '
                   f'{minutes_cell(split["queue"], 11)}  {split["cause"]:<9} {delay}')

    for run in runs:
        source = f'repos/{args.repo}/actions/runs/{run["id"]}'
        subject = f'{run["name"]} ({run["id"]})'
        if run["conclusion"] == "success":
            report.add("run-result", OK, subject, "success", source, f'event {run["event"]}')
        elif not run["conclusion"]:
            report.add("run-result", UNKNOWN, subject, f'status {run["status"]}', source, "no result reported")
        else:
            report.add("run-result", FAIL, subject, run["conclusion"], source, f'event {run["event"]}')

    over = [run for run in runs if splits[run["id"]]["wall"] is not None and splits[run["id"]]["wall"] > budget]
    unmeasured = [run for run in runs if splits[run["id"]]["wall"] is None]
    report.say(f"\n[5] budget {minutes(budget)} min wall clock — {len(over)} of {len(runs)} runs over it")
    if not over:
        # An unfinished run has no wall clock. It gets its own UNKNOWN line so it
        # cannot colour the budget check green.
        if unmeasured:
            report.line("budget", UNKNOWN,
                        f"{len(unmeasured)} of {len(runs)} runs not assessable — not yet completed",
                        "unknown", runs_path,
                        "a wall clock exists only once a run has finished; until then nothing is reported as kept")
        report.line("budget", OK, "no completed run over the wall clock budget",
                    f"0 of {len(runs)} runs over {minutes(budget)} min", runs_path)
    for run in over:
        split, delays = splits[run["id"]], delays_by_label(run)
        parts = [(f'{delay["label"]} from {clock(delay["first_start"])} (+{minutes(delay["min_delay"])} min, '
                  f'{plural(delay["jobs"])})') if delay["min_delay"] is not None
                 else f'{delay["label"]} unknown' for delay in delays]
        share = queue_share(split)
        report.line("budget", FAIL, f'{run["name"]} over budget',
                    f'wall {minutes(split["wall"])} min = compute {minutes(split["compute"])} min '
                    f'+ queue {minutes(split["queue"])} min',
                    f'repos/{args.repo}/actions/runs/{run["id"]} + /jobs',
                    f'cause {split["cause"]}: compute {minutes(split["compute"])} min against queue '
                    f'{minutes(split["queue"])} min ({percent(share)} queue); longest job '
                    f'"{split["longest_job"]}" {mmss(split["compute"])}; start delay from run.created_at '
                    f'{clock(run["created_at"])}: {"; ".join(parts)}')
        report.say(f'        start delay per runner label from run.created_at {clock(run["created_at"])}:')
        for delay in delays:
            if delay["min_delay"] is None:
                report.say(f'          {delay["label"]:<18} {plural(delay["jobs"]):<7}  start unknown')
            else:
                report.say(f'          {delay["label"]:<18} {plural(delay["jobs"]):<7}  '
                           f'earliest start {clock(delay["first_start"])}  delay {minutes(delay["min_delay"])} min')

    if over:
        spans = spans_by_label(over)
        report.say(f"\n[6] runner labels in the {len(over)} runs found (delay from run.created_at)")
        report.say(f'    {"Label":<18} {"Jobs":>5} {"first start":>14} {"last start":>14} '
                   f'{"delay min":>11} {"delay max":>11}')
        for span in spans:
            if span["min_delay"] is None:
                report.say(f'    {span["label"]:<18} {span["jobs"]:>5} {"unknown":>14} {"unknown":>14} '
                           f'{"unknown":>11} {"unknown":>11}')
            else:
                report.say(f'    {span["label"]:<18} {span["jobs"]:>5} {clock(span["first_start"]):>14} '
                           f'{clock(span["last_start"]):>14} {minutes(span["min_delay"]):>10}m '
                           f'{minutes(span["max_delay"]):>10}m')
        report.say("    The FIRST start of a label is the queue signal; later starts may have waited")
        report.say("    on upstream jobs rather than on a runner.")
        if len(spans) > 1 and spans[0]["min_delay"] is not None and spans[-1]["min_delay"] is not None:
            slow, fast = spans[0], spans[-1]
            report.line("runner-split", INFO,
                        f'{slow["label"]} queued longer than {fast["label"]}',
                        f'{slow["label"]} from {clock(slow["first_start"])} (first start, delay '
                        f'{minutes(slow["min_delay"])} min; last start {clock(slow["last_start"])}), '
                        f'{fast["label"]} from {clock(fast["first_start"])} (delay {minutes(fast["min_delay"])} min)',
                        runs_path,
                        "the runner class with the largest delay sets the wall clock, not the job duration")
    return report.finish()


# --- otel-check surface ---------------------------------------------------------
#
# One probe per kind. The targets live in metadata/surfaces.json, never as
# literals here, so a new package or host is a data change.

def probe_npm(surface, report, timeout):
    package = surface["package"]
    index = npm_index(package, timeout)
    if index["versions"] is None:
        report.line("npm", UNKNOWN, f"{package}: registry could not be asked", "unknown",
                    index["url"], index["note"])
    elif not index["versions"]:
        report.line("npm", FAIL, f"{package}: NOT PUBLISHED", "0 versions", index["url"],
                    f'registry.npmjs.org does not know this name ({index["note"]})')
    else:
        report.line("npm", OK, f"{package} is published", index["current"], index["url"],
                    f'{len(index["versions"])} versions in the index; the current one is '
                    f'{index["source"]}, which is what an install resolves to')


def probe_host(surface, report, timeout):
    """DNS and the HTTP answer of a public host. No dashboard, no API token."""
    url = surface["url"]
    host = urllib.parse.urlsplit(url).hostname
    label = f'{surface["id"]} ({surface["provider"]})'
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        report.line("dns", FAIL, f"{label}: {host} does not resolve", "unknown", f"DNS {host}", str(error))
        return
    # sockaddr is a tuple whose first element is the address; widen it explicitly
    # rather than relying on the family to know its shape.
    addresses = sorted({str(info[4][0]) for info in infos})
    report.line("dns", OK, f"{label}: {host} resolves", f"{len(addresses)} addresses",
                f"DNS {host}", ", ".join(addresses))
    for check, target in (("http", url), ("health", surface.get("health"))):
        if not target:
            continue
        try:
            status, body, headers = http_get(target, timeout)
        except SurfaceError as error:
            report.line(check, FAIL, f"{label}: {target} unreachable", "unknown", target, str(error))
            continue
        served_by = headers.get("Server") or headers.get("server") or "no server header"
        excerpt = body[:120].decode("utf-8", "replace").strip().replace("\n", " ")
        level = OK if 200 <= status < 300 else FAIL
        report.line(check, level, f"{label}: {target} answers", f"HTTP {status}", target,
                    f"served by {served_by}; {len(body)} bytes; first bytes: {excerpt!r}")


def probe_oidc(surface, report, timeout):
    """The tenant's public discovery document. No management API token is used."""
    issuer = surface["issuer"].rstrip("/")
    url = issuer + "/.well-known/openid-configuration"
    label = f'{surface["id"]} ({surface["provider"]})'
    try:
        status, body, _ = http_get(url, timeout)
    except SurfaceError as error:
        report.line("oidc", FAIL, f"{label}: discovery document unreachable", "unknown", url, str(error))
        return
    if status != 200:
        report.line("oidc", FAIL, f"{label}: discovery document not served", f"HTTP {status}", url,
                    "an existing tenant answers this path unauthenticated")
        return
    try:
        document = json.loads(body)
    except ValueError as error:
        report.line("oidc", UNKNOWN, f"{label}: discovery document not readable", "unknown", url, str(error))
        return
    measured = (document.get("issuer") or "").rstrip("/")
    level = OK if measured == issuer else FAIL
    report.line("oidc", level, f"{label}: issuer {'matches' if level is OK else 'differs'}",
                measured or "unknown", url,
                f'expected {issuer}; jwks_uri {document.get("jwks_uri")}; '
                f'{len(document.get("scopes_supported") or [])} scopes advertised')


PROBES = {"npm": probe_npm, "host": probe_host, "oidc": probe_oidc}


def cmd_surface(args):
    surfaces = SURFACES["surfaces"]
    if args.id:
        wanted, known = set(args.id), {surface["id"] for surface in surfaces}
        missing = sorted(wanted - known)
        if missing:
            print(f'otel-check surface: unknown surface {", ".join(missing)}; '
                  f'known: {", ".join(sorted(known))}', file=sys.stderr)
            return 2
        surfaces = [surface for surface in surfaces if surface["id"] in wanted]
    report = Report(args.json)
    report.say(f"otel-check surface — {len(surfaces)} public surfaces, no credential used")
    report.say(f'targets: {SURFACES["source"]}\n')
    for surface in surfaces:
        PROBES[surface["kind"]](surface, report, args.timeout)
    return report.finish()
