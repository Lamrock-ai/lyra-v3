"""Pipeline — multi-agent code generation pipeline (Architect → Builder → Reviewer → Tester)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.providers.llm.router import LLMRouter, SpeedTag
from src.providers.tools.registry import ToolRegistry
from .memory import MemoryOrchestrator

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

STRICT_DOMAINS = {"réseau", "caméra", "moteur", "network", "camera", "motor"}


@dataclass
class PipelineResult:
    """Result of a full pipeline run."""
    plan: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    review: dict = field(default_factory=dict)
    test_results: dict = field(default_factory=dict)
    success: bool = False
    attempts: int = 0


class Pipeline:
    """Run the full LYRA pipeline for a given goal."""

    def __init__(
        self,
        router: LLMRouter,
        registry: ToolRegistry,
        memory: MemoryOrchestrator,
    ) -> None:
        self.router = router
        self.registry = registry
        self.memory = memory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        goal: str,
        context: Optional[dict] = None,
    ) -> dict:
        """Execute the full pipeline: Architect → Builder → Reviewer → Tester.

        Returns a PipelineResult as a dict.
        """
        context = context or {}
        strict = self._is_strict(goal)

        last_error = ""
        for attempt in range(1, MAX_RETRIES + 1):
            logger.info("Pipeline attempt %d/%d for goal: %.60s", attempt, MAX_RETRIES, goal)
            try:
                result = await self._execute_pipeline(goal, context, strict)
                result.attempts = attempt
                if result.success:
                    await self._store_success(goal, result)
                    return self._to_dict(result)
                last_error = f"Review/Test failed (attempt {attempt})"
            except Exception as exc:
                last_error = f"Pipeline error: {exc}"
                logger.exception("Pipeline attempt %d failed", attempt)

        return {
            "success": False,
            "error": last_error,
            "attempts": MAX_RETRIES,
        }

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    async def _execute_pipeline(
        self,
        goal: str,
        context: dict,
        strict: bool,
    ) -> PipelineResult:
        result = PipelineResult()

        # --- 1. Architect ---
        plan_prompt = self._build_architect_prompt(goal, context)
        plan_raw = await self.router.generate(plan_prompt, tag=SpeedTag.DEEP)
        result.plan = self._parse_json(plan_raw, "plan")

        # --- 2. Builder ---
        builder_prompt = self._build_builder_prompt(goal, result.plan, context)
        code_raw = await self.router.generate(builder_prompt, tag=SpeedTag.DEEP)
        result.files = self._parse_code_json(code_raw)

        # --- 3. Reviewer ---
        review_ok, review_commentary = await self._run_review(result.files, strict)
        result.review = {"passed": review_ok, "commentary": review_commentary}
        if not review_ok:
            return result

        # --- 4. Tester ---
        test_ok, test_detail = await self._run_tests(result.files)
        result.test_results = {"passed": test_ok, "detail": test_detail}
        if test_ok:
            result.success = True

        return result

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _build_architect_prompt(self, goal: str, context: dict) -> str:
        ctx_str = json.dumps(context, indent=2, ensure_ascii=False)
        return (
            f"Tu es un architecte logiciel. Analyse l'objectif suivant :\n\n{goal}\n\n"
            f"Contexte :\n{ctx_str}\n\n"
            "Produis un plan JSON avec les clés : modules (liste), dependencies (dict), "
            "risks (liste d'objets avec level et description), order (liste ordonnée)."
            "\nRéponds UNIQUEMENT avec un bloc JSON ```json ... ```."
        )

    def _build_builder_prompt(self, goal: str, plan: dict, context: dict) -> str:
        plan_str = json.dumps(plan, indent=2, ensure_ascii=False)
        return (
            f"Génère le code Python pour l'objectif : {goal}\n\n"
            f"Plan d'architecture :\n{plan_str}\n\n"
            "Réponds UNIQUEMENT avec un bloc JSON ```json\n{{\n  \"files\": [\n    "
            "{{\"path\": \"module.py\", \"content\": \"code\"}}\n  ]\n}}\n```"
        )

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    async def _run_review(
        self,
        files: list[dict],
        strict: bool,
    ) -> tuple[bool, str]:
        """Run the reviewer LLM agent + basic static checks."""
        basic_ok, basic_msg = self._static_checks(files)
        if not basic_ok:
            return False, f"Static check failed: {basic_msg}"

        files_json = json.dumps(files, indent=2, ensure_ascii=False)
        strict_note = (
            " (mode STRICT : vérifie particulièrement la sécurité réseau, "
            "les accès caméra et le contrôle moteur)" if strict else ""
        )
        prompt = (
            f"Tu es un reviewer de code Python.{strict_note}\n\n"
            f"Code à reviewer :\n{files_json}\n\n"
            "Vérifie : sécurité, bonnes pratiques, gestion d'erreurs, typage.\n"
            "Réponds avec PASS ou FAIL suivi d'une explication."
        )

        review_text = await self.router.generate(prompt, tag=SpeedTag.DEEP)
        passed = review_text.strip().upper().startswith("PASS")
        return passed, review_text

    def _static_checks(self, files: list[dict]) -> tuple[bool, str]:
        """Basic static analysis without LLM."""
        for f in files:
            path = f.get("path", "")
            content = f.get("content", "")
            if not path or not content:
                return False, f"Fichier avec path ou content vide : {path}"
            if not path.endswith(".py") and any(kw in path for kw in (".py",)):
                continue
            # Check for syntax errors
            if path.endswith(".py"):
                try:
                    compile(content, path, "exec")
                except SyntaxError as e:
                    return False, f"Erreur de syntaxe dans {path} : {e}"
        return True, "ok"

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    async def _run_tests(self, files: list[dict]) -> tuple[bool, str]:
        """Write files to a temp dir and run pytest."""
        py_files = [f for f in files if f["path"].endswith(".py")]
        if not py_files:
            return True, "Aucun fichier .py à tester."

        with tempfile.TemporaryDirectory(prefix="lyra_pipeline_") as tmpdir:
            for f in py_files:
                dst = Path(tmpdir) / f["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(f["content"], encoding="utf-8")

            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pytest", tmpdir, "--tb=short", "--no-header", "-q",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            except (subprocess.TimeoutExpired, asyncio.TimeoutError):
                return False, "Tests interrompus (timeout 60s)."
            except FileNotFoundError:
                return False, "pytest n'est pas installé."

        output = (stdout + stderr).decode("utf-8", errors="replace")
        passed = proc.returncode == 0 if hasattr(proc, 'returncode') else False
        if hasattr(proc, 'returncode') and proc.returncode == 0:
            return True, output
        return False, output

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_strict(self, goal: str) -> bool:
        goal_lower = goal.lower()
        return any(domain in goal_lower for domain in STRICT_DOMAINS)

    def _parse_json(self, raw: str, label: str) -> dict:
        """Extract JSON from LLM output (may be wrapped in markdown)."""
        # Try to extract from ```json ... ``` block
        import re
        m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Could not parse %s JSON from LLM output", label)
            return {}

    def _parse_code_json(self, raw: str) -> list[dict]:
        """Parse the code output — expects JSON with a 'files' key."""
        data = self._parse_json(raw, "code")
        if isinstance(data, dict) and "files" in data:
            return data["files"]
        # Fallback: try to extract files directly
        if isinstance(data, list):
            return data
        return []

    async def _store_success(self, goal: str, result: PipelineResult) -> None:
        """Store a successful pipeline run in memory."""
        try:
            await self.memory.store_event("pipeline.success", {
                "goal": goal[:200],
                "files": [f["path"] for f in result.files],
                "attempts": result.attempts,
            })
        except Exception:
            pass

    @staticmethod
    def _to_dict(result: PipelineResult) -> dict:
        return {
            "success": result.success,
            "plan": result.plan,
            "files": result.files,
            "review": result.review,
            "test_results": result.test_results,
            "attempts": result.attempts,
        }


import sys  # noqa: E402 — needed for subprocess call


