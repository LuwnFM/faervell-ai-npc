from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Аудит и обогащение шаблонов behavior-pack/template-library под личность
# Странника. Работает локально по репозиторию, ничего не удаляет и не
# добавляет новых файлов (деплой проверяет templates == 512), правит только
# содержимое существующих шаблонов и только с бэкапом.
#
# Режимы:
#   audit  (по умолчанию) — отчёт по каждому шаблону, файлов не трогает
#   extend --apply        — дописывает персонажные фрагменты в короткие
#                           arrival/identity/greeting-шаблоны

BACKUP_DIR = ".template-backup-v1.0.2"
REPORT_DEFAULT = "data/exports/template-audit-v1.0.2.json"

_FORBIDDEN = {
    "portal": re.compile(r"(?iu)\b(?:портал|телепорт)\w*"),
    "internal_economy": re.compile(
        r"(?iu)\b(?:ОТН|экономическ(?:ая|ой|ий|ого)\s+(?:база|базы|индекс|индекса)|"
        r"индекс\s+экономики)\b"
    ),
    "ooc_ai": re.compile(r"(?iu)\b(?:нейросет|языков(?:ая|ой)\s+модел|промпт|ИИ-модел)\w*"),
}

# Неизменные приметы Странника из чарника. Шаблон «про личность» должен
# упоминать хотя бы один из них.
_PERSONA_MARKERS = {
    "third_eye": re.compile(r"(?iu)трет(?:ий|ьего)\s+глаз|око\s+развилки"),
    "glowing_lines": re.compile(r"(?iu)светящ\w+\s+лини"),
    "bracelet": re.compile(r"(?iu)браслет\w*\s+из\s+тёмных\s+пластин|ключ\s+тысячи\s+порогов"),
    "pale": re.compile(r"(?iu)пепельн\w+\s+кож|бледн\w+\s+кож"),
    "calm_gaze": re.compile(r"(?iu)спокойн\w+.{0,20}взгляд|утомлённ\w+\s+взгляд"),
}

_IDENTITY_FILE_RE = re.compile(r"(?i)(?:arrival|identity|greeting|introduc|появлен|прибыт)")
_IDENTITY_ID_RE = re.compile(r"(?i)(?:arrival|identity|greeting|появлен|прибыт|кто\W*ты)")

_MASK_KEYS = ("profession_mask_id", "mask", "profession", "mask_id")


@dataclass(slots=True)
class Finding:
    file: str
    template_id: str
    kind: str
    detail: str


@dataclass(slots=True)
class Stats:
    files: int = 0
    templates: int = 0
    identity_like: int = 0
    extended: int = 0
    findings: list[Finding] = field(default_factory=list)


