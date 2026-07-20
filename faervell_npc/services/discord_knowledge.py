from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord
from sqlalchemy import select, update

from faervell_npc.db import SessionLocal
from faervell_npc.models import GMReviewRequest, KnowledgeChunk, SceneConfig
from faervell_npc.services.ingest import ImportedDocument, SourceIngestor

WORLD_NEWS_FORUM_ID = int(os.getenv("DISCORD_WORLD_NEWS_FORUM_ID", "1320514974396973186"))
TRUSTED_WORLD_NEWS_AUTHOR_IDS = {
    item.strip()
    for item in os.getenv(
        "DISCORD_WORLD_NEWS_AUTHOR_IDS",
        "855605848105287711,331217779019481089",
    ).split(",")
    if item.strip()
}
SYNC_TIMEOUT_SECONDS = max(60, int(os.getenv("DISCORD_LOCATION_SYNC_TIMEOUT_SECONDS", "480")))
PER_CHANNEL_TIMEOUT_SECONDS = max(20, int(os.getenv("DISCORD_LOCATION_CHANNEL_TIMEOUT_SECONDS", "90")))
MAX_THREADS_PER_CONTAINER = max(20, int(os.getenv("DISCORD_LOCATION_MAX_THREADS", "500")))
MAX_MESSAGES_PER_SCENE = max(50, int(os.getenv("DISCORD_LOCATION_MAX_MESSAGES", "2000")))
SYNC_CONCURRENCY = max(1, min(8, int(os.getenv("DISCORD_LOCATION_SYNC_CONCURRENCY", "3"))))
LONG_MESSAGE_MIN_CHARS = max(100, int(os.getenv("DISCORD_LOCATION_LONG_MESSAGE_MIN_CHARS", "220")))

FANDOM_LINK_RE = re.compile(r"https?://(?:www\.)?faervellrp\.fandom\.com/[^\s<>]+", re.I)
SEPARATOR_RE = re.compile(r"[━═─]{5,}|》\s*[❈✦*]\s*《")
WORLD_DATE_RE = re.compile(
    r"(?iu)(?:на\s+момент\s+)?(\d{1,2}[./-]\d{1,2}[./-](?:1\d{3}|\d{3,4}))\s*(?:года|г\.)?"
)
SEASON_YEAR_RE = re.compile(r"(?iu)\b(весна|лето|осень|зима)\s+(1\d{3}|\d{3,4})\s+года\b")
DESCRIPTION_HINT_RE = re.compile(
    r"(?iu)\b(?:находится|располож|представляет\s+собой|внутри|снаружи|здание|зал|улица|район|"
    r"локаци|помещени|этаж|башн|рынок|кузн|порт|дорог|ворот|вход|выход|стен|территори|"
    r"город|деревн|крепост|дворец|храм|таверн|площад|мастерск|док|шахт)\w*\b"
)
OFFTOP_RE = re.compile(
    r"(?iu)\b(?:youtube|youtu\.be|тикток|tiktok|хд\b|рофл|мем|скинь\s+хуй|это\s+вс[её]\s+фейк)\b"
)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class SyncReport:
    scenes_created: int = 0
    scenes_updated: int = 0
    containers_seen: int = 0
    threads_seen: int = 0
    documents: int = 0
    chunks: int = 0
    location_messages: int = 0
    news_documents: int = 0
    news_skipped: int = 0
    gm_reviews: int = 0
    timed_out: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenes_created": self.scenes_created,
            "scenes_updated": self.scenes_updated,
            "containers_seen": self.containers_seen,
            "threads_seen": self.threads_seen,
            "documents": self.documents,
            "chunks": self.chunks,
            "location_messages": self.location_messages,
            "news_documents": self.news_documents,
            "news_skipped": self.news_skipped,
            "gm_reviews": self.gm_reviews,
            "timed_out": self.timed_out,
            "errors": self.errors,
        }


