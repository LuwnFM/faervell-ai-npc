from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml  # type: ignore[import-untyped]
from bs4 import BeautifulSoup, Tag
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.models import KnowledgeChunk, SourceRevision
from faervell_npc.schemas import Corpus, DisclosureTier
from faervell_npc.services.embeddings import get_embedder


@dataclass(slots=True)
class ImportedDocument:
    source_id: str
    title: str
    url: str | None
    revision: str | None
    text: str
    sections: list[tuple[str, str]]
    metadata: dict[str, Any]


class SourceIngestor:
    MECHANICS_HINTS = {
        "механик",
        "правил",
        "формул",
        "характерист",
        "эффект",
        "урон",
        "рецепт",
        "ингредиент",
        "цена",
        "стоимость",
        "валют",
        "единиц",
        "требован",
        "шанс",
        "время действия",
        "таблиц",
    }
    VALUABLE_HINTS = {
        "точное мест",
        "скрыт",
        "тайн",
        "редк",
        "сокровищ",
        "секретн",
        "уязвим",
        "подземн",
        "проход",
    }
    RARE_HINTS = {"запретн", "особо опасн", "истинное имя", "древняя тайна"}

    def __init__(self) -> None:
        self.embedder = get_embedder()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={"User-Agent": "Faervell-AI-NPC/0.1 knowledge importer"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def ingest_manifest(
        self,
        session: AsyncSession,
        manifest_path: Path,
        *,
        replace_source: bool = True,
    ) -> dict[str, Any]:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        specs = manifest.get("sources") or []
        report: dict[str, Any] = {"sources": [], "documents": 0, "chunks": 0, "errors": []}

        for spec in specs:
            try:
                documents = await self.load_spec(spec, manifest_path.parent.parent)
                source_chunks = 0
                for document in documents:
                    source_chunks += await self.store_document(
                        session,
                        document,
                        spec,
                        replace_source=replace_source,
                    )
                await session.commit()
                report["documents"] += len(documents)
                report["chunks"] += source_chunks
                report["sources"].append(
                    {"id": spec.get("id"), "documents": len(documents), "chunks": source_chunks}
                )
            except Exception as exc:  # importer should continue with remaining sources
                await session.rollback()
                report["errors"].append({"id": spec.get("id"), "error": str(exc)})
        return report

    async def load_spec(self, spec: dict[str, Any], repository_root: Path) -> list[ImportedDocument]:
        kind = spec.get("kind")
        location = str(spec.get("location") or "")
        if kind == "local":
            path = (repository_root / location).resolve()
            text = path.read_text(encoding="utf-8")
            return [
                ImportedDocument(
                    source_id=str(spec["id"]),
                    title=str(spec.get("title") or path.name),
                    url=None,
                    revision=None,
                    text=text,
                    sections=self._sections_from_plaintext(text),
                    metadata={"path": str(path)},
                )
            ]
        if kind == "url" and spec.get("crawl") and "fandom.com" in location:
            return await self._load_fandom(spec)
        if kind == "url":
            return [await self._load_url(spec)]
        raise ValueError(f"Unsupported source kind: {kind}")

    async def _load_url(self, spec: dict[str, Any]) -> ImportedDocument:
        url = str(spec["location"])
        response = await self.http.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        for element in soup(["script", "style", "noscript", "nav", "footer"]):
            element.decompose()
        root = soup.select_one(".mw-parser-output") or soup.select_one("main") or soup.body or soup
        sections = self._sections_from_html(root)
        text = "\n\n".join(body for _, body in sections)
        return ImportedDocument(
            source_id=str(spec["id"]),
            title=str(spec.get("title") or (soup.title.string if soup.title else url)),
            url=url,
            revision=response.headers.get("etag") or response.headers.get("last-modified"),
            text=text,
            sections=sections,
            metadata={"content_type": response.headers.get("content-type")},
        )

    async def _load_fandom(self, spec: dict[str, Any]) -> list[ImportedDocument]:
        parsed = urlparse(str(spec["location"]))
        api_url = f"{parsed.scheme}://{parsed.netloc}/ru/api.php"
        max_pages = int(spec.get("max_pages") or 1000)
        titles: list[str] = []
        continuation: str | None = None
        while len(titles) < max_pages:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "list": "allpages",
                "aplimit": "max",
                "apnamespace": 0,
            }
            if continuation:
                params["apcontinue"] = continuation
            response = await self.http.get(api_url, params=params)
            response.raise_for_status()
            payload = response.json()
            titles.extend(page["title"] for page in payload["query"]["allpages"])
            continuation = (payload.get("continue") or {}).get("apcontinue")
            if not continuation:
                break
        titles = titles[:max_pages]

        semaphore = asyncio.Semaphore(6)

        async def fetch(title: str) -> ImportedDocument | None:
            async with semaphore:
                params: dict[str, str | int] = {
                    "action": "parse",
                    "format": "json",
                    "page": title,
                    "prop": "text|revid|displaytitle",
                    "redirects": 1,
                }
                try:
                    response = await self.http.get(api_url, params=params)
                    response.raise_for_status()
                    parse = response.json()["parse"]
                    html = parse["text"]["*"]
                    soup = BeautifulSoup(html, "lxml")
                    root = soup.select_one(".mw-parser-output") or soup
                    for element in root.select(".mw-editsection, table.navbox, .toc, script, style"):
                        element.decompose()
                    sections = self._sections_from_html(root)
                    text = "\n\n".join(body for _, body in sections)
                    return ImportedDocument(
                        source_id=f"{spec['id']}:{parse.get('pageid', title)}",
                        title=BeautifulSoup(parse.get("displaytitle") or title, "lxml").get_text(" ", strip=True),
                        url=f"{parsed.scheme}://{parsed.netloc}/ru/wiki/{title.replace(' ', '_')}",
                        revision=str(parse.get("revid") or ""),
                        text=text,
                        sections=sections,
                        metadata={"wiki_title": title, "root_source_id": spec["id"]},
                    )
                except Exception:
                    return None

        fetched = await asyncio.gather(*(fetch(title) for title in titles))
        return [doc for doc in fetched if doc and doc.text.strip()]

    async def store_document(
        self,
        session: AsyncSession,
        document: ImportedDocument,
        spec: dict[str, Any],
        *,
        replace_source: bool,
    ) -> int:
        content_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        revision = (
            await session.execute(
                select(SourceRevision).where(
                    SourceRevision.source_id == document.source_id,
                    SourceRevision.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            revision = SourceRevision(
                source_id=document.source_id,
                title=document.title,
                url=document.url,
                content_hash=content_hash,
                revision=document.revision,
                metadata_json=document.metadata,
            )
            session.add(revision)
            await session.flush()
        else:
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.source_revision_id == revision.id)
            )
        if replace_source:
            await session.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.source_id == document.source_id,
                    KnowledgeChunk.source_revision_id != revision.id,
                )
            )

        count = 0
        for section, body in document.sections:
            for chunk_text in self._chunk_text(body):
                corpus = self._infer_corpus(spec, section, chunk_text)
                tier, inferred = self._infer_tier(spec, corpus, section, chunk_text)
                modes = self._modes_for_tier(tier)
                session.add(
                    KnowledgeChunk(
                        source_revision_id=revision.id,
                        source_id=document.source_id,
                        title=document.title,
                        section=section,
                        content=chunk_text,
                        corpus=corpus.value,
                        access=str(spec.get("access") or "PUBLIC_CANON"),
                        disclosure_tier=tier.value,
                        disclosure_modes=modes,
                        exact_values=corpus == Corpus.MECHANICS and bool(re.search(r"\d", chunk_text)),
                        embedding=self.embedder.embed(f"{document.title}\n{section}\n{chunk_text}"),
                        metadata_json={
                            "tier_auto_inferred": inferred,
                            "root_source_id": spec.get("id"),
                        },
                    )
                )
                count += 1
        return count

    def _infer_corpus(self, spec: dict[str, Any], section: str, content: str) -> Corpus:
        configured = str(spec.get("corpus") or "LORE")
        if configured == Corpus.INTERNAL.value:
            return Corpus.INTERNAL
        if configured == Corpus.MECHANICS.value:
            return Corpus.MECHANICS
        text = f"{section} {content}".casefold()
        if any(hint in text for hint in self.MECHANICS_HINTS):
            return Corpus.MECHANICS
        return Corpus.LORE

    def _infer_tier(
        self,
        spec: dict[str, Any],
        corpus: Corpus,
        section: str,
        content: str,
    ) -> tuple[DisclosureTier, bool]:
        if corpus == Corpus.MECHANICS:
            return DisclosureTier.FREE, False
        configured = DisclosureTier(str(spec.get("disclosure_tier") or "FREE"))
        text = f"{section} {content}".casefold()
        if any(hint in text for hint in self.RARE_HINTS):
            return DisclosureTier.RARE, True
        if any(hint in text for hint in self.VALUABLE_HINTS):
            return DisclosureTier.VALUABLE, True
        return configured, False

    @staticmethod
    def _modes_for_tier(tier: DisclosureTier) -> list[str]:
        return {
            DisclosureTier.FREE: ["FREE"],
            DisclosureTier.USEFUL: ["FREE", "COINS", "ITEM", "SERVICE"],
            DisclosureTier.VALUABLE: ["COINS", "ITEM", "SERVICE", "QUEST", "TRUST"],
            DisclosureTier.RARE: ["QUEST", "TRUST", "GM_APPROVAL"],
            DisclosureTier.RESTRICTED: ["GM_APPROVAL"],
        }[tier]

    @staticmethod
    def _sections_from_plaintext(text: str) -> list[tuple[str, str]]:
        heading = "Документ"
        buffer: list[str] = []
        sections: list[tuple[str, str]] = []
        for line in text.splitlines():
            if re.match(r"^#{1,6}\s+", line):
                if buffer:
                    sections.append((heading, "\n".join(buffer).strip()))
                    buffer = []
                heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            else:
                buffer.append(line)
        if buffer:
            sections.append((heading, "\n".join(buffer).strip()))
        return [(h, b) for h, b in sections if b]

    @staticmethod
    def _sections_from_html(root: Tag | BeautifulSoup) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        heading = "Введение"
        buffer: list[str] = []
        for node in root.descendants:
            if not isinstance(node, Tag):
                continue
            if node.name in {"h1", "h2", "h3", "h4"}:
                if buffer:
                    sections.append((heading, "\n".join(buffer).strip()))
                    buffer = []
                heading = node.get_text(" ", strip=True)
            elif node.name in {"p", "li", "tr"}:
                text = node.get_text(" ", strip=True)
                if text and (not node.parent or node.parent.name not in {"li", "tr"}):
                    buffer.append(text)
        if buffer:
            sections.append((heading, "\n".join(buffer).strip()))
        if not sections:
            text = root.get_text("\n", strip=True)
            sections = [("Документ", text)]
        return [(h, b) for h, b in sections if len(b) >= 20]

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 1600, overlap: int = 180) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            if len(current) + len(paragraph) + 2 <= max_chars:
                current = f"{current}\n\n{paragraph}".strip()
                continue
            if current:
                chunks.append(current)
            if len(paragraph) <= max_chars:
                current = paragraph
            else:
                start = 0
                while start < len(paragraph):
                    chunks.append(paragraph[start : start + max_chars])
                    start += max_chars - overlap
                current = ""
        if current:
            chunks.append(current)
        return chunks
