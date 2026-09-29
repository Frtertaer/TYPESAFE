from __future__ import annotations

import argparse
import copy
import hashlib
from collections import Counter
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SCHEMA_VERSION = 4
PROGRESS_INTS = (
    "rubric_version", "review_points", "max_assessments", "max_model_calls",
    "stall_limit", "max_evidence_chars", "max_checks", "max_items",
)
PROGRESS_TIMES = ("api_timeout_seconds", "command_timeout_seconds", "database_timeout_seconds")
GIT_COMMAND_MIN_SECONDS = 30.0
PROGRESS_PROBABILITIES = ("confidence_floor", "choice_gap")
EVENT_KINDS = {"assessment", "invalidate", "restore", "review"}
DIFF_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--diff-algorithm=myers",
              "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", "--full-index")
GIT_FLAGS = ("-c", "core.quotePath=false", "-c", "core.abbrev=no")
DRIFT_CONFIG_KEYS = ("core.autocrlf", "core.eol", "core.symlinks")


class ProgressError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8", "surrogateescape")).hexdigest()


def assessment_key(stage_id: str, item_id: str, tree: str, attempt: int = 0) -> str:
    return fingerprint([stage_id, item_id, tree, attempt])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProgressError("INVALID_JSON", "Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ProgressError("INVALID_JSON", "Non-finite JSON number")


def _decode(text: str):
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError) as exc:
        raise ProgressError("INVALID_JSON", "Invalid JSON document") from exc


def read_json(path: Path):
    try:
        text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8-sig")
        return _decode(text)
    except (OSError, UnicodeError) as exc:
        raise ProgressError("INPUT_UNREADABLE", "Cannot read JSON input") from exc


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProgressError("INVALID_INPUT", field + " must be a non-empty string")
    return value.strip()


def _identifier(value, field: str) -> str:
    original = value
    value = _text(value, field)
    if original != value or not ID_RE.fullmatch(value):
        raise ProgressError("INVALID_INPUT", field + " must use lowercase letters, digits and underscores")
    return value


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ProgressError("INVALID_INPUT", "Missing or unexpected object fields")


def _sensitive(value) -> bool:
    import jev

    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    return bool(
        jev.SECRET_RE.search(text)
        or re.search(r"(?i)\bbearer\s+[a-z0-9._~+/-]{8,}", text)
        or re.search(r"(?i)TYPESAFE_API_KEY\s*[=:]\s*[a-z0-9_-]{8,}", text)
        or (key and key in text)
    )


def _safe_text(value, field: str) -> str:
    value = _text(value, field)
    if _sensitive(value):
        raise ProgressError("SENSITIVE_INPUT", "Credential-like input cannot be stored or sent")
    return value


def validate_progress_policy(policy: dict) -> None:
    if not isinstance(policy, dict):
        raise ProgressError("INVALID_POLICY", "Policy must be an object")
    if policy.get("endpoint") != "https://api.typesafe.ai/v1/systemone":
        raise ProgressError("INVALID_POLICY", "Progress requests may send credentials only to the TypeSafe API")
    if _sensitive(policy):
        raise ProgressError("INVALID_POLICY", "Policies must not embed credential-like values")
    _text(policy.get("model"), "model")
    settings = policy.get("progress")
    _fields(settings, (*PROGRESS_INTS, *PROGRESS_TIMES, *PROGRESS_PROBABILITIES, "points"),
            ("max_tokens", "harmful"))
    for key in PROGRESS_INTS:
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ProgressError("INVALID_POLICY", "progress." + key + " must be a positive integer")
    if "max_tokens" in settings and (type(settings["max_tokens"]) is not int or settings["max_tokens"] <= 0):
        raise ProgressError("INVALID_POLICY", "progress.max_tokens must be a positive integer")
    if "harmful" in settings and type(settings["harmful"]) is not bool:
        raise ProgressError("INVALID_POLICY", "progress.harmful opt-in must be a boolean")
    for key in (*PROGRESS_TIMES, *PROGRESS_PROBABILITIES):
        value = settings[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ProgressError("INVALID_POLICY", "progress." + key + " must be finite and positive")
        if key in PROGRESS_PROBABILITIES and value > 1:
            raise ProgressError("INVALID_POLICY", "progress." + key + " must be at most one")
    templates = policy.get("templates")
    if not isinstance(templates, dict):
        raise ProgressError("INVALID_POLICY", "Progress templates are required")
    for name in ("contribution", "progress_acceptance", "progress_review"):
        template = templates.get(name)
        _fields(template, ("type", "instructions", "criteria"))
        if template["type"] != "choice":
            raise ProgressError("INVALID_POLICY", name + " must be a choice")
        _text(template["instructions"], name + ".instructions")
        criteria = template["criteria"]
        if not isinstance(criteria, dict) or "none" not in criteria:
            raise ProgressError("INVALID_POLICY", name + " needs an insufficient-evidence hatch")
        for key, label in criteria.items():
            _identifier(key, "category")
            _text(label, "category meaning")
    if set(templates["progress_acceptance"]["criteria"]) != {"met", "unmet", "none"}:
        raise ProgressError("INVALID_POLICY", "Acceptance outcomes must be met, unmet or none")
    hard_max = policy.get("question_hard_max")
    if type(hard_max) is int and settings["max_items"] + 1 > hard_max:
        raise ProgressError("INVALID_POLICY", "progress.max_items must leave room for the review question")
    points = settings["points"]
    criteria = templates["contribution"]["criteria"]
    if not isinstance(points, dict) or set(points) != set(criteria) - {"none"} or len(points) < 2:
        raise ProgressError("INVALID_POLICY", "Point categories must match the contribution rubric")
    if "harmful" in points or "harmful" in criteria:
        if settings.get("harmful") is not True:
            raise ProgressError("INVALID_POLICY", "The harmful category requires an explicit progress.harmful opt-in")
        if type(points.get("harmful")) is not int or points["harmful"] >= 0:
            raise ProgressError("INVALID_POLICY", "The harmful category must carry a negative integer weight")
    if any(type(value) is not int or value < 0 for key, value in points.items() if key != "harmful"):
        raise ProgressError("INVALID_POLICY", "Contribution points must be nonnegative integers")
    if points.get("zero") != 0 or len(set(points.values())) != len(points):
        raise ProgressError("INVALID_POLICY", "Zero must be distinct and each category needs a distinct value")
    if set(templates["progress_review"]["criteria"]) != {"continue", "finish", "none"}:
        raise ProgressError("INVALID_POLICY", "Review directions are added from the frozen stage plan")


def lint_progress(policy: dict) -> list[dict]:
    try:
        validate_progress_policy(policy)
    except (ProgressError, TypeError, KeyError) as exc:
        return [{"rule": "P014", "severity": "error", "path": "progress", "message": str(exc), "fix": "Use valid progress limits, distinct integer points and matching Choice templates"}]
    return []


def _repo_root(entry) -> str:
    if not isinstance(entry, str) or not entry.strip() or "\x00" in entry:
        raise ProgressError("INVALID_PLAN", "Repository roots must be path strings")
    root = Path(entry).expanduser()
    if not root.is_absolute():
        raise ProgressError("INVALID_PLAN", "Repository roots must be absolute paths")
    return str(root.resolve())


def validate_plan(plan: dict, policy: dict) -> dict:
    validate_progress_policy(policy)
    _fields(plan, ("id", "goal", "checks", "required_checks", "items"), ("directions", "platform", "repos"))
    normalized = copy.deepcopy(plan)
    normalized["id"] = _identifier(plan["id"], "stage id")
    normalized["goal"] = _text(plan["goal"], "goal")
    checks = plan["checks"]
    settings = policy["progress"]
    if not isinstance(checks, dict) or not 1 <= len(checks) <= settings["max_checks"]:
        raise ProgressError("INVALID_PLAN", "A bounded set of verification commands is required")
    for name, argv in checks.items():
        _identifier(name, "check id")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv):
            raise ProgressError("INVALID_PLAN", "Verification commands must be non-empty argument arrays")
        if argv[0].lower().endswith((".bat", ".cmd")):
            raise ProgressError("INVALID_PLAN", "Use an executable rather than an implicitly shelled Windows batch file")
    used = set()

    def references(names):
        if not isinstance(names, list) or not names or not all(isinstance(name, str) and name in checks for name in names) or len(set(names)) != len(names):
            raise ProgressError("INVALID_PLAN", "Verification references must name distinct configured checks")
        used.update(names)

    references(plan["required_checks"])
    items = plan["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= settings["max_items"]:
        raise ProgressError("INVALID_PLAN", "A bounded set of agreed work items is required")
    ids = set()
    for item in items:
        _fields(item, ("id", "description", "checks", "paths"))
        item_id = _identifier(item["id"], "item id")
        if item_id in ids:
            raise ProgressError("INVALID_PLAN", "Work item IDs must be unique")
        ids.add(item_id)
        _text(item["description"], "item description")
        references(item["checks"])
        paths = item["paths"]
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path and not path.startswith("/") and not any(part == ".." for part in path.split("/")) and not any(char in path for char in (":", "\\", "\x00")) for path in paths):
            raise ProgressError("INVALID_PLAN", "Item paths must be literal repository-relative files or directories")
    if used != set(checks):
        raise ProgressError("INVALID_PLAN", "Every configured check must be used")
    directions = plan.get("directions", {})
    if not isinstance(directions, dict) or len(directions) + 3 > policy.get("choice_option_hard_max", 255):
        raise ProgressError("INVALID_PLAN", "Invalid set of preauthorized directions")
    for key, label in directions.items():
        _identifier(key, "direction id")
        _text(label, "direction description")
    normalized["directions"] = directions
    platform = plan.get("platform", "any")
    if platform not in ("any", "win32", "linux", "darwin"):
        raise ProgressError("INVALID_PLAN", "Unsupported platform requirement")
    normalized["platform"] = platform
    repos = plan.get("repos")
    if repos is not None:
        if not isinstance(repos, list) or not 1 <= len(repos) <= settings["max_items"]:
            raise ProgressError("INVALID_PLAN", "A bounded list of repository roots is required")
        normalized["repos"] = [_repo_root(entry) for entry in repos]
        if len({os.path.normcase(entry) for entry in normalized["repos"]}) != len(normalized["repos"]):
            raise ProgressError("INVALID_PLAN", "Repository roots must be distinct")
    if _sensitive(normalized):
        raise ProgressError("SENSITIVE_INPUT", "Stage plans must not contain credentials")
    return normalized


def _interpret_answer(answer: dict, policy: dict) -> dict:
    import jev

    choice = answer["choice"]
    confidence = answer["confidence"]
    probabilities = answer["probabilities"]
    settings = policy["progress"]
    audit = {"confidence": confidence, "observed_choice": choice, "probabilities": probabilities}
    if confidence < settings["confidence_floor"] or probabilities[choice] < max(probabilities.values()) or jev.top_two_gap(probabilities) < settings["choice_gap"]:
        return dict(audit, choice=None, reason="uncertain")
    if choice == "none":
        return dict(audit, choice=None, reason="insufficient_evidence")
    return dict(audit, choice=choice, reason="supported")


