from __future__ import annotations

from dataclasses import dataclass, field

from faervell_npc.schemas import QuestDraft


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    requires_gm_approval: bool = False


class RuleEngine:
    MAX_SMALL_QUEST_REWARD = 12.0
    ALLOWED_TEMPLATES_BY_MASK = {
        "traveler": {"DELIVER_ITEM", "INVESTIGATE", "FIND_LOCATION"},
        "herbalist": {"COLLECT", "DELIVER_ITEM", "CRAFT"},
        "artisan": {"COLLECT", "CRAFT", "REPAIR"},
        "merchant": {"DELIVER_ITEM", "COLLECT"},
        "guide": {"FIND_LOCATION", "ESCORT", "INVESTIGATE"},
    }

    def validate_quest(self, quest: QuestDraft, profession_mask_id: str) -> ValidationResult:
        errors: list[str] = []
        allowed = self.ALLOWED_TEMPLATES_BY_MASK.get(profession_mask_id, set())
        if quest.template_id not in allowed:
            errors.append("profession_mask_cannot_issue_template")
        if quest.reward_amount > self.MAX_SMALL_QUEST_REWARD and not quest.gm_approval_required:
            errors.append("reward_exceeds_small_quest_limit")
        if not quest.evidence and not quest.gm_approval_required:
            errors.append("quest_has_no_evidence")

        objective_ids = {objective.id for objective in quest.objectives}
        if len(objective_ids) != len(quest.objectives):
            errors.append("duplicate_objective_ids")
        for objective in quest.objectives:
            unknown_dependencies = set(objective.depends_on) - objective_ids
            if unknown_dependencies:
                errors.append(f"unknown_dependencies:{objective.id}")
        if self._has_cycle(quest):
            errors.append("objective_graph_has_cycle")

        high_risk = any(obj.type in {"ESCORT", "INVESTIGATE"} for obj in quest.objectives)
        return ValidationResult(
            valid=not errors,
            errors=errors,
            requires_gm_approval=quest.gm_approval_required or high_risk,
        )

    @staticmethod
    def _has_cycle(quest: QuestDraft) -> bool:
        graph = {obj.id: obj.depends_on for obj in quest.objectives}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(dep) for dep in graph.get(node, [])):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)
