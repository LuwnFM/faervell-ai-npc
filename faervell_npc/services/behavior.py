from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import KnowledgeGap, ModelCall


class FileUpdate(BaseModel):
    file: str
    path: list[str] = Field(default_factory=list)
    value: Any


class BehaviorPatch(BaseModel):
    patch_id: str
    base_version: str
    reason: str
    author: str = "GM"
    add_examples: list[dict[str, Any]] = Field(default_factory=list)
    update_files: list[FileUpdate] = Field(default_factory=list)
    add_tests: list[dict[str, Any]] = Field(default_factory=list)


class BehaviorManager:
    ALLOWED_FILES = {
        "dialogue-policy.yaml",
        "disclosure-rules.yaml",
        "memory-policy.yaml",
        "quest-templates.yaml",
        "profession-masks.yaml",
        "routing-rules.yaml",
    }

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = Path(self.settings.behavior_pack_path)

    async def scan(self, session: AsyncSession, days: int = 30) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=days)
        gaps = (
            await session.execute(
                select(KnowledgeGap.question, func.count(KnowledgeGap.id).label("count"))
                .where(KnowledgeGap.created_at >= since, KnowledgeGap.status == "PENDING")
                .group_by(KnowledgeGap.question)
                .order_by(func.count(KnowledgeGap.id).desc())
                .limit(100)
            )
        ).all()
        errors = (
            await session.execute(
                select(ModelCall.kind, ModelCall.model, ModelCall.error, ModelCall.created_at)
                .where(ModelCall.created_at >= since, ModelCall.success.is_(False))
                .order_by(ModelCall.created_at.desc())
                .limit(100)
            )
        ).all()
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "days": days,
            "pending_knowledge_gaps": [
                {"question": question, "count": count} for question, count in gaps
            ],
            "recent_model_errors": [
                {
                    "kind": kind,
                    "model": model,
                    "error": error,
                    "created_at": created_at.isoformat(),
                }
                for kind, model, error, created_at in errors
            ],
            "instruction": (
                "Review only important repeated cases. Convert approved decisions into a versioned patch "
                "with tests; never auto-edit IDENTITY_CORE or canon."
            ),
        }

    def export_scan(self, report: dict[str, Any], destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination

    def validate_patch(self, patch_path: Path) -> BehaviorPatch:
        raw = yaml.safe_load(patch_path.read_text(encoding="utf-8"))
        patch = BehaviorPatch.model_validate(raw)
        current = self.current_version()
        if patch.base_version != current:
            raise ValueError(f"Patch base_version={patch.base_version}, current={current}")
        for update in patch.update_files:
            if update.file not in self.ALLOWED_FILES:
                raise ValueError(f"File is not patchable: {update.file}")
        return patch

    def apply_patch(self, patch_path: Path) -> str:
        patch = self.validate_patch(patch_path)
        previous_version = self.current_version()
        history = self.root / "history" / previous_version
        if history.exists():
            shutil.rmtree(history)
        history.mkdir(parents=True, exist_ok=True)
        for path in self.root.iterdir():
            if path.name == "history":
                continue
            target = history / path.name
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)

        for update in patch.update_files:
            file_path = self.root / update.file
            data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
            cursor = data
            for key in update.path[:-1]:
                cursor = cursor.setdefault(key, {})
            if update.path:
                cursor[update.path[-1]] = update.value
            else:
                data = update.value
            file_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

        if patch.add_examples:
            examples = self.root / "approved-examples.jsonl"
            with examples.open("a", encoding="utf-8") as handle:
                for example in patch.add_examples:
                    handle.write(json.dumps(example, ensure_ascii=False) + "\n")

        if patch.add_tests:
            test_file = self.root / "tests" / f"patch-{patch.patch_id}.jsonl"
            with test_file.open("w", encoding="utf-8") as handle:
                for test in patch.add_tests:
                    handle.write(json.dumps(test, ensure_ascii=False) + "\n")

        version_data = json.loads((self.root / "version.json").read_text(encoding="utf-8"))
        version_data.update(
            {
                "version": patch.patch_id,
                "previous_version": previous_version,
                "updated_at": datetime.now(UTC).isoformat(),
                "reason": patch.reason,
                "author": patch.author,
            }
        )
        (self.root / "version.json").write_text(
            json.dumps(version_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return patch.patch_id

    def rollback(self, version: str) -> str:
        source = self.root / "history" / version
        if not source.exists():
            raise FileNotFoundError(f"No behavior backup for version {version}")
        for path in self.root.iterdir():
            if path.name == "history":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in source.iterdir():
            target = self.root / path.name
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
        return self.current_version()

    def current_version(self) -> str:
        data = json.loads((self.root / "version.json").read_text(encoding="utf-8"))
        return str(data["version"])
