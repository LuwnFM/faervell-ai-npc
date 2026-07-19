#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

EXPECTED_PERSONA_SHA256 = "ad4a463496c4655f7dc8e20e41ffaf1ff48da65f9011d8874f4793143bbf08e8"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"v0.8.0 verify: {message}")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    required = [
        "faervell_npc/runtime.py",
        "faervell_npc/discord_bot.py",
        "faervell_npc/services/v080_grounding.py",
        "faervell_npc/services/v080_runtime.py",
        "faervell_npc/services/discord_knowledge.py",
        "behavior-pack/persona.md",
        "behavior-pack/profession-masks.yaml",
        "docs/stranger-persona-source.md",
        "docs/architecture-source.md",
        "scripts/migrate-v0.8.0.sh",
        "tests/test_v080_grounding.py",
    ]
    for relative in required:
        require((root / relative).exists(), f"не найден {relative}")

    for relative in (
        "faervell_npc/runtime.py",
        "faervell_npc/discord_bot.py",
        "faervell_npc/services/v080_grounding.py",
        "faervell_npc/services/v080_runtime.py",
        "faervell_npc/services/discord_knowledge.py",
        "scripts/verify-v0.8.0.py",
        "tests/test_v080_grounding.py",
    ):
        path = root / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    init_text = (root / "faervell_npc/__init__.py").read_text(encoding="utf-8")
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    require('__version__ = "0.8.0"' in init_text, "версия пакета не 0.8.0")
    require(re.search(r'(?m)^version\s*=\s*"0\.8\.0"', project_text) is not None, "версия pyproject не 0.8.0")

    persona = (root / "behavior-pack/persona.md").read_bytes()
    source = (root / "docs/stranger-persona-source.md").read_bytes()
    require(persona == source, "persona и её архивная копия различаются")
    require(hashlib.sha256(persona).hexdigest() == EXPECTED_PERSONA_SHA256, "личность Странника неполная или изменена")

    architecture = (root / "docs/architecture-source.md").read_text(encoding="utf-8")
    require("Версия системы:** `0.8.0`" in architecture, "архитектура не обновлена до 0.8.0")
    require("Дополнение v0.8.0" in architecture, "в архитектуре нет описания v0.8.0")

    masks = (root / "behavior-pack/profession-masks.yaml").read_text(encoding="utf-8")
    require(masks.count("can_trade: true") >= 5, "не все маски могут торговать")
    require(masks.count("quest_capability: any") >= 5, "не все маски могут выдавать любые квесты")
    expected_templates = "[DELIVER_ITEM, INVESTIGATE, FIND_LOCATION, COLLECT, CRAFT, REPAIR, ESCORT]"
    require(masks.count(expected_templates) >= 5, "в масках остались ограничения шаблонов квестов")

    runtime = (root / "faervell_npc/runtime.py").read_text(encoding="utf-8")
    bot = (root / "faervell_npc/discord_bot.py").read_text(encoding="utf-8")
    deploy = (root / "scripts/deploy-production.sh").read_text(encoding="utf-8")
    require("install_v080_runtime" in runtime, "runtime-hook не установлен")
    require("sync_discord_knowledge" in bot, "новая locations_sync не установлена")
    require('action: str = "scan"' in bot, "режимы behavior_scan не установлены")
    require("migrate-v0.8.0.sh" in deploy, "production migration не подключена")
    require("docker compose down -v" not in deploy, "обнаружено удаление production volumes")

    command_count = len(re.findall(r"@stranger\.command\(", bot))
    require(command_count <= 25, f"slash-подкоманд {command_count}, Discord допускает не более 25")

    print("v0.8.0 verify: OK")
    print(f"slash-команд /stranger: {command_count}")
    print(f"persona sha256: {EXPECTED_PERSONA_SHA256}")


if __name__ == "__main__":
    main()
