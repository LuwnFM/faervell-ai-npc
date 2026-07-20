from __future__ import annotations

from .enums import AttributionMode, DisclosureScope
from .schemas import MemoryRecallItem


def can_disclose(item: MemoryRecallItem, *, active_character_id: str) -> bool:
    if item.disclosure_scope.value == DisclosureScope.GM_RESTRICTED.value:
        return False
    if item.attribution_mode == AttributionMode.PRIVATE and item.speaker_character_id == active_character_id:
        return False
    return True
