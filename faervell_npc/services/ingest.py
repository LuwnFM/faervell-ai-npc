from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
import yaml  # type: ignore[import-untyped]
from bs4 import BeautifulSoup, Tag
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import KnowledgeChunk, KnowledgeImportRun, SourceRevision
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
        "механик", "правил", "формул", "характерист", "эффект", "урон", "рецепт",
        "ингредиент", "цена", "стоимость", "валют", "единиц", "требован", "шанс",
        "время действия", "таблиц",
    }
    VALUABLE_HINTS = {"точное мест", "скрыт", "тайн", "редк", "сокровищ", "секретн", "уязвим", "подземн", "проход"}
    RARE_HINTS = {"запретн", "особо опасн", "истинное имя", "древняя тайна"}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedder()
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"User-Agent": "Faervell-AI-NPC/0.7 knowledge-importer (+https://github.com/LuwnFM/faervell-ai-npc)"},
            follow_redirects=True,
        )
        self.page_errors: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self.http.aclose()

    async def ingest_manifest(
        self,
        session: AsyncSession,
        manifest_path: Path,
        *,
        replace_source: bool = True,
        source_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        specs = manifest.get("sources") or []
        if source_ids is not None:
            specs = [spec for spec in specs if str(spec.get("id") or "") in source_ids]
        report: dict[str, Any] = {"sources": [], "documents": 0, "chunks": 0, "errors": []}

        for spec in specs:
            self.page_errors = []
            run = KnowledgeImportRun(source_id=str(spec.get("id") or "unknown"), status="RUNNING")
            session.add(run)
            await session.flush()
            try:
                documents = await self.load_spec(spec, manifest_path.parent.parent)
                if spec.get("crawl") and "fandom.com" in str(spec.get("location") or ""):
                    minimum = int(spec.get("min_documents") or self.settings.knowledge_min_wiki_documents)
                    if len(documents) < minimum:
                        raise RuntimeError(
                            f"Fandom returned only {len(documents)} documents; minimum is {minimum}. "
                            "Old index was preserved."
                        )
                source_chunks = 0
                for document in documents:
                    source_chunks += await self.store_document(
                        session, document, spec, replace_source=replace_source
                    )
                run.status = "SUCCESS" if not self.page_errors else "PARTIAL"
                run.documents = len(documents)
                run.chunks = source_chunks
                run.errors = self.page_errors[:500]
                run.finished_at = datetime.now(UTC)
                await session.commit()
                report["documents"] += len(documents)
                report["chunks"] += source_chunks
                report["errors"].extend(self.page_errors)
                report["sources"].append(
                    {
                        "id": spec.get("id"),
                        "documents": len(documents),
                        "chunks": source_chunks,
                        "page_errors": len(self.page_errors),
                    }
                )
            except Exception as exc:
                await session.rollback()
                failed = KnowledgeImportRun(
                    source_id=str(spec.get("id") or "unknown"),
                    status="FAILED",
                    documents=0,
                    chunks=0,
                    errors=[{"error": f"{type(exc).__name__}: {exc}"}, *self.page_errors[:499]],
                    finished_at=datetime.now(UTC),
                )
                session.add(failed)
                await session.commit()
                report["errors"].append({"id": spec.get("id"), "error": str(exc)})
        return report

    async def load_spec(self, spec: dict[str, Any], repository_root: Path) -> list[ImportedDocument]:
        kind = spec.get("kind")
        location = str(spec.get("location") or "")
        if kind == "local":
            path = (repository_root / location).resolve()
            if not path.exists():
                raise FileNotFoundError(f"Local source does not exist: {path}")
            text = path.read_text(encoding="utf-8")
            return [
                ImportedDocument(
                    source_id=str(spec["id"]),
                    title=str(spec.get("title") or path.name),
                    url=None,
                    revision=str(path.stat().st_mtime_ns),
                    text=text,
                    sections=self._sections_from_plaintext(text),
                    metadata={"path": str(path)},
                )
            ]
        if kind == "url" and "fandom.com" in location:
            if spec.get("crawl"):
                return await self._load_fandom(spec)
            return [await self._load_fandom_page(spec)]
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

    async def probe_fandom(
        self,
        location: str,
        *,
        sample_title: str = "Королевство Ивелтин",
    ) -> dict[str, Any]:
        """Run a small, non-destructive MediaWiki API diagnostic."""
        api_url, _ = self._fandom_api_url(location)
        site_response = await self.http.get(
            api_url,
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "meta": "siteinfo",
                "siprop": "general",
            },
        )
        site_response.raise_for_status()
        site_payload = site_response.json()
        if site_payload.get("error"):
            raise RuntimeError(str(site_payload["error"]))

        pages_response = await self.http.get(
            api_url,
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "list": "allpages",
                "apnamespace": 0,
                "apfilterredir": "nonredirects",
                "aplimit": 5,
            },
        )
        pages_response.raise_for_status()
        pages_payload = pages_response.json()
        if pages_payload.get("error"):
            raise RuntimeError(str(pages_payload["error"]))
        sample_spec = {
            "id": "fandom_probe",
            "location": location,
            "title": sample_title,
        }
        sample = await self._fetch_fandom_parse(sample_spec, sample_title)
        general = (site_payload.get("query") or {}).get("general") or {}
        continuation = pages_payload.get("continue") or {}
        return {
            "api_url": api_url,
            "site_name": general.get("sitename"),
            "generator": general.get("generator"),
            "article_path": general.get("articlepath"),
            "sample_pages": [
                str(item.get("title"))
                for item in ((pages_payload.get("query") or {}).get("allpages") or [])
                if item.get("title")
            ],
            "continuation": continuation,
            "sample_title": sample.title,
            "sample_revision": sample.revision,
            "sample_text_length": len(sample.text),
            "sample_preview": sample.text[:600],
        }

    async def _load_fandom_page(self, spec: dict[str, Any]) -> ImportedDocument:
        title = str(spec.get("wiki_title") or self._title_from_fandom_url(str(spec["location"])))
        return await self._fetch_fandom_parse(spec, title)

    async def _load_fandom(self, spec: dict[str, Any]) -> list[ImportedDocument]:
        """Load namespace-0 pages through batched standard MediaWiki API requests.

        The previous implementation issued one expensive ``action=parse`` request per article.
        On a 700-page wiki that was slow and fragile. This implementation enumerates and fetches
        revision content in batches through ``generator=allpages`` and follows the complete
        MediaWiki continuation object until the namespace is exhausted. A small set of priority
        pages is then parsed separately so transcluded infobox/calendar values are expanded.
        """
        api_url, path_prefix = self._fandom_api_url(str(spec["location"]))
        parsed = urlparse(str(spec["location"]))
        max_pages = int(spec.get("max_pages") or 1200)
        batch_size = min(50, int(spec.get("batch_size") or self.settings.fandom_batch_size))
        documents: dict[str, ImportedDocument] = {}
        continuation: dict[str, Any] = {}
        fetched_pages = 0

        while fetched_pages < max_pages:
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "generator": "allpages",
                "gapnamespace": 0,
                "gapfilterredir": "nonredirects",
                "gaplimit": min(batch_size, max_pages - fetched_pages),
                "prop": "revisions|info",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "inprop": "url",
                "maxlag": 5,
                **continuation,
            }
            payload = await self._mediawiki_json(api_url, params)
            query = payload.get("query") or {}
            pages = query.get("pages") or []
            if isinstance(pages, dict):
                pages = list(pages.values())
            if not pages:
                break
            for page in pages:
                if not isinstance(page, dict) or page.get("missing"):
                    continue
                try:
                    document = self._document_from_revision_page(
                        spec=spec,
                        page=page,
                        parsed=parsed,
                        path_prefix=path_prefix,
                        api_url=api_url,
                    )
                    if document is not None:
                        documents[document.source_id] = document
                except Exception as exc:
                    self.page_errors.append(
                        {
                            "id": spec.get("id"),
                            "title": page.get("title"),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
            fetched_pages += len(pages)
            continuation = dict(payload.get("continue") or {})
            print(
                "Fandom import progress: "
                f"source={spec.get('id')} fetched={fetched_pages} readable={len(documents)} "
                f"continued={bool(continuation)}"
            )
            if not continuation:
                break

        priority_titles = list(
            dict.fromkeys(
                [
                    *[str(item) for item in (spec.get("priority_titles") or []) if str(item).strip()],
                    *[str(item) for item in (spec.get("required_titles") or []) if str(item).strip()],
                ]
            )
        )
        for title in priority_titles:
            try:
                parsed_document = await self._fetch_fandom_parse(spec, title)
                documents[parsed_document.source_id] = parsed_document
            except Exception as exc:
                self.page_errors.append(
                    {
                        "id": spec.get("id"),
                        "title": title,
                        "error": f"priority parse failed: {type(exc).__name__}: {exc}",
                    }
                )

        required = {self._normalise_title(item) for item in (spec.get("required_titles") or [])}
        available = {
            self._normalise_title(str(document.metadata.get("wiki_title") or document.title))
            for document in documents.values()
        }
        missing_required = sorted(item for item in required if item and item not in available)
        if missing_required:
            raise RuntimeError(
                "Fandom import missed required pages: " + ", ".join(missing_required)
            )
        if not documents:
            raise RuntimeError(f"Fandom API returned no readable documents from {api_url}")
        return sorted(documents.values(), key=lambda item: item.title.casefold())[:max_pages]

    async def _mediawiki_json(self, api_url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await self.http.get(api_url, params=params)
                if response.status_code in {429, 502, 503, 504}:
                    if attempt < 3:
                        await asyncio.sleep(1.25 * (attempt + 1))
                        continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    error = payload["error"]
                    code = str(error.get("code") or "") if isinstance(error, dict) else ""
                    if code == "maxlag" and attempt < 3:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise RuntimeError(str(error))
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
        assert last_error is not None
        raise last_error

    def _document_from_revision_page(
        self,
        *,
        spec: dict[str, Any],
        page: dict[str, Any],
        parsed: Any,
        path_prefix: str,
        api_url: str,
    ) -> ImportedDocument | None:
        title = str(page.get("title") or "").strip()
        if not title:
            return None
        revisions = page.get("revisions") or []
        if not revisions:
            return None
        revision = revisions[0] if isinstance(revisions[0], dict) else {}
        slots = revision.get("slots") or {}
        main = slots.get("main") if isinstance(slots, dict) else None
        content = ""
        if isinstance(main, dict):
            content = str(main.get("content") or main.get("*") or "")
        if not content:
            content = str(revision.get("content") or revision.get("*") or "")
        sections = self._sections_from_wikitext(content)
        text = "\n\n".join(body for _, body in sections).strip()
        if len(text) < 40:
            return None
        page_id = str(page.get("pageid") or hashlib.sha1(title.encode()).hexdigest()[:16])
        canonical_url = str(page.get("fullurl") or "") or (
            f"{parsed.scheme}://{parsed.netloc}{path_prefix}/wiki/"
            f"{quote(title.replace(' ', '_'))}"
        )
        return ImportedDocument(
            source_id=f"{spec['id']}:{page_id}",
            title=title,
            url=canonical_url,
            revision=str(revision.get("revid") or revision.get("parentid") or ""),
            text=text,
            sections=sections,
            metadata={
                "wiki_title": title,
                "page_id": page_id,
                "root_source_id": spec["id"],
                "api_url": api_url,
                "revision_timestamp": revision.get("timestamp"),
                "ingest_mode": "mediawiki_revision_batch",
            },
        )

    async def _fetch_fandom_parse(
        self,
        spec: dict[str, Any],
        title: str,
    ) -> ImportedDocument:
        api_url, path_prefix = self._fandom_api_url(str(spec["location"]))
        parsed = urlparse(str(spec["location"]))
        payload = await self._mediawiki_json(
            api_url,
            {
                "action": "parse",
                "format": "json",
                "formatversion": 2,
                "page": title,
                "prop": "text|revid|displaytitle|properties|categories",
                "redirects": 1,
                "disabletoc": 1,
                "maxlag": 5,
            },
        )
        parse = payload.get("parse") or {}
        html = str(parse.get("text") or "")
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one(".mw-parser-output") or soup
        for element in root.select(
            ".mw-editsection, table.navbox, .toc, script, style, "
            ".portable-infobox .pi-navigation"
        ):
            element.decompose()
        sections = self._sections_from_html(root)
        text = "\n\n".join(body for _, body in sections).strip()
        if len(text) < 40:
            raise RuntimeError("parsed page contains too little readable text")
        resolved_title = BeautifulSoup(
            str(parse.get("displaytitle") or parse.get("title") or title), "lxml"
        ).get_text(" ", strip=True)
        page_id = str(parse.get("pageid") or hashlib.sha1(title.encode()).hexdigest()[:16])
        canonical_url = (
            f"{parsed.scheme}://{parsed.netloc}{path_prefix}/wiki/"
            f"{quote(str(parse.get('title') or title).replace(' ', '_'))}"
        )
        return ImportedDocument(
            source_id=f"{spec['id']}:{page_id}" if spec.get("crawl") else str(spec["id"]),
            title=resolved_title or title,
            url=canonical_url,
            revision=str(parse.get("revid") or ""),
            text=text,
            sections=sections,
            metadata={
                "wiki_title": str(parse.get("title") or title),
                "page_id": page_id,
                "root_source_id": spec["id"],
                "api_url": api_url,
                "categories": [
                    str(item.get("*") or item.get("category") or "")
                    for item in (parse.get("categories") or [])
                    if isinstance(item, dict)
                ],
                "ingest_mode": "mediawiki_parse_priority",
            },
        )

    @staticmethod
    def _title_from_fandom_url(location: str) -> str:
        parsed = urlparse(location)
        marker = "/wiki/"
        if marker not in parsed.path:
            raise ValueError(f"Fandom article URL has no /wiki/ title: {location}")
        raw = parsed.path.split(marker, 1)[1]
        return unquote(raw).replace("_", " ").strip()

    @staticmethod
    def _normalise_title(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()

    @staticmethod
    def _fandom_api_url(location: str) -> tuple[str, str]:
        parsed = urlparse(location)
        path_prefix = parsed.path.split("/wiki/", 1)[0].rstrip("/")
        if path_prefix == "/":
            path_prefix = ""
        return f"{parsed.scheme}://{parsed.netloc}{path_prefix}/api.php", path_prefix

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
            revision.title = document.title
            revision.url = document.url
            revision.revision = document.revision
            revision.fetched_at = datetime.now(UTC)
            revision.metadata_json = document.metadata
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
                        disclosure_modes=self._modes_for_tier(tier),
                        exact_values=corpus == Corpus.MECHANICS and bool(re.search(r"\d", chunk_text)),
                        embedding=self.embedder.embed(f"{document.title}\n{section}\n{chunk_text}"),
                        metadata_json={
                            "tier_auto_inferred": inferred,
                            "root_source_id": spec.get("id"),
                            **document.metadata,
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
        return Corpus.MECHANICS if any(hint in text for hint in self.MECHANICS_HINTS) else Corpus.LORE

    def _infer_tier(self, spec: dict[str, Any], corpus: Corpus, section: str, content: str) -> tuple[DisclosureTier, bool]:
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
    def _sections_from_wikitext(text: str) -> list[tuple[str, str]]:
        """Convert revision wikitext into searchable prose without a full MediaWiki parser."""
        if not text.strip():
            return []
        cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
        cleaned = re.sub(r"<ref\b[^>/]*>.*?</ref\s*>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<ref\b[^>]*/\s*>", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"\[\[(?:Файл|File|Изображение|Image|Категория|Category):[^\]]+\]\]", " ", cleaned, flags=re.I)

        infobox_lines: list[str] = []
        for raw_line in cleaned.splitlines():
            line = raw_line.strip()
            if not line.startswith("|") or "=" not in line:
                continue
            key, value = line[1:].split("=", 1)
            key = re.sub(r"[_\s]+", " ", key).strip()
            value = value.strip()
            if not key or not value or len(key) > 80:
                continue
            value = re.sub(r"\{\{[^{}]*\}\}", " ", value)
            value = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", value)
            value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
            value = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
            value = re.sub(r"\s+", " ", value).strip(" |")
            if value and value not in {"-", "—", "нет"}:
                infobox_lines.append(f"{key}: {value}")

        cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
        cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
        cleaned = re.sub(r"\[(?:https?://\S+)\s+([^\]]+)\]", r"\1", cleaned)
        cleaned = re.sub(r"https?://\S+", " ", cleaned)
        cleaned = re.sub(r"\{\{[^{}]*\}\}", " ", cleaned)
        cleaned = re.sub(r"\{\{.*?\}\}", " ", cleaned, flags=re.S)
        cleaned = cleaned.replace("{|", "\n").replace("|}", "\n")
        cleaned = re.sub(r"^\s*[|!]\s*", "", cleaned, flags=re.M)
        cleaned = re.sub(r"\|\|+|!!+", " | ", cleaned)
        cleaned = re.sub(r"'{2,5}", "", cleaned)
        cleaned = BeautifulSoup(cleaned, "lxml").get_text("\n", strip=True)

        sections: list[tuple[str, str]] = []
        if infobox_lines:
            unique = list(dict.fromkeys(infobox_lines))
            sections.append(("Карточка статьи", "\n".join(unique)))

        heading = "Введение"
        buffer: list[str] = []
        for raw_line in cleaned.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            heading_match = re.match(r"^={2,6}\s*(.*?)\s*={2,6}$", line)
            if heading_match:
                if buffer:
                    sections.append((heading, "\n".join(buffer).strip()))
                    buffer = []
                heading = heading_match.group(1).strip() or "Раздел"
                continue
            if line.startswith(("__", "{{", "}}")):
                continue
            line = re.sub(r"^[*#:;]+\s*", "", line)
            if len(line) >= 2:
                buffer.append(line)
        if buffer:
            sections.append((heading, "\n".join(buffer).strip()))
        return [(title, body) for title, body in sections if len(body) >= 20]

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
        """Extract article prose, tables and Fandom portable-infobox facts.

        Fandom stores many important fields (rulers, dates, seasons and geography) in
        `.portable-infobox` divs rather than paragraphs. The old parser silently skipped
        those nodes, which made canonical facts unavailable to RAG even when the page was
        downloaded successfully.
        """
        sections: list[tuple[str, str]] = []

        infobox_lines: list[str] = []
        for item in root.select(".portable-infobox .pi-data, .portable-infobox [data-source]"):
            if not isinstance(item, Tag):
                continue
            label_node = item.select_one(".pi-data-label")
            value_node = item.select_one(".pi-data-value")
            label = label_node.get_text(" ", strip=True) if label_node else ""
            value = value_node.get_text(" ", strip=True) if value_node else item.get_text(" ", strip=True)
            if value and value != label:
                line = f"{label}: {value}" if label else value
                if line not in infobox_lines:
                    infobox_lines.append(line)
        if infobox_lines:
            sections.append(("Карточка статьи", "\n".join(infobox_lines)))

        heading = "Введение"
        buffer: list[str] = []
        seen_text: set[str] = set()
        for node in root.descendants:
            if not isinstance(node, Tag):
                continue
            if node.find_parent(class_="portable-infobox") is not None:
                continue
            if node.name in {"h1", "h2", "h3", "h4"}:
                if buffer:
                    sections.append((heading, "\n".join(buffer).strip()))
                    buffer = []
                heading = node.get_text(" ", strip=True)
            elif node.name in {"p", "li", "tr", "dd", "dt", "figcaption"}:
                text = node.get_text(" ", strip=True)
                nested_same = node.find_parent(node.name) is not None
                if text and not nested_same and text not in seen_text:
                    seen_text.add(text)
                    buffer.append(text)
        if buffer:
            sections.append((heading, "\n".join(buffer).strip()))
        if not sections:
            sections = [("Документ", root.get_text("\n", strip=True))]
        return [(h, b) for h, b in sections if len(b) >= 20]

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 1600, overlap: int = 180) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if not paragraphs:
            paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
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