async def sync_discord_knowledge(
    bot: discord.Client,
    guild_id: int,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    guild = bot.get_guild(guild_id)
    if guild is None:
        guild = await bot.fetch_guild(guild_id)
    report = SyncReport()
    semaphore = asyncio.Semaphore(SYNC_CONCURRENCY)
    ingestor = SourceIngestor()

    async def emit(stage: str, current: str = "") -> None:
        if progress is None:
            return
        await progress({"stage": stage, "current": current, **report.as_dict()})

    async def bounded(item: Any) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(PER_CHANNEL_TIMEOUT_SECONDS):
                    await _sync_container(bot, guild, item, ingestor, report, emit)
            except TimeoutError:
                report.timed_out += 1
                report.errors.append(f"timeout:{getattr(item, 'id', '?')}:{getattr(item, 'name', '?')}")
            except Exception as exc:
                report.errors.append(
                    f"{getattr(item, 'id', '?')}:{getattr(item, 'name', '?')}:{type(exc).__name__}:{str(exc)[:240]}"
                )

    try:
        async with asyncio.timeout(SYNC_TIMEOUT_SECONDS):
            await emit("start")
            containers = _location_containers(bot, guild)
            report.containers_seen = len(containers)
            for offset in range(0, len(containers), SYNC_CONCURRENCY):
                batch = containers[offset : offset + SYNC_CONCURRENCY]
                await asyncio.gather(*(bounded(item) for item in batch))
                await emit("containers", f"{min(offset + len(batch), len(containers))}/{len(containers)}")
            news = guild.get_channel(WORLD_NEWS_FORUM_ID)
            if isinstance(news, discord.ForumChannel):
                await _sync_world_news(guild, news, ingestor, report, emit)
    except TimeoutError:
        report.timed_out += 1
        report.errors.append("overall_sync_timeout")
    finally:
        await ingestor.close()
    await emit("done")
    return report.as_dict()


def _location_containers(bot: discord.Client, guild: discord.Guild) -> list[Any]:
    settings = getattr(bot, "settings", None)
    category_ids: set[int] = set()
    for name in (
        "traveler_rp_category_ids",
        "traveler_manual_only_category_ids",
    ):
        for value in getattr(settings, name, []) or []:
            try:
                category_ids.add(int(value))
            except (TypeError, ValueError):
                continue
    event_id = getattr(settings, "traveler_events_category_id", None)
    if event_id:
        category_ids.add(int(event_id))
    if not category_ids:
        return [
            channel
            for channel in guild.channels
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
            and channel.id != WORLD_NEWS_FORUM_ID
        ]
    return [
        channel
        for channel in guild.channels
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
        and channel.id != WORLD_NEWS_FORUM_ID
        and channel.category_id in category_ids
    ]


async def _sync_container(
    bot: discord.Client,
    guild: discord.Guild,
    container: discord.TextChannel | discord.ForumChannel,
    ingestor: SourceIngestor,
    report: SyncReport,
    emit: Callable[[str, str], Awaitable[None]],
) -> None:
    await emit("container", container.name)
    await _upsert_scene(container, guild, report, is_container=isinstance(container, discord.ForumChannel))

    if isinstance(container, discord.TextChannel):
        document = await _location_document(container, parent=None)
    else:
        document = _forum_container_document(container)
    if document:
        await _store(document, ingestor, report, access="PUBLIC_LOCAL_EVENT")

    threads = await _collect_threads(container)
    report.threads_seen += len(threads)
    for thread in threads:
        await _upsert_scene(thread, guild, report, is_container=False)
        document = await _location_document(thread, parent=container)
        if document:
            await _store(document, ingestor, report, access="PUBLIC_LOCAL_EVENT")


def _forum_container_document(channel: discord.ForumChannel) -> ImportedDocument | None:
    topic = (channel.topic or "").strip()
    if not topic:
        return None
    path = _scene_path(channel)
    sections = [("Описание локации-контейнера", topic)]
    return ImportedDocument(
        source_id=f"discord_location:{channel.id}",
        title=f"Локация: {path or _clean_name(channel.name)}",
        url=f"https://discord.com/channels/{channel.guild.id}/{channel.id}",
        revision=str(getattr(channel, "last_message_id", "") or channel.id),
        text=f"## Описание локации-контейнера\n{topic}",
        sections=sections,
        metadata={
            "source_kind": "DISCORD_LOCATION_LORE",
            "guild_id": str(channel.guild.id),
            "channel_id": str(channel.id),
            "parent_id": None,
            "location_path": path,
            "selected_messages": 0,
            "fandom_links": sorted(set(FANDOM_LINK_RE.findall(topic))),
            "is_forum_container": True,
        },
    )


async def _collect_threads(
    container: discord.TextChannel | discord.ForumChannel,
) -> list[discord.Thread]:
    result: list[discord.Thread] = []
    seen: set[int] = set()
    for thread in list(container.threads):
        if thread.id not in seen:
            seen.add(thread.id)
            result.append(thread)
    try:
        async for thread in container.archived_threads(limit=MAX_THREADS_PER_CONTAINER):
            if thread.id not in seen:
                seen.add(thread.id)
                result.append(thread)
            if len(result) >= MAX_THREADS_PER_CONTAINER:
                break
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass
    return result[:MAX_THREADS_PER_CONTAINER]


async def _upsert_scene(
    channel: discord.abc.GuildChannel | discord.Thread,
    guild: discord.Guild,
    report: SyncReport,
    *,
    is_container: bool,
) -> None:
    parent = channel.parent if isinstance(channel, discord.Thread) else None
    category = getattr(channel, "category", None) or (
        getattr(parent, "category", None) if parent is not None else None
    )
    parts = [
        _clean_name(category.name) if category else "",
        _clean_name(parent.name) if parent else "",
        _clean_name(channel.name),
    ]
    path = " / ".join(item for item in parts if item)
    location_name = _clean_name(channel.name)
    async with SessionLocal() as session:
        scene = await session.get(SceneConfig, str(channel.id))
        if scene is None:
            scene = SceneConfig(
                channel_id=str(channel.id),
                guild_id=str(guild.id),
                enabled=not is_container,
                location_id=_slug(path),
                location_name=location_name,
                profession_mask_id="traveler",
                category_id=str(category.id) if category else None,
                category_name=_clean_name(category.name) if category else None,
                location_path=path,
                automatic_appearance_allowed=not is_container,
            )
            session.add(scene)
            report.scenes_created += 1
        else:
            scene.guild_id = str(guild.id)
            scene.location_id = scene.location_id or _slug(path)
            scene.location_name = location_name
            scene.category_id = str(category.id) if category else None
            scene.category_name = _clean_name(category.name) if category else None
            scene.location_path = path
            if is_container:
                scene.enabled = False
                scene.automatic_appearance_allowed = False
            report.scenes_updated += 1
        await session.commit()


async def _location_document(
    channel: discord.TextChannel | discord.Thread,
    *,
    parent: discord.TextChannel | discord.ForumChannel | None,
) -> ImportedDocument | None:
    sections: list[tuple[str, str]] = []
    topic = ""
    if isinstance(channel, discord.TextChannel):
        topic = channel.topic or ""
    elif isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
        topic = parent.topic or ""
    if topic.strip():
        sections.append(("Описание локации", topic.strip()))

    selected: list[discord.Message] = []
    try:
        async for message in channel.history(limit=MAX_MESSAGES_PER_SCENE, oldest_first=True):
            if message.author.bot:
                continue
            if _is_location_lore_message(message, is_first=not selected):
                selected.append(message)
    except (discord.Forbidden, discord.HTTPException):
        selected = []

    for message in selected:
        content = _clean_message(message.content)
        links = FANDOM_LINK_RE.findall(message.content or "")
        if links:
            content += "\nСвязанные статьи вики: " + " ".join(links)
        if content.strip():
            sections.append(
                (
                    f"Сообщение {message.created_at:%Y-%m-%d} — {message.author.display_name}",
                    content.strip(),
                )
            )
    if not sections:
        return None

    name = _clean_name(channel.name)
    path = _scene_path(channel)
    text = "\n\n".join(f"## {title}\n{body}" for title, body in sections)
    latest = max((message.created_at for message in selected), default=datetime.now(UTC))
    return ImportedDocument(
        source_id=f"discord_location:{channel.id}",
        title=f"Локация: {path or name}",
        url=f"https://discord.com/channels/{channel.guild.id}/{channel.id}",
        revision=latest.isoformat(),
        text=text,
        sections=sections,
        metadata={
            "source_kind": "DISCORD_LOCATION_LORE",
            "guild_id": str(channel.guild.id),
            "channel_id": str(channel.id),
            "parent_id": str(channel.parent_id) if isinstance(channel, discord.Thread) else None,
            "location_path": path,
            "selected_messages": len(selected),
            "fandom_links": sorted(set(FANDOM_LINK_RE.findall(text))),
        },
    )


def _is_location_lore_message(message: discord.Message, *, is_first: bool) -> bool:
    content = (message.content or "").strip()
    if not content:
        return False
    if OFFTOP_RE.search(content) and not FANDOM_LINK_RE.search(content):
        return False
    if FANDOM_LINK_RE.search(content):
        return True
    if is_first and len(content) >= 100:
        return True
    if message.reference is not None and len(content) < 500:
        return False
    if content.endswith("?") and len(content) < 450:
        return False
    structured = bool(
        DESCRIPTION_HINT_RE.search(content)
        or SEPARATOR_RE.search(content)
        or "\n\n" in content
        or len(content) >= 420
    )
    # Long descriptive posts are copied regardless of author role. This is important for
    # location forums where players and event staff publish the actual room description.
    return len(content) >= LONG_MESSAGE_MIN_CHARS and structured


async def _sync_world_news(
    guild: discord.Guild,
    forum: discord.ForumChannel,
    ingestor: SourceIngestor,
    report: SyncReport,
    emit: Callable[[str, str], Awaitable[None]],
) -> None:
    threads = await _collect_threads(forum)
    for thread in threads:
        await emit("world_news", thread.name)
        try:
            async with asyncio.timeout(PER_CHANNEL_TIMEOUT_SECONDS):
                messages: list[discord.Message] = []
                async for message in thread.history(limit=MAX_MESSAGES_PER_SCENE, oldest_first=True):
                    if message.author.bot:
                        continue
                    if str(message.author.id) not in TRUSTED_WORLD_NEWS_AUTHOR_IDS:
                        break
                    content = _clean_message(message.content)
                    if not content:
                        continue
                    if messages and not _is_world_news_continuation(content):
                        break
                    messages.append(message)
                if not messages:
                    continue
                combined = "\n\n".join(_clean_message(item.content) for item in messages if item.content)
                if not _is_structured_world_news(thread.name, combined):
                    report.news_skipped += 1
                    if len(combined) >= 250:
                        await _create_source_review(guild, thread, messages[0], combined, report)
                    continue
                date_label = _extract_world_date_label(thread.name, combined)
                sections = _split_news_sections(combined)
                document = ImportedDocument(
                    source_id=f"discord_world_news:{thread.id}",
                    title=f"Новости мира: {_clean_name(thread.name)}",
                    url=f"https://discord.com/channels/{guild.id}/{thread.id}",
                    revision=messages[-1].edited_at.isoformat() if messages[-1].edited_at else messages[-1].created_at.isoformat(),
                    text=combined,
                    sections=sections,
                    metadata={
                        "source_kind": "DISCORD_WORLD_NEWS",
                        "guild_id": str(guild.id),
                        "forum_id": str(forum.id),
                        "thread_id": str(thread.id),
                        "author_id": str(messages[0].author.id),
                        "author_name": messages[0].author.display_name,
                        "discord_created_at": messages[0].created_at.isoformat(),
                        "world_date_label": date_label,
                        "trusted_author": True,
                    },
                )
                await _store(document, ingestor, report, access="PUBLIC_GLOBAL_EVENT")
                report.news_documents += 1
        except TimeoutError:
            report.timed_out += 1
            report.errors.append(f"news_timeout:{thread.id}:{thread.name}")
        except Exception as exc:
            report.errors.append(f"news:{thread.id}:{type(exc).__name__}:{str(exc)[:240]}")


async def _create_source_review(
    guild: discord.Guild,
    thread: discord.Thread,
    message: discord.Message,
    content: str,
    report: SyncReport,
) -> None:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    async with SessionLocal() as session:
        existing_reviews = list(
            (
                await session.execute(
                    select(GMReviewRequest).where(
                        GMReviewRequest.request_type == "KNOWLEDGE_SOURCE_REVIEW",
                        GMReviewRequest.channel_id == str(thread.id),
                    )
                )
            ).scalars()
        )
        if any(
            str((review.payload or {}).get("content_hash") or "") == content_hash
            for review in existing_reviews
        ):
            return
        review = GMReviewRequest(
            guild_id=str(guild.id),
            scene_id=f"news:{thread.id}",
            channel_id=str(thread.id),
            player_discord_user_id=str(message.author.id),
            character_id="system:world_news",
            request_type="KNOWLEDGE_SOURCE_REVIEW",
            reason="trusted_author_post_did_not_match_news_structure",
            payload={
                "thread_id": str(thread.id),
                "thread_name": thread.name,
                "author_id": str(message.author.id),
                "content_hash": content_hash,
                "preview": content[:1800],
            },
        )
        session.add(review)
        await session.commit()
        report.gm_reviews += 1


async def _store(
    document: ImportedDocument,
    ingestor: SourceIngestor,
    report: SyncReport,
    *,
    access: str,
) -> None:
    spec = {
        "id": document.source_id,
        "kind": "discord",
        "title": document.title,
        "corpus": "LORE",
        "access": access,
        "disclosure_tier": "FREE",
    }
    async with SessionLocal() as session:
        chunks = await ingestor.store_document(session, document, spec, replace_source=True)
        # Channel descriptions and official news are already public Discord material.
        # Do not let generic keyword heuristics accidentally put them behind an exchange.
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.source_id == document.source_id)
            .values(disclosure_tier="FREE", disclosure_modes=["FREE"])
        )
        await session.commit()
    report.documents += 1
    report.chunks += chunks
    if document.source_id.startswith("discord_location:"):
        report.location_messages += int(document.metadata.get("selected_messages") or 0)