def _load_fragments(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fragments = dict(data.get("fragments") or {})
    if "default" not in fragments:
        raise SystemExit(f"В {path} нет обязательного фрагмента 'default'.")
    return {str(key): str(value).strip() for key, value in fragments.items()}


def _iter_template_docs(path: Path) -> list[tuple[object, dict]]:
    """Возвращает [(документ-контейнер, шаблон-словарь), ...] для yaml/json."""
    text = path.read_text(encoding="utf-8", errors="replace")
    docs: list[object]
    if path.suffix.casefold() == ".json":
        docs = [json.loads(text)]
    else:
        docs = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    result: list[tuple[object, dict]] = []
    for doc in docs:
        if isinstance(doc, dict):
            if isinstance(doc.get("templates"), list):
                for item in doc["templates"]:
                    if isinstance(item, dict):
                        result.append((doc, item))
            else:
                result.append((doc, doc))
        elif isinstance(doc, list):
            for item in doc:
                if isinstance(item, dict):
                    result.append((doc, item))
    return result


def _template_text(item: dict) -> str:
    for key in ("text", "template", "content", "body"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def _set_template_text(item: dict, value: str) -> None:
    for key in ("text", "template", "content", "body"):
        if isinstance(item.get(key), str):
            item[key] = value
            return
    item["text"] = value


def _template_id(item: dict, fallback: str) -> str:
    for key in ("template_id", "id", "name", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def _mask_of(item: dict) -> str:
    for key in _MASK_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    return "default"


def _is_identity_like(path: Path, item: dict) -> bool:
    if _IDENTITY_FILE_RE.search(path.name):
        return True
    identifier = _template_id(item, "")
    if identifier and _IDENTITY_ID_RE.search(identifier):
        return True
    archetype = str(item.get("quest_archetype_title") or item.get("archetype") or "")
    return bool(archetype and _IDENTITY_ID_RE.search(archetype))


def _audit_text(path: Path, item: dict, stats: Stats) -> None:
    text = _template_text(item)
    identifier = _template_id(item, path.stem)
    for name, pattern in _FORBIDDEN.items():
        match = pattern.search(text)
        if match:
            stats.findings.append(
                Finding(str(path), identifier, f"forbidden:{name}", match.group(0))
            )
    if _is_identity_like(path, item):
        stats.identity_like += 1
        if not any(marker.search(text) for marker in _PERSONA_MARKERS.values()):
            stats.findings.append(
                Finding(
                    str(path),
                    identifier,
                    "identity_without_persona_markers",
                    "нет ни одной неизменной приметы Странника",
                )
            )
        if len(text) < 220:
            stats.findings.append(
                Finding(str(path), identifier, "identity_too_short", f"{len(text)} символов")
            )


def _extend_item(
    path: Path,
    item: dict,
    fragments: dict[str, str],
    min_chars: int,
    stats: Stats,
) -> bool:
    if not _is_identity_like(path, item):
        return False
    text = _template_text(item)
    if len(text) >= min_chars:
        return False
    if any(marker.search(text) for marker in _PERSONA_MARKERS.values()):
        # Приметы уже есть — не раздуваем текст автоматически.
        return False
    fragment = fragments.get(_mask_of(item), fragments["default"])
    _set_template_text(item, text.rstrip() + "\n\n" + fragment)
    stats.extended += 1
    return True


def _dump(path: Path, docs: list[object]) -> None:
    if path.suffix.casefold() == ".json":
        path.write_text(
            json.dumps(docs[0], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    rendered = yaml.safe_dump_all(
        docs,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("audit", "extend"), default="audit")
    parser.add_argument("--root", default="behavior-pack/template-library")
    parser.add_argument("--fragments", default="behavior-pack/persona-fragments-v1.0.2.yaml")
    parser.add_argument("--min-chars", type=int, default=220)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", default=REPORT_DEFAULT)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        raise SystemExit(f"Каталог шаблонов не найден: {root}")

    fragments: dict[str, str] = {}
    if args.mode == "extend":
        fragments = _load_fragments(Path(args.fragments))

    stats = Stats()
    backup_root = Path(BACKUP_DIR)

    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix.casefold() not in {".yaml", ".yml", ".json"}:
            continue
        stats.files += 1
        try:
            pairs = _iter_template_docs(path)
        except Exception as exc:  # noqa: BLE001 - битый файл идёт в отчёт
            stats.findings.append(Finding(str(path), path.stem, "parse_error", str(exc)))
            continue

        docs_seen: list[object] = []
        changed = False
        for container, item in pairs:
            if container not in docs_seen:
                docs_seen.append(container)
            stats.templates += 1
            _audit_text(path, item, stats)
            if args.mode == "extend":
                if _extend_item(path, item, fragments, args.min_chars, stats):
                    changed = True

        if changed and args.apply:
            backup_path = backup_root / path.relative_to(root.parent)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                shutil.copy2(path, backup_path)
            _dump(path, docs_seen)

    payload = {
        "mode": args.mode,
        "apply": bool(args.apply),
        "root": str(root),
        "files": stats.files,
        "templates": stats.templates,
        "identity_like": stats.identity_like,
        "extended": stats.extended,
        "findings": [
            {
                "file": item.file,
                "template_id": item.template_id,
                "kind": item.kind,
                "detail": item.detail,
            }
            for item in stats.findings
        ],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(rendered + "\n", encoding="utf-8")

    if args.mode == "extend" and stats.extended and not args.apply:
        print(f"\nDRY-RUN: {stats.extended} шаблонов было бы дополнено. Запустите с --apply.")
    if args.strict and stats.findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
