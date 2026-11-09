"""Agents are defined in Markdown: agentic/agents/<agent>.md, one file per agent, and ONLY model-backed agents get one.

A spec is the agent's *role, limits and prompt*: what it reads and writes, which guardrails bind it and the exact
system prompt sent to the model. Deterministic work (parsing code, running tests, enforcing write scopes, retries,
human gates) is plain Python in agentic/steps/ and the engine modules; it is not an "agent" and has no spec. The
loop, sandbox and validators never take instructions from these files; only the prompt text reaches a model.

File format (stdlib parser, no YAML dependency):

    ---
    name: code_author               # must equal the Python agent's `name` and the file name
    title: Code Author
    kind: llm                    # llm | hybrid
    stage: design                # understand | design | build | check
    plans: [greenfield]          # plan types that use it
    inputs: [requirement]        # blackboard keys / files it reads
    outputs: [design]            # blackboard keys / files it produces
    writes: [openapi.json]       # workspace write scope (globs)
    ---
    ## Role
    ...
    ## System prompt             # required for kind llm / hybrid; sent to the model verbatim
    ...

Shared prompt context lives in agents/context/*.md and are read with load_context().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

SPEC_DIR = Path(__file__).resolve().parent / "agents"
KINDS = ("llm", "hybrid")  # llm = writes artifacts; hybrid = rule-based, a model may only add to its result
STAGES = ("understand", "plan", "design", "build", "check")
REQUIRED_KEYS = ("name", "title", "kind", "stage")
LIST_KEYS = ("plans", "inputs", "outputs", "writes")
PROMPT_SECTION = "System prompt"
REQUIRED_SECTIONS = ("Role", "Guardrails", PROMPT_SECTION)


class SpecError(ValueError):
    """A spec file is malformed, or the specs and the registered agents disagree."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    title: str
    kind: str
    stage: str
    plans: Tuple[str, ...] = ()
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    writes: Tuple[str, ...] = ()
    sections: Dict[str, str] = field(default_factory=dict, compare=False)
    path: Path = field(default=Path("."), compare=False)

    def section(self, title: str) -> str:
        try:
            return self.sections[title]
        except KeyError:
            raise SpecError(f"{self.path.name}: missing section '## {title}'") from None

    def prompt(self) -> str:
        """The system prompt sent to the model, exactly as written in the spec."""
        return self.section(PROMPT_SECTION)


