"""Skill system — SkillBase (ABC), SkillRegistry (singleton), SkillSynthesizer."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from src.providers.llm.router import LLMRouter, SpeedTag

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Lifecycle states
# ------------------------------------------------------------------

class SkillLifecycle(str, Enum):
    CANDIDATE = "candidate"   # proposed but not tested
    SANDBOXED = "sandboxed"   # tested in isolation
    ACTIVE = "active"         # available for use
    STALE = "stale"           # deprecated / not maintained
    ARCHIVED = "archived"     # removed from active set


# ------------------------------------------------------------------
# SkillBase
# ------------------------------------------------------------------

class SkillBase(ABC):
    """Abstract base for all skills."""

    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    lifecycle: SkillLifecycle = SkillLifecycle.CANDIDATE
    author: str = "lyra"

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> str:
        """Execute the skill with the given parameters."""
        ...

    def __repr__(self) -> str:
        return f"<Skill {self.name} v{self.version} [{self.lifecycle.value}]>"


# ------------------------------------------------------------------
# SkillRegistry (singleton)
# ------------------------------------------------------------------

class SkillRegistry:
    """Thread-safe singleton registry for skills."""

    _instance: Optional["SkillRegistry"] = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs) -> "SkillRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._skills: dict[str, SkillBase] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, skill: SkillBase) -> None:
        """Register a skill instance."""
        if not skill.name:
            raise ValueError("Skill must have a non-empty name.")
        self._skills[skill.name] = skill
        logger.info("Skill registered: %s v%s", skill.name, skill.version)

    def unregister(self, name: str) -> None:
        """Remove a skill from the registry."""
        self._skills.pop(name, None)
        logger.info("Skill unregistered: %s", name)

    def get_skill(self, name: str) -> Optional[SkillBase]:
        """Get a skill by name."""
        return self._skills.get(name)

    def get_active_skills(self) -> list[SkillBase]:
        """Return all skills with ACTIVE lifecycle."""
        return [s for s in self._skills.values() if s.lifecycle == SkillLifecycle.ACTIVE]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cycle_lifecycle(self, name: str, new_state: SkillLifecycle) -> bool:
        """Transition a skill to a new lifecycle state."""
        skill = self._skills.get(name)
        if skill is None:
            logger.warning("Cannot cycle lifecycle for unknown skill: %s", name)
            return False
        skill.lifecycle = new_state
        logger.info("Skill '%s' → %s", name, new_state.value)
        return True

    # ------------------------------------------------------------------
    # System prompt integration
    # ------------------------------------------------------------------

    def get_combined_system_prompt(self) -> str:
        """Build a system prompt fragment listing all active skills."""
        active = self.get_active_skills()
        if not active:
            return ""
        lines = ["\n## Compétences disponibles\n"]
        for s in active:
            lines.append(f"- **{s.name}** v{s.version} : {s.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # File-based loading
    # ------------------------------------------------------------------

    def load_from_dir(self, path: str) -> int:
        """Scan a directory for .py skill files and import them.

        Each file must define a class that inherits from SkillBase and
        has a module-level attribute ``skill_instance`` or the class
        itself named ``Skill``.
        """
        p = Path(path)
        if not p.is_dir():
            logger.warning("Skill directory not found: %s", path)
            return 0

        count = 0
        sys.path.insert(0, str(p.parent))
        for file in sorted(p.glob("*.py")):
            if file.name.startswith("_"):
                continue
            try:
                mod_name = f"lyra_skills_{file.stem}"
                spec = importlib.util.spec_from_file_location(mod_name, file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                # Look for skill_instance first, then Skill class
                skill = getattr(mod, "skill_instance", None)
                if skill is None:
                    cls = getattr(mod, "Skill", None)
                    if cls is not None and issubclass(cls, SkillBase) and cls is not SkillBase:
                        skill = cls()
                if skill is not None and isinstance(skill, SkillBase):
                    self.register(skill)
                    count += 1
                else:
                    logger.debug("No SkillBase subclass found in %s", file.name)
            except Exception as exc:
                logger.warning("Failed to load skill from %s: %s", file.name, exc)

        sys.path.pop(0)
        logger.info("Loaded %d skill(s) from %s", count, path)
        return count


# ------------------------------------------------------------------
# SkillSynthesizer
# ------------------------------------------------------------------

class SkillSynthesizer:
    """Generate new skills from task trajectories using the LLM."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def synthesize(self, task_trajectory: str) -> Optional[SkillBase]:
        """Ask the LLM to produce a new skill from a task trajectory.

        Returns a SkillBase instance if successful.
        """
        prompt = (
            "Tu es un ingénieur en compétences. Analyse la trace de tâche suivante "
            "et génère une compétence réutilisable.\n\n"
            f"Trace :\n{task_trajectory}\n\n"
            "Réponds UNIQUEMENT avec un JSON de cette forme :\n"
            '```json\n{\n'
            '  "name": "nom_de_la_skill",\n'
            '  "description": "description courte",\n'
            '  "version": "1.0.0",\n'
            '  "code": "async def execute(params):\\n    return \\"résultat\\""\n'
            "}\n```\n\n"
            "Le code doit être une fonction asynchrone nommée `execute` "
            "qui prend `params: dict` et retourne une chaîne."
        )

        raw = await self.router.generate(prompt, tag=SpeedTag.DEEP)

        # Extract JSON
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("SkillSynthesizer: could not parse LLM output as JSON.")
            return None

        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        version = data.get("version", "0.1.0")
        code = data.get("code", "")

        if not name or not code:
            logger.warning("SkillSynthesizer: LLM output missing name or code.")
            return None

        # Dynamically create a SkillBase subclass
        skill = self._build_skill(name, description, version, code)
        return skill

    def _build_skill(
        self,
        name: str,
        description: str,
        version: str,
        code: str,
    ) -> Optional[SkillBase]:
        """Dynamically compile and wrap code into a SkillBase subclass."""
        # Prepare a namespace
        ns: dict[str, Any] = {"SkillBase": SkillBase}

        # Wrap code in a class definition
        class_def = (
            f"class _DynamicSkill(SkillBase):\n"
            f"    name = {json.dumps(name)}\n"
            f"    description = {json.dumps(description)}\n"
            f"    version = {json.dumps(version)}\n\n"
            f"    {code.replace(chr(10), chr(10) + '    ')}"
        )

        try:
            exec(class_def, ns)
            cls = ns["_DynamicSkill"]
            # Verify it's a proper SkillBase subclass
            if not issubclass(cls, SkillBase) or cls is SkillBase:
                raise TypeError("Generated class does not inherit SkillBase.")
            instance = cls()
            return instance
        except Exception as exc:
            logger.error("SkillSynthesizer: failed to build skill class: %s", exc)
            return None