def _is_world_news_continuation(content: str) -> bool:
    """Keep split report messages, stop before a trusted author's ordinary chat."""
    clean = content.strip()
    if len(clean) < 180:
        return False
    if OFFTOP_RE.search(clean) and len(clean) < 700:
        return False
    return bool(
        SEPARATOR_RE.search(clean)
        or len(clean) >= 500
        or ("\n\n" in clean and len(re.findall(r"(?m)^.{4,100}$", clean)) >= 3)
    )

def _is_structured_world_news(title: str, content: str) -> bool:
    if len(content) < 400:
        return False
    folded = content.casefold()
    markers = 0
    markers += 2 if "редакция" in folded and ("новост" in folded or "отчёт" in folded or "отчет" in folded) else 0
    markers += 1 if SEPARATOR_RE.search(content) else 0
    markers += 1 if WORLD_DATE_RE.search(content) or SEASON_YEAR_RE.search(f"{title}\n{content}") else 0
    markers += 1 if len(re.findall(r"(?m)^.{3,90}$", content)) >= 4 else 0
    return markers >= 3


def _extract_world_date_label(title: str, content: str) -> str:
    match = WORLD_DATE_RE.search(content)
    if match:
        return match.group(1).replace("-", ".").replace("/", ".") + " года"
    match = SEASON_YEAR_RE.search(f"{title}\n{content}")
    if match:
        return f"{match.group(1).capitalize()} {match.group(2)} года"
    return ""