def _split_frontmatter(text: str, where: str) -> Tuple[Dict[str, object], str]:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SpecError(f"{where}: must start with a '---' front-matter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SpecError(f"{where}: front-matter block is not closed with '---'") from None
    meta: Dict[str, object] = {}
    for raw in lines[1:end]:
        line = raw.split("  #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SpecError(f"{where}: bad front-matter line {raw!r}")
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key] = tuple(v.strip() for v in val[1:-1].split(",") if v.strip())
        else:
            meta[key] = val
    return meta, "\n".join(lines[end + 1:])


def _sections(body: str) -> Dict[str, str]:
    out: Dict[str, List[str]] = {}
    current, in_fence = None, False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = line[3:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_spec(text: str, path: Path = Path("<memory>")) -> AgentSpec:
    where = path.name
    meta, body = _split_frontmatter(text, where)
    missing = [k for k in REQUIRED_KEYS if not meta.get(k)]
    if missing:
        raise SpecError(f"{where}: missing front-matter key(s) {missing}")
    if meta["kind"] not in KINDS:
        raise SpecError(f"{where}: kind must be one of {KINDS}, got {meta['kind']!r}")
    if meta["stage"] not in STAGES:
        raise SpecError(f"{where}: stage must be one of {STAGES}, got {meta['stage']!r}")
    for k in LIST_KEYS:
        if k in meta and not isinstance(meta[k], tuple):
            raise SpecError(f"{where}: '{k}' must be a [list]")
    sections = _sections(body)
    absent = [s for s in REQUIRED_SECTIONS if not sections.get(s)]
    if absent:
        raise SpecError(f"{where}: missing or empty section(s) {absent}")
    if path.stem != meta["name"] and path.name != "<memory>":
        raise SpecError(f"{where}: file name must equal front-matter name {meta['name']!r}")
    return AgentSpec(name=str(meta["name"]), title=str(meta["title"]), kind=str(meta["kind"]), stage=str(meta["stage"]),
                     plans=tuple(meta.get("plans", ())), inputs=tuple(meta.get("inputs", ())),
                     outputs=tuple(meta.get("outputs", ())), writes=tuple(meta.get("writes", ())),
                     sections=sections, path=path)


@lru_cache(maxsize=1)
def load_specs() -> Dict[str, AgentSpec]:
    """Every agent spec in SPEC_DIR (files starting with '_' and sub-folders are not agents)."""
    specs: Dict[str, AgentSpec] = {}
    for p in sorted(SPEC_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        spec = parse_spec(p.read_text(encoding="utf-8"), p)
        specs[spec.name] = spec
    return specs


def load_spec(name: str) -> AgentSpec:
    try:
        return load_specs()[name]
    except KeyError:
        raise SpecError(f"no agent spec for {name!r} in {SPEC_DIR}") from None


def load_context(name: str) -> str:
    """A reusable prompt fragment from agents/context/<name>.md (body only, trimmed)."""
    p = SPEC_DIR / "context" / f"{name}.md"
    if not p.is_file():
        raise SpecError(f"no shared prompt fragment {name!r} at {p}")
    return p.read_text(encoding="utf-8").strip()


def check_specs(registry: Dict[str, object]) -> List[str]:
    """Problems between the registered Python agents and the MD specs ([] means consistent).

    Rules: an agent is "model-backed" when its class sets `model_backed = True`. Every model-backed agent has exactly
    one spec and every spec has a model-backed agent. Agents implemented in authoring_agents.py must be kind 'llm'.
    Deterministic steps are deliberately not listed here: they are not agents.
    """
    problems: List[str] = []
    specs = load_specs()
    backed = {n for n, a in registry.items() if getattr(a, "model_backed", False)}
    for name in sorted(backed - set(specs)):
        problems.append(f"agent {name!r} is model-backed but has no spec file")
    for name in sorted(set(specs) - set(registry)):
        problems.append(f"spec {name!r} has no registered agent")
    for name in sorted(set(specs) & set(registry) - backed):
        problems.append(f"spec {name!r} exists but its implementation is not marked model_backed (deterministic steps need no spec)")
    for name in sorted(set(specs) & backed):
        is_llm_impl = type(registry[name]).__module__.endswith(("authoring_agents", "harness.lead"))
        if is_llm_impl and specs[name].kind != "llm":
            problems.append(f"{name}: implemented as a model agent but spec kind is {specs[name].kind!r}")
        if not is_llm_impl and specs[name].kind == "llm":
            problems.append(f"{name}: spec says kind 'llm' but the implementation is not a model agent (authoring_agents / harness.lead)")
    return problems


def describe(specs: Iterable[AgentSpec], registry: Dict[str, object]) -> str:
    """Two tables: the model-backed agents (from their MD specs) and the deterministic steps (everything else)."""
    rows = ["AGENTS: model-backed, defined in agentic/agents/*.md", f"  {'agent':<22}{'kind':<8}{'plans':<62}{'writes / outputs'}"]
    for s in sorted(specs, key=lambda x: x.name):
        rows.append(f"  {s.name:<22}{s.kind:<8}{', '.join(s.plans):<62}{', '.join(s.writes or s.outputs) or '-'}")
    steps = sorted(n for n, a in registry.items() if not getattr(a, "model_backed", False))
    rows += ["", "STEPS: deterministic Python in agentic/steps/ (no model calls)", "  " + ", ".join(steps)]
    return "\n".join(rows)