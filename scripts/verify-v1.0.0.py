#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path


EXPECTED_PERSONA_SHA256 = "8085ffda1caf7b687fbeebe5c32cdf12a0925cb8c14fe5f19b4118d37d11a7e6"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"v1.0.0 verify: {message}")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    required = [
        "faervell_npc/runtime.py",
        "faervell_npc/discord_bot.py",
        "faervell_npc/services/memory/__init__.py",
        "faervell_npc/services/memory/cortex.py",
        "faervell_npc/services/memory/writer.py",
        "faervell_npc/services/economy.py",
        "faervell_npc/services/characters.py",
        "behavior-pack/persona.md",
        "docs/v1.0.0-release.md",
        "scripts/migrate-v1.0.0.sh",
        "data/economy/economy.sqlite3",
        "data/economy/manifest.json",
        "data/fandom-api-audit.json",
    ]
    for relative in required:
        require((root / relative).exists(), f"не найден {relative}")
    for relative in (
        "faervell_npc/runtime.py",
        "faervell_npc/discord_bot.py",
        "faervell_npc/services/economy.py",
        "faervell_npc/services/characters.py",
        "faervell_npc/services/memory/__init__.py",
        "faervell_npc/services/memory/cortex.py",
        "faervell_npc/services/memory/writer.py",
    ):
        path = root / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    init_text = (root / "faervell_npc/__init__.py").read_text(encoding="utf-8")
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    require('__version__ = "1.0.0"' in init_text, "версия пакета не 1.0.0")
    require(re.search(r'(?m)^version\s*=\s*"1\.0\.0"', project_text) is not None, "версия pyproject не 1.0.0")

    persona = (root / "behavior-pack/persona.md").read_bytes().replace(b"\r\n", b"\n")
    source_path = root / "docs/stranger-persona-source.md"
    if source_path.exists():
        source = source_path.read_bytes().replace(b"\r\n", b"\n")
        require(persona == source, "persona и её архивная копия различаются")
    require(hashlib.sha256(persona).hexdigest() == EXPECTED_PERSONA_SHA256, "личность Странника изменена")

    runtime = (root / "faervell_npc/runtime.py").read_text(encoding="utf-8")
    runtime_ast = ast.parse(runtime)
    orchestrator_ast = ast.parse(
        (root / "faervell_npc/services/orchestrator.py").read_text(encoding="utf-8")
    )
    bot = (root / "faervell_npc/discord_bot.py").read_text(encoding="utf-8")
    deploy = (root / "scripts/deploy-production.sh").read_text(encoding="utf-8")
    require("install_v080_runtime" not in runtime, "legacy v0.8 runtime hook still active")
    require("MemoryService" in runtime and "EconomyService" in runtime, "core services are not wired")
    init = next(
        node
        for node in ast.walk(orchestrator_ast)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    accepted = {argument.arg for argument in init.args.kwonlyargs}
    call = next(
        node
        for node in ast.walk(runtime_ast)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "StrangerOrchestrator"
    )
    passed = {keyword.arg for keyword in call.keywords if keyword.arg}
    require(not (passed - accepted), f"unknown orchestrator arguments: {sorted(passed - accepted)}")
    require("_character_registry_loop" in bot, "periodic character registry sync is missing")
    require("migrate-v1.0.0.sh" in deploy, "production migration is not connected")
    require("docker compose down -v" not in deploy, "production volumes would be deleted")
    require(len(re.findall(r"@stranger\.command\(", bot)) <= 25, "Discord command group exceeds 25 subcommands")

    economy_manifest = json.loads((root / "data/economy/manifest.json").read_text(encoding="utf-8"))
    require(economy_manifest.get("items") == 144768, "economy manifest is incomplete")
    economy_db = root / "data/economy/economy.sqlite3"
    connection = sqlite3.connect(f"file:{economy_db.resolve()}?mode=ro", uri=True)
    try:
        economy_count = connection.execute("SELECT count(*) FROM economy_items").fetchone()[0]
    finally:
        connection.close()
    require(economy_count == 144768, "economy index is incomplete")

    fandom = json.loads((root / "data/fandom-api-audit.json").read_text(encoding="utf-8"))
    require(fandom.get("status") == "OK", "Fandom API audit failed")
    require((fandom.get("siteinfo_stats") or {}).get("articles") == 700, "Fandom article count changed")
    require(fandom.get("readable_articles", 0) >= 689, "Fandom readable corpus is incomplete")
    print("v1.0.0 verify: OK")


if __name__ == "__main__":
    main()