def _split_news_sections(content: str) -> list[tuple[str, str]]:
    blocks = [item.strip() for item in re.split(r"\n?\s*[━═─]{5,}.*?\n", content) if item.strip()]
    result: list[tuple[str, str]] = []
    for index, block in enumerate(blocks):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        title = lines[0][:300] if len(lines[0]) <= 300 else f"Новостной блок {index + 1}"
        body = "\n".join(lines[1:] if len(lines) > 1 else lines)
        if body:
            result.append((title, body))
    return result or [("Новостной отчёт", content)]


def _clean_message(content: str) -> str:
    lines: list[str] = []
    for line in (content or "").replace("\r", "").splitlines():
        clean = line.strip()
        if not clean:
            lines.append("")
            continue
        if re.fullmatch(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", clean, re.I):
            continue
        if clean.casefold() in {"изображение", "."}:
            continue
        lines.append(clean)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _scene_path(channel: discord.TextChannel | discord.Thread) -> str:
    parent = channel.parent if isinstance(channel, discord.Thread) else None
    category = getattr(channel, "category", None) or (
        getattr(parent, "category", None) if parent is not None else None
    )
    parts = [
        _clean_name(category.name) if category else "",
        _clean_name(parent.name) if parent else "",
        _clean_name(channel.name),
    ]
    return " / ".join(item for item in parts if item)


def _clean_name(value: str) -> str:
    clean = re.sub(r"^[^A-Za-zА-ЯЁа-яё0-9]+", "", value or "")
    clean = re.sub(r"\s+", " ", clean).strip(" -—–│┃・")
    return clean or (value or "Локация")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "_", value.casefold()).strip("_")[:128]