def interpret_choice(response: dict, name: str, question: dict, policy: dict) -> dict:
    import jev

    try:
        parsed = jev.validate_response(response, {name: question})
        return _interpret_answer(parsed["answers"][name], policy)
    except (SystemExit, ValueError, TypeError, KeyError):
        return {"choice": None, "confidence": None, "reason": "invalid_response"}


def _denied(reason: str, questions: dict, called: bool) -> dict:
    return {
        "called_jev": called, "model": None, "reason": reason, "usage": None,
        "results": {qid: {"choice": None, "confidence": None, "reason": reason} for qid in questions},
    }


def _ask(state: dict, questions: dict, policy: dict, asker=None) -> dict:
    import jev

    if _sensitive(state) or _sensitive(questions) or _sensitive(policy):
        return _denied("sensitive_evidence", questions, False)
    if len(canonical(state)) > policy["progress"]["max_evidence_chars"]:
        return _denied("evidence_too_large", questions, False)
    try:
        response = asker(state, questions, policy) if asker else jev.post_systemone(
            state, questions, policy, timeout=policy["progress"]["api_timeout_seconds"], retries=0
        )
        parsed = jev.validate_response(response, questions)
        results = {qid: _interpret_answer(parsed["answers"][qid], policy) for qid in questions}
        model = response.get("model") if isinstance(response, dict) else None
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            # validate_response tolerates extra usage fields (e.g.
            # total_tokens); the ledger's event schema does not — keep
            # only the two budget keys before anything persists it.
            usage = {
                key: usage[key]
                for key in ("input_tokens", "output_tokens")
                if key in usage
            }
        return {
            "called_jev": True,
            "model": model if isinstance(model, str) and not _sensitive(model) else None,
            "reason": None, "results": results,
            "usage": usage if isinstance(usage, dict) else None,
        }
    except SystemExit as exc:
        message = str(exc)
        reason = "invalid_response" if message.startswith("Jev response invalid:") else "sensitive_evidence" if message.startswith("Jev request blocked:") else "jev_unavailable"
        return _denied(reason, questions, True)
    except OSError:
        return _denied("jev_unavailable", questions, True)
    except (ValueError, TypeError, KeyError):
        return _denied("invalid_response", questions, True)


def _replay_metadata(evidence: dict) -> dict:
    metadata = copy.deepcopy(evidence)
    candidate = metadata.get("candidate")
    if isinstance(candidate, dict):
        candidate.pop("diff", None)
        if "checks" in candidate:
            candidate["checks"] = _clean_checks(candidate.get("checks"))
    baseline = metadata.get("baseline")
    if isinstance(baseline, dict) and "checks" in baseline:
        baseline["checks"] = _clean_checks(baseline.get("checks"))
    return metadata


def _review_questions(stage: dict) -> dict:
    question = copy.deepcopy(stage["policy"]["templates"]["progress_review"])
    for key, description in stage["plan"]["directions"].items():
        question["criteria"]["pivot_" + key] = "Close this stage without claiming completion and move to the preauthorized direction: " + description
    template = stage["policy"]["templates"]["progress_acceptance"]
    questions = {"progress_review": question}
    for item in stage["plan"]["items"]:
        item_question = copy.deepcopy(template)
        item_question["instructions"] = item_question["instructions"].replace("{item_id}", item["id"])
        questions["accept_" + item["id"]] = item_question
    return questions


