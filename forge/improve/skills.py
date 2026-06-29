"""Rung 2 — Skills (Phase 7 §4).

A skill is a *verified* reusable procedure promoted to a callable tool, so the
agent stops re-deriving solutions it has already gotten right (the Voyager
pattern). Candidates are staged by reflect(); a candidate is promoted only after
it clears the regression gate (§5.2). Everything is append-only, versioned, and
reversible — promotion never overwrites a prior version, and rollback just points
the index at it.

Skill module contract: a skill file defines
    def run(workspace: str, args: dict) -> dict   # {"ok": bool, "output": str}
and carries a docstring stating its contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from forge.improve.regression import RegressionGate
from forge.providers.base import ToolSpec
from forge.tools.base import Tool, ToolContext, ToolResult


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class SkillCandidate:
    name: str
    code: str                     # full module source defining run(workspace, args)
    signature: str = ""
    when_to_use: str = ""
    certifying_trace: str = ""


@dataclass
class SkillLibrary:
    skills_dir: str               # .forge/skills/
    gate: Optional[RegressionGate] = None
    _index: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        os.makedirs(self.staging_dir, exist_ok=True)
        self._index = self._load_index()

    # -- paths ------------------------------------------------------------- #

    @property
    def index_path(self) -> str:
        return os.path.join(self.skills_dir, "index.json")

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.skills_dir, "_staged")

    def _version_file(self, name: str, version: int) -> str:
        return os.path.join(self.skills_dir, f"{name}__v{version}.py")

    # -- contract (§4.3) --------------------------------------------------- #

    def propose(self, candidate: SkillCandidate) -> Optional[str]:
        """Stage a candidate. Does NOT promote (promotion is gated + throttled)."""

        if not candidate.name.isidentifier():
            return None
        os.makedirs(self.staging_dir, exist_ok=True)
        with open(os.path.join(self.staging_dir, f"{candidate.name}.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(candidate.code)
        meta = {
            "name": candidate.name,
            "signature": candidate.signature,
            "when_to_use": candidate.when_to_use,
            "certifying_trace": candidate.certifying_trace,
        }
        with open(os.path.join(self.staging_dir, f"{candidate.name}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh)
        return candidate.name

    def staged(self) -> list[str]:
        if not os.path.isdir(self.staging_dir):
            return []
        return sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(self.staging_dir)
            if f.endswith(".py")
        )

    def promote(self, name: str) -> bool:
        """Promote a staged skill only if the regression gate passes (§5.2)."""

        staged_py = os.path.join(self.staging_dir, f"{name}.py")
        staged_json = os.path.join(self.staging_dir, f"{name}.json")
        if not os.path.isfile(staged_py):
            return False
        if self.gate is None:
            return False

        # Gate the change: the staged module must import cleanly AND the frozen
        # regression suite must still pass.
        result = self.gate.gate(
            change_id=f"skill:{name}",
            validator=lambda: _load_run(staged_py, f"_stage_{name}"),
        )
        if not result.passed:
            return False

        meta = {}
        if os.path.isfile(staged_json):
            with open(staged_json, encoding="utf-8") as fh:
                meta = json.load(fh)

        entry = self._index.get(name)
        new_version = (entry["current_version"] + 1) if entry else 1
        shutil.copy2(staged_py, self._version_file(name, new_version))

        versions = entry["versions"] if entry else {}
        versions[str(new_version)] = os.path.basename(self._version_file(name, new_version))
        self._index[name] = {
            "current_version": new_version,
            "signature": meta.get("signature", ""),
            "when_to_use": meta.get("when_to_use", ""),
            "certifying_trace": meta.get("certifying_trace", ""),
            "pass_record": {
                "promoted": _today(),
                "uses": (entry["pass_record"]["uses"] if entry else 0),
                "regressions": (entry["pass_record"]["regressions"] if entry else 0),
            },
            "versions": versions,
        }
        self._save_index()
        # Clear staging for this skill once promoted.
        for p in (staged_py, staged_json):
            if os.path.exists(p):
                os.remove(p)
        return True

    def rollback(self, name: str, version: Optional[int] = None) -> bool:
        """Point a skill at a prior version. Files are never deleted (§5.3)."""

        entry = self._index.get(name)
        if not entry:
            return False
        if version is None:
            # Roll back to the previous version.
            versions = sorted(int(v) for v in entry["versions"])
            prior = [v for v in versions if v < entry["current_version"]]
            if not prior:
                return False
            version = prior[-1]
        if str(version) not in entry["versions"]:
            return False
        entry["current_version"] = version
        self._save_index()
        return True

    def as_tools(self) -> list[ToolSpec]:
        return [t.spec() for t in self.tool_objects()]

    def tool_objects(self) -> list[Tool]:
        return [_SkillTool(name, self) for name in self._index]

    def catalog(self) -> str:
        if not self._index:
            return ""
        lines = []
        for name, entry in self._index.items():
            lines.append(f"- {name}: {entry.get('when_to_use', '')}".rstrip())
        return "\n".join(lines)

    def record_use(self, name: str, regressed: bool) -> None:
        entry = self._index.get(name)
        if not entry:
            return
        entry["pass_record"]["uses"] += 1
        if regressed:
            entry["pass_record"]["regressions"] += 1
        self._save_index()

    def current_module_path(self, name: str) -> Optional[str]:
        entry = self._index.get(name)
        if not entry:
            return None
        fname = entry["versions"][str(entry["current_version"])]
        return os.path.join(self.skills_dir, fname)

    # -- index io ---------------------------------------------------------- #

    def _load_index(self) -> dict:
        if not os.path.isfile(self.index_path):
            return {}
        with open(self.index_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("skills", {})

    def _save_index(self) -> None:
        os.makedirs(self.skills_dir, exist_ok=True)
        directory = self.skills_dir
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".index-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"skills": self._index}, fh, indent=2)
        os.replace(tmp, self.index_path)


def _load_run(path: str, module_name: str):
    """Import a skill module file and return its `run` callable."""

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise AttributeError(f"skill module {path} defines no run(workspace, args)")
    return module.run


class _SkillTool(Tool):
    """Wraps a promoted skill as a normal dispatchable tool (§4.4)."""

    parameters = {
        "type": "object",
        "properties": {"args": {"type": "object", "description": "skill arguments"}},
        "required": [],
    }

    def __init__(self, name: str, library: SkillLibrary) -> None:
        self.name = name
        self.library = library
        entry = library._index.get(name, {})
        self.description = entry.get("when_to_use", f"promoted skill {name}")

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = self.library.current_module_path(self.name)
        if not path or not os.path.isfile(path):
            return ToolResult(ok=False, content=f"skill '{self.name}' has no module")
        try:
            run = _load_run(path, f"_skill_{self.name}_run")
            out = run(ctx.workspace, args.get("args", {}) or {})
        except Exception as exc:
            self.library.record_use(self.name, regressed=True)
            return ToolResult(ok=False, content=f"skill '{self.name}' raised: {exc}")
        ok = bool(out.get("ok", False)) if isinstance(out, dict) else False
        output = out.get("output", "") if isinstance(out, dict) else str(out)
        self.library.record_use(self.name, regressed=not ok)
        return ToolResult(ok=ok, content=output or f"skill '{self.name}' ran")
