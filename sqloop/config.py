"""Swappable generator config -- the backbone of the self-improvement loop.

A GeneratorConfig bundles the SQL Generator's prompt template (with {intent} and
{schema} placeholders) plus a list of few-shot examples. Baseline = config v0
(current prompt, no few-shots). The Optimizer proposes candidate configs; the
pipeline can be rebuilt with any config, so baseline and candidate run the exact
same code with only the config swapped (clean A/B).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqloop.prompts import SQL_GENERATOR_INSTRUCTION

CONFIG_DIR = Path(__file__).resolve().parent.parent / "data" / "configs"
ACTIVE_PATH = CONFIG_DIR / "active.json"


@dataclass
class GeneratorConfig:
    prompt: str                                   # instruction template, keeps {intent}/{schema}
    few_shots: list[dict] = field(default_factory=list)  # [{"question","sql"}, ...]
    version: str = "v0"
    notes: str = ""

    def render_instruction(self) -> str:
        """Full SQL Generator instruction: prompt + a few-shot block (if any)."""
        if not self.few_shots:
            return self.prompt
        lines = ["", "", "Examples of correct question -> SQLite SQL:"]
        for fs in self.few_shots:
            lines.append(f"Q: {fs['question']}")
            lines.append(f"SQL: {fs['sql']}")
        return self.prompt + "\n".join(lines)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path) -> "GeneratorConfig":
        data = json.loads(Path(path).read_text())
        return cls(**data)


def baseline_config() -> GeneratorConfig:
    """Config v0: the current hand-written prompt, no few-shots."""
    return GeneratorConfig(
        prompt=SQL_GENERATOR_INSTRUCTION, few_shots=[], version="v0", notes="baseline"
    )


def active_config() -> GeneratorConfig:
    """The config the running pipeline should use.

    Order: env SQLOOP_CONFIG path -> committed active.json -> baseline.
    """
    env_path = os.environ.get("SQLOOP_CONFIG")
    if env_path and Path(env_path).exists():
        return GeneratorConfig.load(env_path)
    if ACTIVE_PATH.exists():
        return GeneratorConfig.load(ACTIVE_PATH)
    return baseline_config()