class GitEvidence:
    def __init__(self, repo: Path, database: Path):
        self.repo = repo.resolve()
        self.database = database.resolve()

    @staticmethod
    def _git_env():
        return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}

    def _git(self, args, settings, allowed=(0,)):
        # command_timeout_seconds budgets the evidence commands, not git
        # plumbing: a worktree add or status on a slow host legitimately
        # takes longer and must not be killed at the evidence budget.
        try:
            git_timeout = float(settings.get("command_timeout_seconds"))
        except (TypeError, ValueError):
            git_timeout = 0.0
        git_timeout = max(git_timeout, GIT_COMMAND_MIN_SECONDS)
        if not math.isfinite(git_timeout):
            git_timeout = None
        try:
            result = subprocess.run(
                ["git", *GIT_FLAGS, "--no-pager", "-C", str(self.repo), *args], capture_output=True,
                timeout=git_timeout, check=False, env=self._git_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProgressError("GIT_UNAVAILABLE", "Git command could not complete") from exc
        if result.returncode not in allowed:
            raise ProgressError("GIT_ERROR", "Git could not verify repository state")
        return result.returncode, result.stdout.decode("utf-8", errors="surrogateescape")

    def snapshot(self, settings):
        _, root = self._git(["rev-parse", "--show-toplevel"], settings)
        if Path(root.strip()).resolve() != self.repo:
            raise ProgressError("REPO_ROOT_REQUIRED", "Use the repository root for progress tracking")
        exclusions = []
        try:
            relative = self.database.relative_to(self.repo).as_posix()
        except ValueError:
            relative = None
        if relative:
            tracked, _ = self._git(["ls-files", "--error-unmatch", "--", relative], settings, (0, 1))
            if tracked == 0:
                raise ProgressError("TRACKED_DATABASE", "The runtime progress database must not be tracked by Git")
            exclusions = [":(top,exclude,literal)" + relative + suffix for suffix in ("", "-journal", "-wal", "-shm", ".heads", ".heads.tmp")]
            exclusions.append(":(top,exclude)" + relative + ".heads.tmp.*")
        _, dirty = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", *exclusions], settings)
        if dirty:
            raise ProgressError("WORKTREE_DIRTY", "Commit the intended changes and resolve unrelated untracked files before assessment")
        _, revision = self._git(["rev-parse", "--verify", "HEAD^{commit}"], settings)
        _, tree = self._git(["rev-parse", "--verify", "HEAD^{tree}"], settings)
        config = {}
        for key in DRIFT_CONFIG_KEYS:
            _, config[key] = self._git(["config", "--get", key], settings, (0, 1))
        return {"revision": revision.strip(), "tree": tree.strip(), "platform": sys.platform,
                "config": {key: value.strip() for key, value in config.items()}}

    def descendant(self, baseline, revision, settings):
        code, _ = self._git(["merge-base", "--is-ancestor", baseline, revision], settings, (0, 1))
        return code == 0

    def diff(self, baseline, revision, settings, paths=None):
        scope = [":(top,literal)" + path for path in paths] if paths else []
        _, text = self._git(["diff", *DIFF_FLAGS, baseline, revision, "--", *scope], settings)
        return text

    @contextmanager
    def _worktree(self, revision, settings):
        path = Path(tempfile.mkdtemp(prefix="typesafe-verify-"))
        path.rmdir()
        try:
            self._git(["worktree", "add", "--detach", str(path), revision], settings)
            try:
                yield path
            finally:
                try:
                    self._git(["worktree", "remove", "--force", str(path)], settings)
                except ProgressError:
                    shutil.rmtree(path, ignore_errors=True)
        except ProgressError:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                self._git(["worktree", "prune"], settings, allowed=(0, 1))
            raise

    @staticmethod
    def _check_env():
        scrub = re.compile(r"(?i)(api[_-]?key|secret|token|password|credential)")
        return {key: value for key, value in os.environ.items()
                if not key.upper().startswith("GIT_") and not scrub.search(key)}

    def checks(self, definitions, names, settings, revision):
        results = []
        with self._worktree(revision, settings) as cwd:
            env = self._check_env()
            env["PYTHONPATH"] = str(cwd) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            for name in names:
                argv = list(definitions[name])
                if argv[0] == "{python}":
                    argv[0] = sys.executable
                timed_out = False
                exit_code = None
                with tempfile.TemporaryFile() as output:
                    try:
                        with subprocess.Popen(
                            argv, cwd=cwd, env=env, shell=False, stdout=output, stderr=subprocess.STDOUT,
                            start_new_session=os.name != "nt",
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                        ) as process:
                            try:
                                exit_code = process.wait(timeout=settings["command_timeout_seconds"])
                            except subprocess.TimeoutExpired:
                                timed_out = True
                                if os.name == "nt":
                                    killer = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
                                    try:
                                        subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=settings["command_timeout_seconds"], check=False)
                                    except (OSError, subprocess.TimeoutExpired):
                                        pass
                                else:
                                    try:
                                        os.killpg(process.pid, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                process.kill()
                                exit_code = process.wait()
                    except OSError:
                        exit_code = None
                    output.seek(0)
                    digest = hashlib.sha256()
                    size = 0
                    while block := output.read(65536):
                        digest.update(block)
                        size += len(block)
                results.append({"id": name, "exit_code": exit_code, "timed_out": timed_out, "output_sha256": digest.hexdigest(), "output_bytes": size})
        return results


def _passed(checks) -> bool:
    return bool(checks) and all(type(check.get("exit_code")) is int and check["exit_code"] == 0 and check.get("timed_out") is False for check in checks)


CHECK_FIELDS = ("id", "exit_code", "timed_out", "output_sha256", "output_bytes")
HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _check_record_ok(check) -> bool:
    return (
        isinstance(check, dict) and isinstance(check.get("id"), str)
        and (check.get("exit_code") is None or type(check["exit_code"]) is int)
        and type(check.get("timed_out")) is bool
        and isinstance(check.get("output_sha256"), str) and HEX64_RE.fullmatch(check["output_sha256"]) is not None
        and type(check.get("output_bytes")) is int and check["output_bytes"] >= 0
    )


def _clean_checks(checks):
    if not isinstance(checks, list):
        return checks
    return [{key: check[key] for key in CHECK_FIELDS if key in check} if isinstance(check, dict) else check for check in checks]


def _line_digest(path: str, text: str) -> str:
    return hashlib.sha256((path + "\x00" + text).encode("utf-8", "surrogateescape")).hexdigest()


STRUCTURAL_PREFIXES = ("new file mode", "deleted file mode", "old mode", "new mode",
                       "similarity index", "dissimilarity index", "rename from", "rename to",
                       "copy from", "copy to")
BINARY_MARKERS = ("Binary files", "GIT binary patch", "Submodule ")


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


_WS_RE = re.compile(r"\s+")


def _normalize_line(text: str) -> str:
    """Whitespace-insensitive view of a diff line: indentation churn and
    interior spacing collapse to a single form — except inside a string
    literal, where whitespace changes program behavior and stays literal.
    A best-effort line scanner (no multi-line/triple-quote awareness)."""
    out: list[str] = []
    in_quote = ""
    pending_ws = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif ch == in_quote:
                in_quote = ""
        elif ch in "\"'":
            if pending_ws and out:
                out.append(" ")
            pending_ws = False
            in_quote = ch
            out.append(ch)
        elif ch.isspace():
            pending_ws = True
        else:
            if pending_ws and out:
                out.append(" ")
            pending_ws = False
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _indent_width(text: str) -> int:
    """Leading-whitespace width with tabs expanded — the line's indent
    depth signature for indentation-sensitive languages."""
    leading = text[: len(text) - len(text.lstrip(" \t"))]
    return len(leading.expandtabs(8))


def _norm_digest(text: str, context: str = "") -> str:
    """Path-free digest of a normalized line bound to a normalized
    neighbor context (context + hunk position): pure reformats and
    renames keep it; a line relocated or reordered does not."""
    return hashlib.sha256(
        (context + "\x00" + _normalize_line(text)).encode("utf-8", "surrogateescape")
    ).hexdigest()


_OPS_PATH_RE = re.compile(r"[ab]/\S+")
_GIT_HEADER_RE = re.compile(r"diff --git a/(.+) b/(.+)$")


def _ops_norm(line: str) -> str:
    """Structural-op identity: full line with the a//b// path tokens
    stripped, so a rename keeps structural credit but a binary or blob
    replacement (a different index hash) still reads as a new change."""
    return _content_digest(_normalize_line(_OPS_PATH_RE.sub("", line)))


def _git_paths(header: str) -> tuple[str, str]:
    """(old, new) paths from a 'diff --git a/OLD b/NEW' header."""
    m = _GIT_HEADER_RE.match(header.strip())
    return (m.group(1), m.group(2)) if m else ("", "")


def _ops_norm_info(line: str, path: str) -> dict:
    """Norm record for a structural op: n binds the op text to its file
    (the same mode/index change on another path is a different change);
    f stays path-free for verified-move retention."""
    stripped = _OPS_PATH_RE.sub("", line)
    return {
        "n": _content_digest(path + "\x00" + _normalize_line(stripped)),
        "p": path,
        "f": _ops_norm(line),
    }


def _diff_line_hashes(diff: str) -> dict:
    added, removed, ops = [], [], []
    norms = {"added": {}, "removed": {}, "ops": {}}
    sections = {}
    old_file = new_file = header = ""
    block_old = block_new = ""
    deleted_paths: set = set()
    created_paths: set = set()
    in_hunk = saw_hunk = binary_section = False
    pending_index = []
    hunk_lines = []

    def flush_hunk():
        nonlocal hunk_lines
        context = fingerprint(sorted(text for kind, text in hunk_lines if kind == " "))
        norm_context = fingerprint(sorted(_normalize_line(text) for kind, text in hunk_lines if kind == " "))
        # Indentation is semantics in Python/YAML, not style: bind the
        # in-place digest to the line's indent depth relative to its hunk
        # context (counts of context lines shallower/equal/deeper). A dedent
        # out of a block changes the signature and loses credit; a uniform
        # reindent shifts line and context together and keeps it. Hunks with
        # no context lines stay indent-free — no structure is visible.
        ctx_indents = [
            _indent_width(text)
            for kind, text in hunk_lines
            # Blank context lines (including the split() artifact after a
            # trailing newline) carry no structural indent to compare.
            if kind == " " and text.strip(" \t")
        ]

        def indent_sig(text):
            if not ctx_indents:
                return ""
            width = _indent_width(text)
            return "\x00i:%d,%d,%d" % (
                sum(1 for c in ctx_indents if c < width),
                sum(1 for c in ctx_indents if c == width),
                sum(1 for c in ctx_indents if c > width),
            )

        for index, (kind, text) in enumerate(hunk_lines):
            if kind == "+":
                digest = _line_digest(new_file, context + "\x00" + str(index) + "\x00" + text)
                added.append(digest)
                norms["added"][digest] = {
                    "n": _norm_digest(text, new_file + "\x00" + norm_context + "\x00" + str(index) + indent_sig(text)),
                    "n0": _norm_digest(text, new_file + "\x00" + norm_context + "\x00" + str(index)),
                    "p": new_file, "f": _norm_digest(text),
                }
                sections.setdefault(new_file, []).append(
                    (digest, _content_digest(text), _norm_digest(text))
                )
            elif kind == "-":
                digest = _line_digest(old_file, context + "\x00" + str(index) + "\x00" + text)
                removed.append(digest)
                norms["removed"][digest] = {
                    "n": _norm_digest(text, old_file + "\x00" + norm_context + "\x00" + str(index) + indent_sig(text)),
                    "n0": _norm_digest(text, old_file + "\x00" + norm_context + "\x00" + str(index)),
                    "p": old_file, "f": _norm_digest(text),
                }
        hunk_lines = []

    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            flush_hunk()
            if binary_section:
                for digest, norm in pending_index:
                    ops.append(digest)
                    norms["ops"][digest] = norm
            pending_index = []
            binary_section = in_hunk = False
            old_file = new_file = ""
            block_old, block_new = _git_paths(line)
            header = line
        elif line.startswith("@@"):
            flush_hunk()
            in_hunk = saw_hunk = True
        elif in_hunk and line.startswith("+"):
            hunk_lines.append(("+", line[1:]))
        elif in_hunk and line.startswith("-"):
            hunk_lines.append(("-", line[1:]))
        elif in_hunk and line.startswith("\\"):
            continue
        elif in_hunk:
            hunk_lines.append((" ", line))
        elif line.startswith("--- "):
            target = line[4:]
            old_file = target[2:] if target.startswith("a/") else target
        elif line.startswith("+++ "):
            target = line[4:]
            new_file = target[2:] if target.startswith("b/") else target
        elif line.startswith("index "):
            pending_index.append(
                (_line_digest(header, line), _ops_norm_info(line, block_new or block_old))
            )
        elif line.startswith(BINARY_MARKERS):
            binary_section = True
            digest = _line_digest(header, line)
            ops.append(digest)
            norms["ops"][digest] = _ops_norm_info(line, block_new or block_old)
        elif line.startswith(STRUCTURAL_PREFIXES):
            digest = _line_digest(header, line)
            ops.append(digest)
            norms["ops"][digest] = _ops_norm_info(line, block_new or block_old)
            if line.startswith("deleted file mode"):
                deleted_paths.add(block_old)
            elif line.startswith("new file mode"):
                created_paths.add(block_new)
    flush_hunk()
    if binary_section:
        for digest, norm in pending_index:
            ops.append(digest)
            norms["ops"][digest] = norm
    if not saw_hunk:
        added, removed, ops = [], [], []
        norms = {"added": {}, "removed": {}, "ops": {}}
        sections = {}
        old_file = new_file = header = ""
        deleted_paths = set()
        created_paths = set()
        binary_section = False
        pending_index = []
        position = 0
        block_old = block_new = ""
        for line in diff.split("\n"):
            if line.startswith("diff --git"):
                if binary_section:
                    for digest, norm in pending_index:
                        ops.append(digest)
                        norms["ops"][digest] = norm
                pending_index = []
                binary_section = False
                old_file = new_file = ""
                block_old, block_new = _git_paths(line)
                header = line
                position = 0
            elif line.startswith("--- "):
                target = line[4:]
                old_file = target[2:] if target.startswith("a/") else target
            elif line.startswith("+++ "):
                target = line[4:]
                new_file = target[2:] if target.startswith("b/") else target
            elif line.startswith("+"):
                path = new_file or header
                digest = _line_digest(path, str(position) + "\x00" + line[1:])
                added.append(digest)
                norms["added"][digest] = {
                    "n": _norm_digest(line[1:], path + "\x00" + str(position)),
                    "p": path, "f": _norm_digest(line[1:]),
                }
                sections.setdefault(path, []).append(
                    (digest, _content_digest(line[1:]), _norm_digest(line[1:]))
                )
                position += 1
            elif line.startswith("-"):
                path = old_file or header
                digest = _line_digest(path, str(position) + "\x00" + line[1:])
                removed.append(digest)
                norms["removed"][digest] = {
                    "n": _norm_digest(line[1:], path + "\x00" + str(position)),
                    "p": path, "f": _norm_digest(line[1:]),
                }
                position += 1
            elif line.startswith("index "):
                pending_index.append(
                    (_line_digest(header, line), _ops_norm_info(line, block_new or block_old))
                )
            elif line.startswith(BINARY_MARKERS):
                binary_section = True
                digest = _line_digest(header, line)
                ops.append(digest)
                norms["ops"][digest] = _ops_norm_info(line, block_new or block_old)
            elif line.startswith(STRUCTURAL_PREFIXES):
                digest = _line_digest(header, line)
                ops.append(digest)
                norms["ops"][digest] = _ops_norm_info(line, block_new or block_old)
                if line.startswith("deleted file mode"):
                    deleted_paths.add(block_old)
                elif line.startswith("new file mode"):
                    created_paths.add(block_new)
        if binary_section:
            for digest, norm in pending_index:
                ops.append(digest)
                norms["ops"][digest] = norm
    return {
        "added": sorted(added), "removed": sorted(removed), "ops": sorted(ops),
        "added_norm": norms["added"], "removed_norm": norms["removed"],
        "ops_norm": norms["ops"],
        "sections": sections,
        "deleted": sorted(deleted_paths),
        "created": sorted(created_paths),
    }


def _move_destinations(wide: dict, path: str, credit: dict, current: dict = None) -> set:
    """Created paths plausibly holding the credited file's last-known
    content: every baseline line that survived the credited change plus
    the credited additions must appear together, and none of the lines
    the credit removed may resurface. Requires at least one uncredited
    carryover line — a one-line file's move is not provable this way.

    ``current`` is the (possibly path-scoped) parse of the same diff;
    the delete block for `path` is visible in both, so baseline lines
    are collected from either."""
    sources = [wide] + ([current] if current is not None else [])
    baseline = Counter()
    for src in sources:
        baseline |= Counter(
            info["f"]
            for info in src["removed_norm"].values()
            if isinstance(info, dict) and info.get("p") == path and info.get("f")
        )
    credited_removed = Counter(
        info["f"]
        for info in (credit.get("removed_norm") or {}).values()
        if isinstance(info, dict) and info.get("p") == path and info.get("f")
    )
    survivors = baseline - credited_removed
    if not survivors:
        return set()
    credited_added = Counter(
        info["f"]
        for info in (credit.get("added_norm") or {}).values()
        if isinstance(info, dict) and info.get("p") == path and info.get("f")
    )
    expected = survivors + credited_added
    out = set()
    for dest in wide["created"]:
        added_f = Counter(norm for _d, _c, norm in wide["sections"].get(dest, ()))
        if expected <= added_f and not any(credited_removed[f] for f in added_f):
            out.add(dest)
    return out


def _credited_retained(credit: dict, diff: str, wide_diff: str = None) -> bool:
    """Credited lines still present in the current diff — exactly by raw
    digest, by normalized in-place digest (reformats), or by pure-text
    digest after the credited file itself was deleted (the shape a move
    takes under --no-renames: delete + create). A file reverted to
    baseline is absent from the diff entirely and never qualifies.

    ``diff`` may be scoped to the credited item's paths; a delete block
    for the credited path is still visible there because the old side of
    a move matches the pathspec. ``wide_diff`` — the unscoped
    baseline->candidate diff — supplies the move destination scan so
    content moved to a path outside the item's scope is still found.

    Cross-path fallback only accepts a verified move: the credited file
    must be deleted in this diff, and a created path must contain the
    file's last-known content (uncredited baseline survivors plus the
    credited additions, with none of the credited removals reappearing).
    A single identical line elsewhere is not evidence of a move."""
    current = _diff_line_hashes(diff)
    wide = _diff_line_hashes(wide_diff) if wide_diff is not None else current
    deleted_now = set(current["deleted"]) | set(wide["deleted"])
    for key, norm_key in zip(CREDIT_RAW, CREDIT_NORM):
        needed = Counter(credit.get(key, ()))
        present = Counter(current[key])
        missing = {digest: count - present[digest]
                   for digest, count in needed.items() if present[digest] < count}
        if not missing:
            continue
        norms = credit.get(norm_key)
        if not isinstance(norms, dict):
            return False
        # Match current lines, not digests: a new-format line carries `n`
        # (indent-aware) and `n0` (indent-free). Credits written before
        # indent binding stored the unsigned digest under "n" with no "n0"
        # companion — resolve those against each line's "n0" channel. For
        # new records only the signed "n" channel counts, so a dedented
        # line cannot re-match through the indent-free digest.
        present_lines = [
            info
            for info in current[norm_key].values()
            if isinstance(info, dict)
        ]
        for digest, short in missing.items():
            info = norms.get(digest)
            if not isinstance(info, dict):
                return False
            want = info.get("n")
            line_key = "n" if "n0" in info else "n0"
            satisfied = 0
            for _ in range(short):
                hit = next(
                    (
                        line
                        for line in present_lines
                        if line.get(line_key) == want
                    ),
                    None,
                )
                if hit is None:
                    break
                present_lines.remove(hit)
                satisfied += 1
            if satisfied >= short:
                continue
            path = info.get("p")
            if not path or path not in deleted_now or not info.get("f"):
                return False
            if key == "removed":
                # The file's deletion removes the line anew only when a
                # verified move destination still carries the file's
                # remaining content; a file that is simply gone takes the
                # removal credit with it. A destination that reintroduces
                # the credited text is disqualified inside _move_destinations.
                if not _move_destinations(wide, path, credit, current):
                    return False
                continue
            destinations = _move_destinations(wide, path, credit, current)
            pool = Counter(
                dst["f"]
                for dst in wide[norm_key].values()
                if isinstance(dst, dict) and dst.get("p") in destinations
            )
            if pool[info["f"]] < short - satisfied:
                return False
    return True


def _chain_head(stage_id: str, head, seal=None) -> str:
    return fingerprint({"stage": stage_id, "previous": head, "seal": seal})


def _active_credits(events, before_sequence=None):
    active = {}
    for event in events:
        if before_sequence is not None and event["sequence"] >= before_sequence:
            continue
        data = event["data"]
        if event["kind"] == "assessment" and data.get("status") == "scored" and data.get("points", 0) > 0 and isinstance(data.get("credit"), dict):
            active.setdefault(data["item_id"], data["credit"])
        elif event["kind"] == "invalidate":
            active.pop(data["item_id"], None)
        elif event["kind"] == "restore" and isinstance(data.get("credit"), dict):
            active[data["item_id"]] = data["credit"]
    return active


CREDIT_RAW = ("added", "removed", "ops")
CREDIT_NORM = ("added_norm", "removed_norm", "ops_norm")
CREDIT_SECTS = ("sections", "sections_norm")


def _credited_union(events, before_sequence=None):
    total = {key: Counter() for key in CREDIT_RAW}
    for key in CREDIT_NORM:
        total[key] = {}  # raw digest -> {n, p, f}; last writer wins
    for credit in _active_credits(events, before_sequence).values():
        for key in CREDIT_RAW:
            total[key].update(credit.get(key, ()))
        for key in CREDIT_NORM:
            norms = credit.get(key)
            if isinstance(norms, dict):
                total[key].update(norms)
    return total


def _credited_sections(events, before_sequence=None):
    """Prior credited sections as (content digests, norm digests) pairs
    per path — old credits carry only the content list."""
    sections = []
    for credit in _active_credits(events, before_sequence).values():
        contents = credit.get("sections")
        if not isinstance(contents, dict):
            continue
        norms = credit.get("sections_norm")
        norms = norms if isinstance(norms, dict) else {}
        for path, lines in contents.items():
            sections.append((lines, norms.get(path, [])))
    return sections


def _suppress_norm_dupes(digests, norms_now, credited_norms):
    """Drop earned digests that repeat a line already credited in the same
    file: a normalized (path, content) match means reformats and in-file
    moves of a credited line earn nothing new. A copy into a different
    path still earns — it is new content placement, not a reformat."""
    cfp = Counter(
        (info["p"], info["f"])
        for info in credited_norms.values()
        if isinstance(info, dict) and info.get("p") and info.get("f")
    )
    kept = []
    for digest in digests:
        info = norms_now.get(digest)
        if not isinstance(info, dict):
            kept.append(digest)
            continue
        key = (info.get("p"), info.get("f"))
        if info.get("p") and info.get("f") and cfp[key] > 0:
            cfp[key] -= 1
            continue
        kept.append(digest)
    return kept


def _earned_credit(raw, credited, credited_sections):
    """Credit a diff earns now: raw line digests minus the credited union,
    minus credited sections blindly re-added (content digest, or its
    normalized digest when the section was reformatted), minus credited
    norm-equivalents. The *_norm lists stored alongside are the retention
    channel used by _credited_retained."""
    added = Counter(raw["added"]) - credited["added"]
    removed = Counter(raw["removed"]) - credited["removed"]
    ops = Counter(raw["ops"]) - credited["ops"]
    counts = {"added": added, "removed": removed, "ops": ops}
    for kind, norm_key in zip(CREDIT_RAW, CREDIT_NORM):
        norms_now = raw.get(norm_key) or {}
        credited_norms = credited.get(norm_key) or {}
        if isinstance(norms_now, dict) and isinstance(credited_norms, dict):
            counts[kind] = Counter(
                _suppress_norm_dupes(
                    list(counts[kind].elements()), norms_now, credited_norms
                )
            )
    added, removed, ops = counts["added"], counts["removed"], counts["ops"]
    for pairs in raw["sections"].values():
        blind = Counter(content for _, content, _n in pairs)
        blind_norm = Counter(_n for _, _c, _n in pairs)
        for prior, prior_norm in credited_sections:
            needed = Counter(prior)
            needed_norm = Counter(prior_norm)
            if sum(needed.values()) >= 2 and needed <= blind:
                channel = 1
            elif sum(needed_norm.values()) >= 2 and needed_norm <= blind_norm:
                channel = 2
            else:
                continue
            use = needed if channel == 1 else needed_norm
            remaining = use.copy()
            for digest, content, norm in pairs:
                key = content if channel == 1 else norm
                if remaining[key] > 0 and added[digest] > 0:
                    remaining[key] -= 1
                    added[digest] -= 1
            if channel == 1:
                blind -= needed
            else:
                blind_norm -= needed_norm
    credit = {
        "added": sorted(added.elements()),
        "removed": sorted(removed.elements()),
        "ops": sorted(ops.elements()),
    }
    for kind, norm_key in zip(CREDIT_RAW, CREDIT_NORM):
        keep = Counter(credit[kind])
        norms = {}
        for digest, info in raw[norm_key].items():
            if keep[digest] > 0:
                keep[digest] -= 1
                norms[digest] = info
        credit[norm_key] = norms
    sections = {}
    sections_norm = {}
    remaining = Counter(credit["added"])
    for path, pairs in raw["sections"].items():
        keep, keep_norm = [], []
        for digest, content, norm in pairs:
            if remaining[digest] > 0:
                remaining[digest] -= 1
                keep.append(content)
                keep_norm.append(norm)
        if keep:
            sections[path] = sorted(keep)
            sections_norm[path] = sorted(keep_norm)
    if sections:
        credit["sections"] = sections
        credit["sections_norm"] = sections_norm
    return credit


def _align_credit_shape(stored, rebuilt):
    """Drop norm channels from a rebuilt credit when the stored credit
    predates them — old ledgers keep validating under the new code."""
    if isinstance(stored, dict) and "added_norm" not in stored:
        rebuilt = {
            key: value
            for key, value in rebuilt.items()
            if key not in CREDIT_NORM + CREDIT_SECTS[1:]
        }
    return rebuilt


def _credit_overlaps(credit, union) -> bool:
    if any(Counter(credit.get(key, ())) & union[key] for key in CREDIT_RAW):
        return True
    return any(
        set(credit.get(key) or ()) & set(union.get(key) or ())
        for key in CREDIT_NORM
    )


def _credit_ok(credit) -> bool:
    if not isinstance(credit, dict) or not set(credit) - set(CREDIT_SECTS) - set(CREDIT_NORM) == set(CREDIT_RAW):
        return False
    lines_ok = all(
        isinstance(credit[key], list) and all(type(line) is str and HEX64_RE.fullmatch(line) is not None for line in credit[key])
        for key in CREDIT_RAW
    )
    norms_ok = all(
        credit.get(key) is None or (
            isinstance(credit[key], dict)
            and all(
                type(digest) is str and HEX64_RE.fullmatch(digest) is not None
                and isinstance(info, dict) and set(info) <= {"n", "n0", "p", "f"}
                and type(info.get("n")) is str and HEX64_RE.fullmatch(info["n"]) is not None
                and (info.get("n0") is None or (
                    type(info.get("n0")) is str
                    and HEX64_RE.fullmatch(info["n0"]) is not None
                ))
                and type(info.get("p")) is str
                and type(info.get("f")) is str and HEX64_RE.fullmatch(info["f"]) is not None
                for digest, info in credit[key].items()
            )
        )
        for key in CREDIT_NORM
    )
    sections_ok = all(
        isinstance(credit.get(key, {}), dict)
        and all(
            isinstance(path, str) and isinstance(lines, list)
            and all(type(line) is str and HEX64_RE.fullmatch(line) is not None for line in lines)
            for path, lines in credit.get(key, {}).items()
        )
        for key in CREDIT_SECTS
    )
    return lines_ok and norms_ok and sections_ok


def _allowed_repos(stage: dict) -> frozenset:
    """Canonical repository roots the stored stage may be operated from."""
    repos = stage["plan"].get("repos")
    if repos is None:
        repos = [stage["repo"]]
    if not isinstance(repos, list) or not repos or not all(
            isinstance(entry, str) and entry for entry in repos):
        raise ProgressError("STORE_INVALID", "Stored stage carries an inconsistent repository list")
    return frozenset(os.path.normcase(entry) for entry in repos)


def _validate_event(stage, event):
    try:
        kind, data = event["kind"], event["data"]
        items = {item["id"] for item in stage["plan"]["items"]}
        if kind not in EVENT_KINDS or not isinstance(data, dict):
            raise ValueError()
        if kind != "review" and data["item_id"] not in items:
            raise ValueError()
        if type(data.get("called_jev", False)) is not bool:
            raise ValueError()
        if "repo" in data and (
                not isinstance(data["repo"], str)
                or os.path.normcase(data["repo"]) not in _allowed_repos(stage)):
            raise ValueError()
        usage = data.get("usage")
        if usage is not None and (
            not isinstance(usage, dict)
            or set(usage) - {"input_tokens", "output_tokens"}
            or any(
                type(usage.get(key)) is not int or usage[key] < 0
                for key in ("input_tokens", "output_tokens")
            )
        ):
            raise ValueError()
        for key in ("revision", "tree"):
            if key in data and (not isinstance(data[key], str) or REVISION_RE.fullmatch(data[key]) is None):
                raise ValueError()
        if "platform" in data and not isinstance(data["platform"], str):
            raise ValueError()
        checks = data.get("checks", [])
        if not isinstance(checks, list):
            raise ValueError()
        for check in checks:
            if not _check_record_ok(check) or check["id"] not in stage["plan"]["checks"]:
                raise ValueError()
        if kind in ("assessment", "review", "restore") and (not isinstance(data.get("evidence_sha256"), str) or HEX64_RE.fullmatch(data["evidence_sha256"]) is None):
            raise ValueError()
        if kind in ("assessment", "restore") and (not isinstance(data.get("change_sha256"), str) or HEX64_RE.fullmatch(data["change_sha256"]) is None):
            raise ValueError()
        if kind == "assessment":
            points = stage["policy"]["progress"]["points"]
            if type(data["points"]) is not int or data["status"] not in ("scored", "unscored"):
                raise ValueError()
            expected = points[data["level"]] if data["status"] == "scored" else 0
            if data["points"] != expected or (data["status"] == "unscored" and data["level"] is not None):
                raise ValueError()
            key = assessment_key(stage["plan"]["id"], data["item_id"], data["tree"])
            if type(data.get("attempt")) is not int or data["attempt"] < 1:
                raise ValueError()
            if data.get("assessment_key") != key or data["id"] != assessment_key(stage["plan"]["id"], data["item_id"], data["tree"], data["attempt"]):
                raise ValueError()
            item = next(entry for entry in stage["plan"]["items"] if entry["id"] == data["item_id"])
            if len(checks) != len({check["id"] for check in checks}) or {check["id"] for check in checks} != set(stage["plan"]["required_checks"]) | set(item["checks"]):
                raise ValueError()
            credit = data.get("credit")
            if credit is not None and not _credit_ok(credit):
                raise ValueError()
            if data["points"] > 0 and (credit is None or not isinstance(data.get("evidence_replay"), dict)):
                raise ValueError()
        if kind == "review":
            allowed = {"continue", "finish", None} | {"pivot_" + key for key in stage["plan"]["directions"]}
            if type(data["applied"]) is not bool or data["choice"] not in allowed or (data["applied"] and data["choice"] is None):
                raise ValueError()
            if type(data.get("approve_finish")) is not bool or (not data["applied"] and not isinstance(data.get("reason"), str)):
                raise ValueError()
            if "over_budget" in data and type(data["over_budget"]) is not bool:
                raise ValueError()
            if len(checks) != len({check["id"] for check in checks}) or {check["id"] for check in checks} != set(stage["plan"]["checks"]):
                raise ValueError()
            acceptance = data["item_acceptance"]
            if not isinstance(acceptance, dict) or set(acceptance) != items:
                raise ValueError()
            for entry in acceptance.values():
                if not isinstance(entry, dict) or entry.get("status") not in ("met", "unmet", "unknown"):
                    raise ValueError()
        if kind == "restore":
            item = next(entry for entry in stage["plan"]["items"] if entry["id"] == data["item_id"])
            if len(checks) != len({check["id"] for check in checks}) or {check["id"] for check in checks} != set(stage["plan"]["required_checks"]) | set(item["checks"]):
                raise ValueError()
            if not isinstance(data.get("restores"), str) or HEX64_RE.fullmatch(data["restores"]) is None:
                raise ValueError()
            if not _credit_ok(data.get("credit")):
                raise ValueError()
        replay = data.get("evidence_replay")
        if replay is not None:
            if not isinstance(replay, dict) or not isinstance(replay.get("metadata"), dict):
                raise ValueError()
            meta = replay["metadata"]
            candidate, baseline = meta.get("candidate"), meta.get("baseline")
            if not isinstance(candidate, dict) or "diff" in candidate or candidate.get("checks") != checks:
                raise ValueError()
            if (candidate.get("revision") != data.get("revision") or candidate.get("tree") != data.get("tree")
                    or candidate.get("platform") != data.get("platform") or candidate.get("config") != data.get("config")
                    or meta.get("goal") != stage["plan"]["goal"]):
                raise ValueError()
            if kind in ("assessment", "restore"):
                item = next(entry for entry in stage["plan"]["items"] if entry["id"] == data["item_id"])
                if not isinstance(baseline, dict) or baseline.get("revision") != stage["baseline"]["revision"] or meta.get("item") != item:
                    raise ValueError()
                if kind == "restore" and (not _credit_ok(meta.get("credit")) or meta["credit"] != data["credit"]):
                    raise ValueError()
            if kind == "review":
                required = stage["plan"]["required_checks"]
                if (meta.get("items") != stage["plan"]["items"] or meta.get("completion_approval") != data["approve_finish"]
                        or meta.get("review_context") != data.get("review_context")
                        or meta.get("all_checks_passed") != _passed(candidate["checks"])
                        or meta.get("required_checks_passed") != _passed([check for check in candidate["checks"] if check["id"] in required])):
                    raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise ProgressError("STORE_INVALID", "Stored event is inconsistent with the frozen stage") from exc


def _summarize(stage: dict, events: list[dict]) -> dict:
    settings = stage["policy"]["progress"]
    awards = {}
    award_ids = {}
    award_credit = {}
    assessed = set()
    invalidated = set()
    active_credit = {}
    failed_checks = set()
    assessments = 0
    model_attempts = 0
    no_gain = 0
    unscored_streak = 0
    review_at = settings["review_points"]
    terminal = None
    next_direction = None
    unresolved_review = None
    bypassed = False
    penalties = {}
    tokens_used = 0
    for event in events:
        _validate_event(stage, event)
        kind, data = event["kind"], event["data"]
        attempts_before = model_attempts
        tokens_before = tokens_used
        model_attempts += int(data.get("called_jev") is True)
        usage = data.get("usage")
        if isinstance(usage, dict):
            tokens_used += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        for check in data.get("checks", []):
            if _passed([check]):
                failed_checks.discard(check["id"])
            else:
                failed_checks.add(check["id"])
        if kind == "assessment":
            assessments += 1
            assessed.add(data["item_id"])
            if data["status"] == "scored" and data["points"] > 0:
                awards.setdefault(data["item_id"], data["points"])
                award_ids.setdefault(data["item_id"], data["id"])
                award_credit.setdefault(data["item_id"], data["credit"])
                if data["item_id"] not in invalidated:
                    active_credit.setdefault(data["item_id"], data["credit"])
                penalties.pop(data["item_id"], None)
                no_gain = unscored_streak = 0
            else:
                if data["status"] == "scored" and data["points"] < 0:
                    penalties[data["item_id"]] = data["points"]
                no_gain += 1
                unscored_streak = unscored_streak + 1 if data["status"] == "unscored" else 0
        elif kind == "invalidate":
            if data["item_id"] not in awards or data["item_id"] in invalidated:
                raise ProgressError("STORE_INVALID", "Invalidation requires a credited, unrestored outcome")
            invalidated.add(data["item_id"])
            active_credit.pop(data["item_id"], None)
        elif kind == "restore":
            union = {key: Counter() for key in CREDIT_RAW}
            for key in CREDIT_NORM:
                union[key] = set()
            for item_id, credit in active_credit.items():
                for key in CREDIT_RAW:
                    union[key].update(credit.get(key, ()))
                for key in CREDIT_NORM:
                    norms = credit.get(key)
                    if isinstance(norms, dict):
                        union[key].update(norms)
            if (data["item_id"] not in invalidated or data.get("restores") != award_ids.get(data["item_id"])
                    or data.get("credit") != award_credit.get(data["item_id"])
                    or _credit_overlaps(data.get("credit") or {}, union)):
                raise ProgressError("STORE_INVALID", "Restoration requires a previously invalidated, still-unclaimed outcome")
            invalidated.discard(data["item_id"])
            active_credit[data["item_id"]] = data["credit"]
        elif kind == "review":
            token_limit = settings.get("max_tokens")
            if data.get("over_budget") is True:
                exhausted = attempts_before >= settings["max_model_calls"] or (
                    type(token_limit) is int and tokens_before >= token_limit
                )
                if bypassed or not exhausted or data["approve_finish"] is not True:
                    raise ProgressError("STORE_INVALID", "Over-budget review bypass is allowed once and requires finish approval")
                bypassed = True
            if data["applied"]:
                if not isinstance(data.get("evidence_replay"), dict):
                    raise ProgressError("STORE_INVALID", "An applied review requires replayable evidence")
                if data["choice"] == "continue":
                    running = (
                        sum(value for item, value in awards.items() if item not in invalidated)
                        + sum(penalties.values())
                    )
                    if type(data.get("review_at")) is not int or data["review_at"] != running + settings["review_points"]:
                        raise ProgressError("STORE_INVALID", "Applied review is inconsistent with recorded history")
                    review_at = data["review_at"]
                elif data["choice"] == "finish":
                    if (data["approve_finish"] is not True or not _passed(data["checks"]) or invalidated or failed_checks
                            or any(entry["status"] != "met" for entry in data["item_acceptance"].values())
                            or any(item["id"] not in assessed for item in stage["plan"]["items"])):
                        raise ProgressError("STORE_INVALID", "Applied finish lacks approval, passing checks, accepted items or per-item evidence")
                    terminal = "finished"
                elif data["choice"].startswith("pivot_"):
                    required = stage["plan"]["required_checks"]
                    if not _passed([check for check in data["checks"] if check["id"] in required]) or invalidated:
                        raise ProgressError("STORE_INVALID", "Applied pivot lacks passing required checks or leaves blocked items")
                    terminal = "pivoted"
                    next_direction = data["choice"][len("pivot_"):]
                unresolved_review = None
                no_gain = unscored_streak = 0
            else:
                unresolved_review = data["reason"]
    points = sum(value for item, value in awards.items() if item not in invalidated) + sum(penalties.values())
    token_limit = settings.get("max_tokens")
    if terminal:
        action, reason = terminal, terminal
    elif model_attempts >= settings["max_model_calls"] or assessments >= settings["max_assessments"] or (
            type(token_limit) is int and tokens_used >= token_limit):
        action = "budget_exhausted"
        reason = (
            "token_budget"
            if type(token_limit) is int and tokens_used >= token_limit
            and model_attempts < settings["max_model_calls"] and assessments < settings["max_assessments"]
            else "iteration_budget"
        )
    elif invalidated or failed_checks:
        action, reason = "repair_required", "invalidated" if invalidated else "checks_failed"
    elif points >= review_at:
        action, reason = "review_required", "checkpoint"
    elif no_gain >= settings["stall_limit"]:
        action, reason = "review_required", "unscored" if unscored_streak >= settings["stall_limit"] else "stalled"
    elif unresolved_review:
        action, reason = "review_required", unresolved_review
    elif len(awards) == len(stage["plan"]["items"]):
        action, reason = "review_required", "all_items_credited"
    else:
        action, reason = "continue", "active"
    return {
        "stage_id": stage["plan"]["id"], "goal": stage["plan"]["goal"],
        "points": points, "review_at": review_at,
        "assessment_count": assessments, "assessment_limit": settings["max_assessments"],
        "model_attempts": model_attempts, "model_attempt_limit": settings["max_model_calls"],
        "tokens_used": tokens_used, "token_limit": token_limit if type(token_limit) is int else None,
        "harmful_items": sorted(penalties),
        "awarded_items": sorted(item for item in awards if item not in invalidated),
        "blocked_items": sorted(invalidated), "failed_checks": sorted(failed_checks),
        "action": action, "reason": reason, "next_direction": next_direction,
        "rubric_version": settings["rubric_version"], "policy_hash": stage["policy_hash"],
        "max_possible_points": len(stage["plan"]["items"]) * max(settings["points"].values()),
    }


class Ledger:
    # Serializes same-process writers: BEGIN IMMEDIATE already serializes
    # across processes, but the anchor file is written just after commit —
    # without this lock a same-process writer can observe the lag.
    _txn_lock = threading.Lock()

    def __init__(self, path: Path, repo: Path, *, evidence=None):
        self.path = Path(path).resolve()
        self.repo = Path(repo).resolve()
        self.collector = evidence or GitEvidence(self.repo, self.path)

    @contextmanager
    def _database(self, *, create=False, settings=None):
        connection = None
        try:
            if not self.path.exists():
                if not create:
                    raise ProgressError("STORE_MISSING", "Initialize a progress stage before using its ledger")
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            kwargs = {"timeout": settings["database_timeout_seconds"]} if settings else {}
            connection = sqlite3.connect(self.path, isolation_level=None, **kwargs)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchone():
                        raise ProgressError("SCHEMA_VERSION", "Refusing to modify an unrelated SQLite database")
                    connection.execute("CREATE TABLE stages (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
                    connection.execute("CREATE TABLE heads (stage_id TEXT PRIMARY KEY REFERENCES stages(id), seal TEXT NOT NULL)")
                    connection.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, stage_id TEXT NOT NULL REFERENCES stages(id), kind TEXT NOT NULL, document TEXT NOT NULL, dedupe_key TEXT, seal TEXT, UNIQUE(stage_id, dedupe_key))")
                    connection.execute("CREATE TRIGGER stages_no_update BEFORE UPDATE ON stages BEGIN SELECT RAISE(ABORT, 'immutable stage'); END")
                    connection.execute("CREATE TRIGGER stages_no_delete BEFORE DELETE ON stages BEGIN SELECT RAISE(ABORT, 'immutable stage'); END")
                    connection.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
                    connection.execute("CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
                    connection.execute("PRAGMA user_version = 4")
                    version = SCHEMA_VERSION
                connection.commit()
            if version != SCHEMA_VERSION:
                raise ProgressError("SCHEMA_VERSION", "Unsupported progress database schema")
            yield connection
        except sqlite3.OperationalError as exc:
            raise ProgressError("STORE_BUSY" if "locked" in str(exc).lower() else "STORE_ERROR", "Progress store is busy or unavailable; no successful write is claimed") from exc
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ProgressError("STORE_ERROR", "Progress store could not be read or written") from exc
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _anchor_path(self):
        return Path(str(self.path) + ".heads")

    def _read_anchor(self):
        # os.replace on Windows denies a read that lands mid-rename; a short
        # bounded retry turns that transient into the intended ordering.
        anchor = None
        last_exc = None
        for attempt in range(4):
            try:
                anchor = _decode(self._anchor_path().read_text(encoding="utf-8"))
                break
            except PermissionError as exc:
                last_exc = exc
                if attempt == 3:
                    raise ProgressError("STORE_INVALID", "The progress chain anchor file is missing or unreadable") from exc
                time.sleep(0.05 * (attempt + 1))
            except (OSError, ProgressError) as exc:
                raise ProgressError("STORE_INVALID", "The progress chain anchor file is missing or unreadable") from exc
        if anchor is None and last_exc is None:
            raise ProgressError("STORE_INVALID", "The progress chain anchor file is missing or unreadable")
        if not isinstance(anchor, dict) or not all(
                isinstance(stage_id, str) and isinstance(seal, str) and HEX64_RE.fullmatch(seal) is not None
                for stage_id, seal in anchor.items()):
            raise ProgressError("STORE_INVALID", "The progress chain anchor file is inconsistent")
        return anchor

    def _write_anchor(self, db):
        anchor = {row["stage_id"]: row["seal"] for row in db.execute("SELECT stage_id, seal FROM heads")}
        target = self._anchor_path()
        # Unique tmp name: callers can run on several threads in one process,
        # and Windows refuses to unlink a file another thread still holds open.
        temp = target.with_name(
            "%s.tmp.%d.%d" % (target.name, os.getpid(), threading.get_ident())
        )
        try:
            temp.unlink(missing_ok=True)
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(canonical(anchor))
            # Windows denies the rename while a concurrent reader holds the
            # target open — retry like _read_anchor does.
            _watch.atomic_replace(temp, target, attempts=4)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProgressError("STORE_ERROR", "The progress chain anchor could not be written") from exc

    def _load(self, db, stage_id, *, member=True):
        row = db.execute("SELECT document FROM stages WHERE id = ?", (stage_id,)).fetchone()
        if row is None:
            raise ProgressError("STAGE_MISSING", "Unknown progress stage")
        stage = _decode(row["document"])
        try:
            validate_progress_policy(stage["policy"])
            if stage["policy_hash"] != fingerprint(stage["policy"]) or stage["plan_hash"] != fingerprint(stage["plan"]):
                raise ProgressError("STORE_INVALID", "Frozen stage data no longer matches its fingerprint")
            allowed = _allowed_repos(stage)
            if os.path.normcase(stage["repo"]) not in allowed or (
                    member and os.path.normcase(str(self.repo)) not in allowed):
                raise ProgressError("REPO_MISMATCH", "This ledger stage belongs to another repository path")
        except (KeyError, TypeError) as exc:
            raise ProgressError("STORE_INVALID", "Invalid stored stage") from exc
        return stage

    def _events(self, db, stage, *, committed=True):
        result = []
        stage_id = stage["plan"]["id"]
        head_row = db.execute("SELECT seal FROM heads WHERE stage_id = ?", (stage_id,)).fetchone()
        if head_row is None:
            raise ProgressError("STORE_INVALID", "The event chain anchor is missing")
        previous = None
        head = _chain_head(stage_id, fingerprint(stage))
        sequence_floor = 0
        for row in db.execute("SELECT sequence, stage_id, kind, document, dedupe_key, seal FROM events WHERE stage_id = ? ORDER BY sequence", (stage_id,)):
            data = _decode(row["document"])
            expected = fingerprint({"previous": previous, "stage_id": row["stage_id"], "kind": row["kind"],
                                    "document": data, "sequence": row["sequence"], "dedupe": row["dedupe_key"]})
            if (row["kind"] not in EVENT_KINDS or not isinstance(data, dict) or row["seal"] != expected
                    or type(row["sequence"]) is not int or row["sequence"] <= sequence_floor):
                raise ProgressError("STORE_INVALID", "Stored event chain is inconsistent")
            sequence_floor = row["sequence"]
            previous = row["seal"]
            head = _chain_head(stage_id, head, row["seal"])
            event = {"sequence": row["sequence"], "kind": row["kind"], "data": data, "seal": row["seal"]}
            _validate_event(stage, event)
            result.append(event)
        if head_row["seal"] != head:
            raise ProgressError("STORE_INVALID", "Stored event chain is inconsistent")
        if committed:
            # A cross-process writer commits before its anchor file lands;
            # retry briefly so a lagging anchor is not misread as corruption.
            for attempt in range(4):
                if self._read_anchor().get(stage_id) == head:
                    break
                time.sleep(0.05 * (attempt + 1))
            else:
                raise ProgressError("STORE_INVALID", "Stored event chain is inconsistent")
        return result

    def _append(self, db, stage_id, kind, data, dedupe=None):
        payload = dict(data, repo=str(self.repo), recorded_at=time.time())
        row = db.execute("SELECT seal FROM events WHERE stage_id = ? ORDER BY sequence DESC LIMIT 1", (stage_id,)).fetchone()
        sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events").fetchone()[0]
        seal = fingerprint({"previous": row["seal"] if row else None, "stage_id": stage_id, "kind": kind,
                            "document": payload, "sequence": sequence, "dedupe": dedupe})
        head_row = db.execute("SELECT seal FROM heads WHERE stage_id = ?", (stage_id,)).fetchone()
        db.execute(
            "INSERT INTO events(sequence, stage_id, kind, document, dedupe_key, seal) VALUES (?, ?, ?, ?, ?, ?)",
            (sequence, stage_id, kind, canonical(payload), dedupe, seal),
        )
        db.execute(
            "UPDATE heads SET seal = ? WHERE stage_id = ?",
            (_chain_head(stage_id, head_row["seal"] if head_row else None, seal), stage_id),
        )

    @contextmanager
    def _transaction(self, stage_id):
        with self._txn_lock:
            with self._database() as db:
                stage = self._load(db, stage_id)
            with self._database(settings=stage["policy"]["progress"]) as db:
                db.execute("BEGIN IMMEDIATE")
                stage = self._load(db, stage_id)
                events = self._events(db, stage)
                yield db, stage, events
                db.commit()
                self._write_anchor(db)

    def _snapshot(self, stage):
        settings = stage["policy"]["progress"]
        snapshot = self.collector.snapshot(settings)
        if not isinstance(snapshot, dict) or not all(isinstance(snapshot.get(key), str) and REVISION_RE.fullmatch(snapshot[key]) for key in ("revision", "tree")):
            raise ProgressError("INVALID_EVIDENCE", "A committed Git revision and tree are required")
        platform = stage["plan"]["platform"]
        if platform != "any" and snapshot.get("platform") != platform:
            raise ProgressError("PLATFORM_MISMATCH", "Verification must run on the stage's required operating system")
        baseline = stage.get("baseline")
        if baseline and not self.collector.descendant(baseline["revision"], snapshot["revision"], settings):
            raise ProgressError("HISTORY_CHANGED", "Candidate revision must descend from the frozen stage baseline")
        if baseline and baseline.get("config") != snapshot.get("config"):
            raise ProgressError("CONFIG_DRIFT", "Verification-affecting Git configuration changed since the stage baseline")
        return snapshot

    def _verify(self, stage, names, before):
        settings = stage["policy"]["progress"]
        checks = self.collector.checks(stage["plan"]["checks"], sorted(set(names)), settings, before["revision"])
        if (not isinstance(checks, list) or len(checks) != len(set(names)) or not all(_check_record_ok(check) for check in checks)
                or {check["id"] for check in checks} != set(names)):
            raise ProgressError("INVALID_EVIDENCE", "Verifier results do not cover the required checks")
        if self._snapshot(stage) != before:
            raise ProgressError("WORKTREE_CHANGED", "Repository state changed while verification was running")
        return _clean_checks(checks)

    @staticmethod
    def _item(stage, item_id):
        for item in stage["plan"]["items"]:
            if item["id"] == item_id:
                return item
        raise ProgressError("UNKNOWN_ITEM", "Only work items frozen at stage initialization can earn credit")

    @staticmethod
    def _open(state):
        if state["action"] in ("finished", "pivoted"):
            raise ProgressError("STAGE_CLOSED", "The stage is closed; initialize an explicitly agreed next stage")

    def _revoke_reverted(self, db, stage, state, before, item_ids):
        settings = stage["policy"]["progress"]
        awards = {}
        for event in self._events(db, stage, committed=False):
            if event["kind"] == "assessment" and event["data"].get("points", 0) > 0:
                awards.setdefault(event["data"]["item_id"], event["data"])
        full_diff = None
        for item_id in item_ids:
            if item_id not in state["awarded_items"]:
                continue
            item = self._item(stage, item_id)
            diff = self.collector.diff(stage["baseline"]["revision"], before["revision"], settings, item["paths"])
            retained = bool(diff.strip())
            if retained:
                credit = (awards.get(item_id) or {}).get("credit")
                if isinstance(credit, dict):
                    # Retention also reads the unscoped baseline->candidate
                    # diff: a credited file moved outside this item's frozen
                    # paths is still present work — the delete block proves
                    # the move and the wide diff shows the moved-to content.
                    # Earning stays scoped; only retention widens.
                    if full_diff is None:
                        full_diff = self.collector.diff(
                            stage["baseline"]["revision"], before["revision"], settings
                        )
                    retained = _credited_retained(credit, diff, full_diff)
                else:
                    retained = False
            if not retained:
                if self._snapshot(stage) != before:
                    raise ProgressError("WORKTREE_CHANGED", "Repository changed while checking retained credit")
                reason = "scope_returned_to_baseline" if not diff.strip() else "credited_change_reverted"
                self._append(db, stage["plan"]["id"], "invalidate", {"item_id": item_id, "reason": reason, **before})
        return _summarize(stage, self._events(db, stage, committed=False))

    def initialize(self, plan: dict, policy: dict):
        normalized = validate_plan(plan, policy)
        allowed = normalized.get("repos") or [str(self.repo)]
        if os.path.normcase(str(self.repo)) not in {os.path.normcase(entry) for entry in allowed}:
            raise ProgressError("REPO_MISMATCH", "This ledger stage belongs to another repository path")
        stage = {"plan": normalized, "policy": copy.deepcopy(policy), "repo": str(self.repo), "created_at": time.time()}
        stage["policy_hash"] = fingerprint(stage["policy"])
        stage["plan_hash"] = fingerprint(normalized)
        settings = policy["progress"]
        with self._database(create=True, settings=settings) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM stages WHERE id = ?", (normalized["id"],)).fetchone():
                raise ProgressError("STAGE_EXISTS", "Existing stages and their configuration cannot be replaced")
            stage["baseline"] = self._snapshot(stage)
            stage["baseline_checks"] = self._verify(stage, list(normalized["checks"]), stage["baseline"])
            db.execute("INSERT INTO stages(id, document) VALUES (?, ?)", (normalized["id"], canonical(stage)))
            db.execute("INSERT INTO heads(stage_id, seal) VALUES (?, ?)", (normalized["id"], _chain_head(normalized["id"], fingerprint(stage))))
            db.commit()
            self._write_anchor(db)
        return _summarize(stage, [])

    def stage_ids(self):
        with self._database() as db:
            db.execute("BEGIN")
            return sorted(row["id"] for row in db.execute("SELECT id FROM stages"))

    def status(self, stage_id, *, member=True):
        with self._database() as db:
            db.execute("BEGIN")
            stage = self._load(db, stage_id, member=member)
            return _summarize(stage, self._events(db, stage))

    def history(self, stage_id, *, member=True):
        with self._database() as db:
            db.execute("BEGIN")
            stage = self._load(db, stage_id, member=member)
            events = self._events(db, stage)
            _summarize(stage, events)
            return {"stage": stage, "events": events}

    def evidence(self, stage_id, sequence):
        if type(sequence) is not int or sequence < 1:
            raise ProgressError("INVALID_INPUT", "A positive event sequence number is required")
        with self._database() as db:
            db.execute("BEGIN")
            stage = self._load(db, stage_id)
            events = self._events(db, stage)
        _summarize(stage, events)
        event = next((entry for entry in events if entry["sequence"] == sequence), None)
        if event is None or event["kind"] not in ("assessment", "review", "restore"):
            raise ProgressError("EVIDENCE_UNAVAILABLE", "Only assessment, review and restore events carry replayable evidence")
        _validate_event(stage, event)
        data = event["data"]
        replay = data.get("evidence_replay")
        if not isinstance(replay, dict) or not isinstance(replay.get("metadata"), dict) or not isinstance(data.get("evidence_sha256"), str):
            raise ProgressError("EVIDENCE_UNAVAILABLE", "Evidence input was withheld or is not replayable")
        metadata = copy.deepcopy(replay["metadata"])
        candidate = metadata.get("candidate")
        revision = candidate.get("revision") if isinstance(candidate, dict) else None
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision) or "diff" in candidate:
            raise ProgressError("STORE_INVALID", "Stored evidence metadata is inconsistent")
        item = metadata.get("item")
        paths = item.get("paths") if isinstance(item, dict) else None
        candidate["diff"] = self.collector.diff(stage["baseline"]["revision"], revision, stage["policy"]["progress"], paths)
        if event["kind"] in ("assessment", "restore") and data.get("change_sha256") != fingerprint(candidate["diff"]):
            raise ProgressError("EVIDENCE_MISMATCH", "Rebuilt evidence does not match the recorded change fingerprint")
        state = dict(metadata, candidate=candidate)
        digest = fingerprint(state)
        if digest != data["evidence_sha256"]:
            raise ProgressError("EVIDENCE_MISMATCH", "Repository evidence no longer reconstructs the recorded input")
        if event["kind"] == "assessment":
            credited = _credited_union(events, before_sequence=event["sequence"])
            sections = _credited_sections(events, before_sequence=event["sequence"])
            if data.get("credit") != _align_credit_shape(
                data.get("credit"),
                _earned_credit(_diff_line_hashes(candidate["diff"]), credited, sections),
            ):
                raise ProgressError("EVIDENCE_MISMATCH", "Recorded credit does not match the reconstructed change")
        if event["kind"] == "restore" and not _credited_retained(
            metadata.get("credit") or {},
            candidate["diff"],
            self.collector.diff(
                stage["baseline"]["revision"], revision, stage["policy"]["progress"]
            ),
        ):
            raise ProgressError("EVIDENCE_MISMATCH", "Rebuilt evidence no longer retains the credited outcome")
        if event["kind"] == "assessment":
            questions = {"contribution": stage["policy"]["templates"]["contribution"]}
        elif event["kind"] == "review":
            questions = _review_questions(stage)
        else:
            questions = {}
        return {"state": state, "sha256": digest, "questions": questions}

    def assess(self, stage_id, item_id, summary, *, retry_unavailable=False, asker=None):
        summary = _safe_text(summary, "summary")
        if type(retry_unavailable) is not bool:
            raise ProgressError("INVALID_INPUT", "Retry approval must be an explicit boolean")
        with self._transaction(stage_id) as (db, stage, events):
            state = _summarize(stage, events)
            self._open(state)
            item = self._item(stage, item_id)
            if item_id in state["blocked_items"]:
                raise ProgressError("ITEM_INVALIDATED", "Restore the original verified outcome without earning new credit")
            before = self._snapshot(stage)
            key = assessment_key(stage_id, item_id, before["tree"])
            previous = [event["data"] for event in events if event["kind"] == "assessment"]
            award = next((data for data in previous if data["item_id"] == item_id and data["points"] > 0), None)
            matching = [data for data in previous if data.get("assessment_key", data["id"]) == key]
            cached = award or (matching[-1] if matching else None)
            if retry_unavailable:
                if not cached or cached["status"] != "unscored" or cached["reason"] != "jev_unavailable":
                    raise ProgressError("RETRY_NOT_ALLOWED", "Only an unavailable request can be retried; grades and uncertain answers are cached")
            elif cached:
                if award and (award["tree"] != before["tree"] or award["revision"] != before["revision"]
                              or award.get("config") != before.get("config")):
                    state = self._revoke_reverted(db, stage, state, before, [item_id])
                if self._snapshot(stage) != before:
                    raise ProgressError("WORKTREE_CHANGED", "Repository changed before the cached outcome could be returned")
                return dict(state, cached=True, assessment_id=cached["id"])
            attempt = len(matching) + 1
            if state["action"] == "budget_exhausted":
                raise ProgressError("BUDGET_EXHAUSTED", "Stage iteration budget is exhausted; review or stop")
            if state["action"] == "review_required":
                raise ProgressError("REVIEW_REQUIRED", "Review the stage before assessing additional work")
            settings = stage["policy"]["progress"]
            names = sorted(set(stage["plan"]["required_checks"] + item["checks"]))
            checks = self._verify(stage, names, before)
            diff = self.collector.diff(stage["baseline"]["revision"], before["revision"], settings, item["paths"])
            change_sha256 = fingerprint(diff)
            blocked = set(state["blocked_items"])
            duplicate_change = any(
                data["points"] > 0 and data["item_id"] not in blocked and data.get("change_sha256") == change_sha256
                for data in previous
            )
            evidence = {
                "goal": stage["plan"]["goal"], "item": item,
                "baseline": {"revision": stage["baseline"]["revision"], "checks": [c for c in stage["baseline_checks"] if c["id"] in names]},
                "candidate": dict(before, checks=checks, diff=diff),
                "coder_summary": summary, "already_credited_items": state["awarded_items"],
            }
            raw_credit = _diff_line_hashes(diff)
            fresh_credit = _earned_credit(raw_credit, _credited_union(events), _credited_sections(events))
            result = {"choice": None, "confidence": None, "called_jev": False, "reason": "checks_failed"}
            if _passed(checks):
                if not diff.strip():
                    result.update(choice="zero", reason="unchanged_tree")
                elif duplicate_change:
                    result.update(choice="zero", reason="duplicate_change")
                elif not any(raw_credit[key] for key in ("added", "removed", "ops")):
                    result.update(choice="zero", reason="untrackable_change")
                elif not any(fresh_credit[key] for key in ("added", "removed", "ops")):
                    result.update(choice="zero", reason="already_credited")
                else:
                    outcome = _ask(evidence, {"contribution": stage["policy"]["templates"]["contribution"]}, stage["policy"], asker)
                    result = dict(outcome["results"]["contribution"], called_jev=outcome["called_jev"], model=outcome["model"],
                                  usage=outcome.get("usage"))
            if self._snapshot(stage) != before:
                raise ProgressError("WORKTREE_CHANGED", "Repository changed before the assessment could be recorded")
            choice = result["choice"]
            attempt_id = assessment_key(stage_id, item_id, before["tree"], attempt)
            withheld = _sensitive(evidence)
            data = {
                "id": attempt_id, "assessment_key": key, "attempt": attempt, "item_id": item_id, **before,
                "change_sha256": change_sha256,
                "credit": None if withheld else fresh_credit,
                "status": "scored" if choice is not None else "unscored",
                "level": choice, "points": settings["points"][choice] if choice is not None else 0,
                "confidence": result["confidence"], "reason": result["reason"],
                "observed_choice": result.get("observed_choice"), "probabilities": result.get("probabilities", {}),
                "called_jev": result["called_jev"], "model": result.get("model"),
                "checks": checks, "evidence_sha256": fingerprint(evidence),
                "evidence_replay": None if withheld else {"metadata": _replay_metadata(evidence)},
            }
            if isinstance(result.get("usage"), dict):
                data["usage"] = result["usage"]
            self._append(db, stage_id, "assessment", data, attempt_id)
            return dict(_summarize(stage, self._events(db, stage, committed=False)), cached=False, assessment_id=attempt_id)

    def invalidate(self, stage_id, item_id, reason):
        reason = _safe_text(reason, "reason")
        with self._transaction(stage_id) as (db, stage, events):
            state = _summarize(stage, events)
            self._open(state)
            self._item(stage, item_id)
            if item_id in state["blocked_items"]:
                return state
            if item_id not in state["awarded_items"]:
                raise ProgressError("NO_AWARD", "Only an existing credited outcome can be invalidated")
            before = self._snapshot(stage)
            self._append(db, stage_id, "invalidate", {"item_id": item_id, "reason": reason, **before})
            if self._snapshot(stage) != before:
                raise ProgressError("WORKTREE_CHANGED", "Repository changed before the invalidation could be recorded")
            return _summarize(stage, self._events(db, stage, committed=False))

    def restore(self, stage_id, item_id, reason, reviewer):
        reason, reviewer = _safe_text(reason, "reason"), _safe_text(reviewer, "reviewer")
        with self._transaction(stage_id) as (db, stage, events):
            state = _summarize(stage, events)
            self._open(state)
            item = self._item(stage, item_id)
            if item_id not in state["blocked_items"]:
                raise ProgressError("NOT_INVALIDATED", "There is no invalidated award to restore")
            before = self._snapshot(stage)
            names = stage["plan"]["required_checks"] + item["checks"]
            checks = self._verify(stage, names, before)
            if not _passed(checks):
                raise ProgressError("VERIFICATION_FAILED", "Fresh acceptance checks must pass before restoring an award")
            diff = self.collector.diff(stage["baseline"]["revision"], before["revision"], stage["policy"]["progress"], item["paths"])
            if not diff.strip():
                raise ProgressError("NO_CURRENT_CHANGE", "A scope still at its baseline cannot recover contribution credit")
            award = next((e["data"] for e in events if e["kind"] == "assessment" and e["data"].get("item_id") == item_id and e["data"].get("points", 0) > 0), None)
            credit = (award or {}).get("credit")
            # Retention uses the unscoped diff like review does — a file
            # moved outside the item's frozen paths is still present work.
            wide = self.collector.diff(
                stage["baseline"]["revision"], before["revision"], stage["policy"]["progress"]
            )
            if not isinstance(credit, dict) or not _credited_retained(credit, diff, wide):
                raise ProgressError("RESTORE_MISMATCH", "The restored change must contain the originally credited outcome")
            if _credit_overlaps(credit, _credited_union(events)):
                raise ProgressError("RESTORE_CONFLICT", "Another item now holds credit for those lines; resolve it before restoring")
            if self._snapshot(stage) != before:
                raise ProgressError("WORKTREE_CHANGED", "Repository changed before credit could be restored")
            evidence = {
                "goal": stage["plan"]["goal"], "item": item,
                "baseline": {"revision": stage["baseline"]["revision"]},
                "candidate": dict(before, checks=checks, diff=diff),
                "restore_reason": reason, "reviewer": reviewer, "credit": credit,
            }
            self._append(db, stage_id, "restore", {
                "item_id": item_id, "reason": reason, "reviewer": reviewer, "restores": award["id"], "credit": credit,
                **before, "checks": checks,
                "change_sha256": fingerprint(diff), "evidence_sha256": fingerprint(evidence),
                "evidence_replay": None if _sensitive(evidence) else {"metadata": _replay_metadata(evidence)},
            })
            return _summarize(stage, self._events(db, stage, committed=False))

    def review(self, stage_id, reason, reviewer, *, approve_finish=False, asker=None):
        reason, reviewer = _safe_text(reason, "reason"), _safe_text(reviewer, "reviewer")
        if type(approve_finish) is not bool:
            raise ProgressError("INVALID_INPUT", "Completion approval must be an explicit boolean")
        with self._transaction(stage_id) as (db, stage, events):
            state = _summarize(stage, events)
            self._open(state)
            settings = stage["policy"]["progress"]
            over_budget = (
                state["model_attempts"] >= settings["max_model_calls"]
                or (type(settings.get("max_tokens")) is int
                    and state.get("tokens_used", 0) >= settings["max_tokens"])
            )
            if over_budget and (not approve_finish or any(
                    event["kind"] == "review" and event["data"].get("over_budget") for event in events)):
                raise ProgressError("BUDGET_EXHAUSTED", "Jev request-attempt budget is exhausted; human review is required")
            before = self._snapshot(stage)
            checks = self._verify(stage, list(stage["plan"]["checks"]), before)
            state = self._revoke_reverted(db, stage, state, before, state["awarded_items"])
            checks_passed = _passed(checks)
            required_passed = _passed([check for check in checks if check["id"] in stage["plan"]["required_checks"]])
            diff = self.collector.diff(stage["baseline"]["revision"], before["revision"], settings)
            questions = _review_questions(stage)
            evidence = {
                "goal": stage["plan"]["goal"], "items": stage["plan"]["items"],
                "progress": state, "candidate": dict(before, checks=checks, diff=diff),
                "all_checks_passed": checks_passed, "required_checks_passed": required_passed,
                "unresolved_blockers": state["blocked_items"],
                "completion_approval": approve_finish, "review_context": reason,
                "assessments": [
                    {key: e["data"][key] for key in ("id", "item_id", "revision", "status", "level", "points", "reason", "evidence_sha256")}
                    for e in events if e["kind"] == "assessment"
                ],
            }
            if state["blocked_items"] or not required_passed:
                outcome = _denied("acceptance_failed", questions, False)
            else:
                outcome = _ask(evidence, questions, stage["policy"], asker)
            if self._snapshot(stage) != before:
                raise ProgressError("WORKTREE_CHANGED", "Repository changed before the review could be recorded")
            result = outcome["results"]["progress_review"]
            item_acceptance = {}
            for item in stage["plan"]["items"]:
                verdict = outcome["results"].get("accept_" + item["id"], {"choice": None, "confidence": None, "reason": outcome["reason"]})
                item_acceptance[item["id"]] = {
                    "status": verdict["choice"] if verdict["choice"] in ("met", "unmet") else "unknown",
                    "confidence": verdict["confidence"], "reason": verdict["reason"],
                }
            choice = result["choice"]
            applied = choice is not None
            rejection = result["reason"]
            all_met = all(entry["status"] == "met" for entry in item_acceptance.values())
            if choice == "finish" and not approve_finish:
                applied, rejection = False, "approval_required"
            if choice == "finish" and (not checks_passed or state["blocked_items"]):
                applied, rejection = False, "acceptance_failed"
            assessed_items = {event["data"]["item_id"] for event in events if event["kind"] == "assessment"}
            if choice == "finish" and any(item["id"] not in assessed_items for item in stage["plan"]["items"]):
                applied, rejection = False, "items_not_assessed"
            if choice == "finish" and not all_met:
                applied, rejection = False, "item_acceptance_required"
            if choice and choice.startswith("pivot_") and (not required_passed or state["blocked_items"]):
                applied, rejection = False, "acceptance_failed"
            data = {
                "choice": choice, "applied": applied, "reason": rejection,
                "review_context": reason, "reviewer": reviewer, "approve_finish": approve_finish,
                "over_budget": over_budget,
                "item_acceptance": item_acceptance,
                **before, "checks": checks, "confidence": result["confidence"],
                "observed_choice": result.get("observed_choice"), "probabilities": result.get("probabilities", {}),
                "called_jev": outcome["called_jev"], "model": outcome["model"],
                "review_at": state["points"] + settings["review_points"],
                "evidence_sha256": fingerprint(evidence),
                "evidence_replay": None if _sensitive(evidence) else {"metadata": _replay_metadata(evidence)},
            }
            if isinstance(outcome.get("usage"), dict):
                data["usage"] = outcome["usage"]
            self._append(db, stage_id, "review", data)
            return _summarize(stage, self._events(db, stage, committed=False))


def main(argv=None):
    """Library module: --version/--help only, no standalone commands."""
    argv = sys.argv[1:] if argv is None else argv
    if _watch.maybe_version(argv):
        return 0
    parser = argparse.ArgumentParser(
        description="progress_core is the ledger/evidence engine imported by "
        "progress.py and policy_lint.py; it has no standalone commands."
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    _watch.exit_safely(main())
