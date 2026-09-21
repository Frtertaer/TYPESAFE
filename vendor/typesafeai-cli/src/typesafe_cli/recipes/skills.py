from __future__ import annotations

from pathlib import Path
from typing import Any

from typesafe_cli.questions import QuestionError
from typesafe_cli.recipes.thresholds import SKILL_EXCERPT_CHARS, SKILL_FITS_MIN, SKILL_NEEDS_MIN, SKILL_TOP_K


def parse_skill_md(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    meta, body = _frontmatter(text)
    name = str(meta.get("name") or path.parent.name).strip()
    description = str(meta.get("description") or "").strip()
    excerpt = body.strip()[:SKILL_EXCERPT_CHARS]
    return {
        "name": name,
        "description": description or excerpt[:200],
        "excerpt": excerpt,
        "path": str(path),
    }


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :]
    meta: dict[str, str] = {}
    key: str | None = None
    chunks: list[str] = []
    for line in raw.splitlines():
        if key and (line.startswith(" ") or line.startswith("\t")):
            chunks.append(line.strip())
            continue
        if ":" in line:
            if key is not None:
                meta[key] = " ".join(chunks).strip().lstrip(">|").strip()
            key, _, rest = line.partition(":")
            key = key.strip()
            chunks = [rest.strip()]
    if key is not None:
        meta[key] = " ".join(chunks).strip().lstrip(">|").strip()
    return meta, body


def discover_skills(skills_dir: Path) -> list[dict[str, str]]:
    if not skills_dir.is_dir():
        raise QuestionError(f"skills dir not found: {skills_dir}")
    found = [parse_skill_md(path) for path in sorted(skills_dir.rglob("SKILL.md"))]
    if len(found) < 2:
        raise QuestionError("suggest-skill needs at least two SKILL.md files")
    names = [skill["name"] for skill in found]
    if len(names) != len(set(names)):
        raise QuestionError("duplicate skill names in skills dir")
    return found


def rank_questions(task: str, skills: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
    criteria = {skill["name"]: skill["description"] or None for skill in skills}
    questions = {
        "skill": {
            "type": "choice",
            "instructions": "Which installed skill, if any, is the best fit for `task`?",
            "criteria": criteria,
        },
        "needs_skill": {
            "type": "noul",
            "instructions": "Does `task` require loading a specialized skill rather than general reasoning?",
        },
        "documented_procedure": {
            "type": "noul",
            "instructions": "Does `task` match a documented procedure that a skill would encode?",
        },
        "prose_suffices": {
            "type": "noul",
            "instructions": "Would ordinary prose without a skill be enough to complete `task`?",
        },
    }
    state = {
        "task": task,
        "skills": {skill["name"]: skill["description"] for skill in skills},
    }
    return state, questions


def reread_questions(
    task: str,
    shortlist: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    questions: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    for skill in shortlist:
        qid = f"fits_{skill['name']}"
        questions[qid] = {
            "type": "noul",
            "instructions": (
                f"After reading `{skill['name']}`'s excerpt, does this skill fit `task`?"
            ),
        }
        catalog[skill["name"]] = {
            "description": skill["description"],
            "excerpt": skill["excerpt"],
        }
    return {"task": task, "skills": catalog}, questions


def shortlist_from_rank(
    skills: list[dict[str, str]],
    answers: dict[str, Any],
    *,
    top_k: int = SKILL_TOP_K,
) -> list[dict[str, str]]:
    by_name = {skill["name"]: skill for skill in skills}
    probabilities = answers["skill"].get("probabilities") or {}
    ranked = sorted(probabilities.items(), key=lambda item: float(item[1]), reverse=True)
    picked: list[dict[str, str]] = []
    for name, _probability in ranked:
        if name in by_name:
            picked.append(by_name[name])
        if len(picked) >= top_k:
            break
    winner = answers["skill"].get("choice")
    if winner in by_name and by_name[winner] not in picked:
        picked = [by_name[winner], *picked][:top_k]
    return picked


def pick_skill(
    *,
    rank_answers: dict[str, Any],
    fit_answers: dict[str, Any],
    shortlist: list[dict[str, str]],
) -> str | None:
    needs = float(rank_answers["needs_skill"]["noul"])
    if needs < SKILL_NEEDS_MIN:
        return None
    scored: list[tuple[float, str]] = []
    for skill in shortlist:
        noul = float(fit_answers[f"fits_{skill['name']}"]["noul"])
        scored.append((noul, skill["name"]))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < SKILL_FITS_MIN:
        return None
    return scored[0][1]
