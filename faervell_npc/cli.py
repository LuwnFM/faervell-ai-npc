from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from sqlalchemy import delete, select

from faervell_npc.db import SessionLocal, close_db, init_db
from faervell_npc.models import CachedDecision
from faervell_npc.services.behavior import BehaviorManager
from faervell_npc.services.ingest import SourceIngestor

app = typer.Typer(help="Faervell Stranger NPC administration CLI")
behavior_app = typer.Typer(help="Manual versioned behavior updater")
decision_app = typer.Typer(help="Review reusable planner decision candidates")
app.add_typer(behavior_app, name="behavior")
app.add_typer(decision_app, name="decision")


@app.command("init-db")
def init_database() -> None:
    """Create PostgreSQL extensions, tables and append-only archive trigger."""

    async def runner() -> None:
        await init_db()
        await close_db()

    asyncio.run(runner())
    typer.echo("Database initialized.")


@app.command("ingest")
def ingest(
    manifest: Path = typer.Argument(Path("data/sources.yaml"), exists=True, readable=True),
) -> None:
    """Ingest local files, web pages and the Fandom wiki into pgvector."""

    async def runner() -> dict[str, object]:
        ingestor = SourceIngestor()
        try:
            async with SessionLocal() as session:
                return await ingestor.ingest_manifest(session, manifest)
        finally:
            await ingestor.close()
            await close_db()

    report = asyncio.run(runner())
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@behavior_app.command("scan")
def behavior_scan(
    days: int = typer.Option(30, min=1, max=365),
    output: Path = typer.Option(Path("data/exports/behavior-scan.json")),
) -> None:
    """Export important recent gaps/errors without changing behavior."""

    manager = BehaviorManager()

    async def runner() -> dict[str, object]:
        async with SessionLocal() as session:
            return await manager.scan(session, days)

    report = asyncio.run(runner())
    manager.export_scan(report, output)
    typer.echo(str(output))


@behavior_app.command("validate")
def behavior_validate(patch: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    manager = BehaviorManager()
    parsed = manager.validate_patch(patch)
    typer.echo(f"Valid patch: {parsed.patch_id}")


@behavior_app.command("apply")
def behavior_apply(patch: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    manager = BehaviorManager()
    version = manager.apply_patch(patch)
    typer.echo(f"Applied behavior version: {version}")


@behavior_app.command("rollback")
def behavior_rollback(version: str) -> None:
    manager = BehaviorManager()
    restored = manager.rollback(version)
    typer.echo(f"Restored behavior version: {restored}")


@decision_app.command("list")
def decision_list(
    pending_only: bool = typer.Option(True, "--pending-only/--all"),
    limit: int = typer.Option(30, min=1, max=500),
) -> None:
    """List cached planner results awaiting explicit approval."""

    async def runner() -> list[dict[str, object]]:
        async with SessionLocal() as session:
            statement = select(CachedDecision).order_by(CachedDecision.created_at.desc()).limit(limit)
            if pending_only:
                statement = statement.where(CachedDecision.approved.is_(False))
            records = (await session.execute(statement)).scalars().all()
            return [
                {
                    "fingerprint": record.fingerprint,
                    "route": record.route,
                    "approved": record.approved,
                    "hits": record.hit_count,
                    "request": record.request_summary,
                }
                for record in records
            ]

    typer.echo(json.dumps(asyncio.run(runner()), ensure_ascii=False, indent=2))


@decision_app.command("approve")
def decision_approve(fingerprint: str = typer.Argument(..., min=8)) -> None:
    """Approve one exact decision fingerprint for safe local reuse."""

    async def runner() -> str:
        async with SessionLocal() as session:
            records = (
                await session.execute(
                    select(CachedDecision).where(CachedDecision.fingerprint.startswith(fingerprint))
                )
            ).scalars().all()
            if not records:
                raise typer.BadParameter("Decision fingerprint not found")
            if len(records) > 1:
                raise typer.BadParameter("Fingerprint prefix is ambiguous")
            records[0].approved = True
            await session.commit()
            return records[0].fingerprint

    typer.echo(f"Approved decision: {asyncio.run(runner())}")


@decision_app.command("reject")
def decision_reject(fingerprint: str = typer.Argument(..., min=8)) -> None:
    """Delete an unsafe or unhelpful decision candidate."""

    async def runner() -> int:
        async with SessionLocal() as session:
            records = (
                await session.execute(
                    select(CachedDecision.id).where(CachedDecision.fingerprint.startswith(fingerprint))
                )
            ).scalars().all()
            if not records:
                raise typer.BadParameter("Decision fingerprint not found")
            if len(records) > 1:
                raise typer.BadParameter("Fingerprint prefix is ambiguous")
            await session.execute(delete(CachedDecision).where(CachedDecision.id == records[0]))
            await session.commit()
            return 1

    typer.echo(f"Rejected decisions: {asyncio.run(runner())}")


if __name__ == "__main__":
    app()
