"""MissionEngine — autonomous mission execution with governance (Risk, Category, Budget)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from src.kernel.eventbus import EventBus
from src.providers.llm.router import LLMRouter, SpeedTag
from src.providers.tools.registry import ToolRegistry
from src.kernel.config import ConfigManager
from .memory import MemoryOrchestrator

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Governance enums
# ------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Category(str, Enum):
    CODE = "code"
    HARDWARE = "hardware"
    NETWORK = "network"
    DATA = "data"
    SYSTEM = "system"


class Budget(str, Enum):
    MICRO = "micro"       # 1-2 LLM calls
    SMALL = "small"       # 3-5
    MEDIUM = "medium"     # 5-10
    LARGE = "large"       # 10-20
    UNLIMITED = "unlimited"


# Budget cost tracking (approximate LLM calls)
BUDGET_LIMITS = {
    Budget.MICRO: 2,
    Budget.SMALL: 5,
    Budget.MEDIUM: 10,
    Budget.LARGE: 20,
    Budget.UNLIMITED: float("inf"),
}


# ------------------------------------------------------------------
# Mission Engine
# ------------------------------------------------------------------

class MissionEngine:
    """Plan, execute, and audit autonomous missions with governance controls."""

    def __init__(
        self,
        eventbus: EventBus,
        router: LLMRouter,
        registry: ToolRegistry,
        memory: MemoryOrchestrator,
        config: ConfigManager,
    ) -> None:
        self.eventbus = eventbus
        self.router = router
        self.registry = registry
        self.memory = memory
        self.config = config

        # Active missions
        self._missions: dict[str, dict[str, Any]] = {}

        # Audit directory
        self._audit_dir = Path(config.get("mission.audit_dir", "data/missions"))
        self._audit_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch_mission(
        self,
        goal: str,
        context: Optional[dict] = None,
        risk: RiskLevel = RiskLevel.LOW,
        category: Category = Category.CODE,
        budget: Budget = Budget.SMALL,
    ) -> str:
        """Launch a new autonomous mission. Returns mission_id."""
        mission_id = uuid.uuid4().hex[:12]
        context = context or {}

        mission = {
            "id": mission_id,
            "goal": goal,
            "context": context,
            "risk": risk.value,
            "category": category.value,
            "budget": budget.value,
            "status": "planning",
            "steps": [],
            "current_step": 0,
            "cost": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._missions[mission_id] = mission

        # Start execution in background
        asyncio.ensure_future(self._execute_mission(mission_id))

        self._audit_log(mission_id, "launched", {"goal": goal, "risk": risk.value, "budget": budget.value})
        logger.info("Mission %s launched: %.60s", mission_id, goal)

        return mission_id

    async def get_status(self, mission_id: str) -> dict[str, Any]:
        """Return the current state of a mission."""
        mission = self._missions.get(mission_id)
        if mission is None:
            return {"error": f"Mission {mission_id} not found."}
        return {
            "id": mission["id"],
            "goal": mission["goal"],
            "status": mission["status"],
            "current_step": mission["current_step"],
            "total_steps": len(mission["steps"]),
            "cost": mission["cost"],
            "risk": mission["risk"],
            "category": mission["category"],
            "budget": mission["budget"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute_mission(self, mission_id: str) -> None:
        mission = self._missions[mission_id]

        try:
            # --- ProjectOrchestrator: plan ---
            plan = await self._plan_mission(mission)
            mission["steps"] = plan.get("steps", [])
            mission["status"] = "executing"
            self._audit_log(mission_id, "plan_complete", {"steps": len(mission["steps"])})

            # --- WorkerAgent: step-by-step execution ---
            for idx, step in enumerate(mission["steps"]):
                mission["current_step"] = idx

                # BudgetGuard
                if not self._check_budget(mission):
                    mission["status"] = "budget_exceeded"
                    self._audit_log(mission_id, "budget_exceeded", {"cost": mission["cost"]})
                    break

                # Verifier (before execution for high risk)
                if mission["risk"] in (RiskLevel.HIGH.value, RiskLevel.CRITICAL.value):
                    ok, msg = await self._verify_step(mission, step)
                    if not ok:
                        mission["status"] = "blocked"
                        self._audit_log(mission_id, "step_blocked", {"step": idx, "reason": msg})
                        break

                # Execute step
                result = await self._execute_step(mission, step)
                step["result"] = result
                mission["cost"] += 1
                self._audit_log(mission_id, "step_complete", {"step": idx, "result_preview": str(result)[:100]})

                # Verifier (after execution)
                ok, msg = await self._verify_step(mission, step)
                if not ok:
                    mission["status"] = "step_failed"
                    self._audit_log(mission_id, "step_failed", {"step": idx, "reason": msg})
                    break

                await asyncio.sleep(0.1)  # small delay between steps

            else:
                mission["status"] = "completed"
                self._audit_log(mission_id, "completed", {})

        except Exception as exc:
            mission["status"] = "failed"
            mission["error"] = str(exc)
            self._audit_log(mission_id, "failed", {"error": str(exc)})
            logger.exception("Mission %s failed.", mission_id)

        mission["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Post-mission reflection → skill candidacy
        await self._post_mission_reflection(mission)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan_mission(self, mission: dict) -> dict:
        prompt = (
            f"Tu es un orchestrateur de projet. Décompose l'objectif suivant en étapes :\n\n"
            f"{mission['goal']}\n\n"
            f"Catégorie : {mission['category']}, Risque : {mission['risk']}, Budget : {mission['budget']}\n\n"
            "Réponds UNIQUEMENT avec un JSON :\n"
            '{"steps": [{"id": 1, "action": "...", "tool": "...", "params": {}}]}'
        )
        raw = await self.router.generate(prompt, tag=SpeedTag.DEEP)
        return self._parse_json(raw)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    async def _execute_step(self, mission: dict, step: dict) -> str:
        action = step.get("action", "")
        tool_name = step.get("tool", "")
        params = step.get("params", {})

        # If a tool is specified, execute it directly
        if tool_name and tool_name in self.registry:
            try:
                result = self.registry.execute(tool_name, **params)
                return str(result)
            except Exception as exc:
                return f"Tool error: {exc}"

        # Otherwise, use LLM to figure out the action
        prompt = (
            f"Mission : {mission['goal']}\n"
            f"Étape actuelle : {action}\n"
            f"Contexte : {json.dumps(mission['context'], ensure_ascii=False)}\n\n"
            "Exécute cette étape et retourne le résultat."
        )
        return await self.router.generate(prompt, tag=SpeedTag.DEEP)

    # ------------------------------------------------------------------
    # Verifier
    # ------------------------------------------------------------------

    async def _verify_step(self, mission: dict, step: dict) -> tuple[bool, str]:
        """Validate a step outcome using the LLM."""
        prompt = (
            f"Tu es un validateur. Vérifie l'étape suivante de la mission :\n"
            f"Mission : {mission['goal']}\n"
            f"Étape : {json.dumps(step, ensure_ascii=False)}\n"
            f"Risque : {mission['risk']}\n\n"
            "Cette étape est-elle valide ? Réponds PASS ou FAIL suivi d'une explication."
        )
        review = await self.router.generate(prompt, tag=SpeedTag.FAST)
        passed = review.strip().upper().startswith("PASS")
        return passed, review

    # ------------------------------------------------------------------
    # BudgetGuard
    # ------------------------------------------------------------------

    def _check_budget(self, mission: dict) -> bool:
        limit = BUDGET_LIMITS.get(Budget(mission["budget"]), float("inf"))
        return mission["cost"] < limit

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def _audit_log(self, mission_id: str, event: str, data: dict) -> None:
        """Append a JSONL entry to the mission audit file."""
        entry = {
            "mission_id": mission_id,
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log_path = self._audit_dir / f"{mission_id}.jsonl"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Audit log write failed: %s", exc)

    # ------------------------------------------------------------------
    # Post-mission reflection
    # ------------------------------------------------------------------

    async def _post_mission_reflection(self, mission: dict) -> None:
        """Reflect on the mission and generate a skill candidate if warranted."""
        if mission["status"] != "completed":
            return

        prompt = (
            f"Analyse cette mission terminée avec succès :\n"
            f"Objectif : {mission['goal']}\n"
            f"Étapes : {json.dumps(mission['steps'], ensure_ascii=False)[:500]}\n\n"
            "Cette mission mérite-t-elle de devenir une compétence réutilisable ? "
            "Si oui, réponds avec un JSON : "
            '{"should_archive": true, "skill_name": "...", "skill_description": "..."}. '
            "Sinon, {\"should_archive\": false}."
        )
        raw = await self.router.generate(prompt, tag=SpeedTag.FAST)
        reflection = self._parse_json(raw)

        if reflection.get("should_archive"):
            await self.eventbus.emit("mission.skill_candidate", {
                "mission_id": mission["id"],
                "goal": mission["goal"],
                "skill_name": reflection.get("skill_name", ""),
                "skill_description": reflection.get("skill_description", ""),
            })
            logger.info("Mission %s → skill candidate: %s", mission["id"], reflection.get("skill_name"))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_json(self, raw: str) -> dict:
        import re
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Could not parse JSON from LLM output.")
            return {}


