from __future__ import annotations

from dataclasses import dataclass

from faervell_npc.services.actor import ActorService
from faervell_npc.services.cache import SceneLockManager
from faervell_npc.services.characters import CharacterRegistryService
from faervell_npc.services.context import SceneContextBuilder
from faervell_npc.services.decision_cache import DecisionCacheService
from faervell_npc.services.disclosure import LoreDisclosureEngine
from faervell_npc.services.examples import ApprovedExampleService
from faervell_npc.services.guard import OutputGuard
from faervell_npc.services.knowledge import KnowledgeService
from faervell_npc.services.llm import OpenRouterClient
from faervell_npc.services.local_planner import LocalPlanner
from faervell_npc.services.memory import MemoryService
from faervell_npc.services.orchestrator import StrangerOrchestrator
from faervell_npc.services.planner import PlannerService
from faervell_npc.services.presence import PresenceService
from faervell_npc.services.router import IntentRouter
from faervell_npc.services.rules import RuleEngine
from faervell_npc.services.tools import ToolExecutor


@dataclass(slots=True)
class Runtime:
    llm: OpenRouterClient
    locks: SceneLockManager
    characters: CharacterRegistryService
    presence: PresenceService
    knowledge: KnowledgeService
    orchestrator: StrangerOrchestrator

    async def close(self) -> None:
        await self.llm.close()
        await self.locks.close()


def build_runtime() -> Runtime:
    memory = MemoryService()
    characters = CharacterRegistryService()
    presence = PresenceService()
    contexts = SceneContextBuilder(memory, characters)
    router = IntentRouter()
    knowledge = KnowledgeService()
    disclosure = LoreDisclosureEngine()
    rules = RuleEngine()
    llm = OpenRouterClient()
    tools = ToolExecutor(knowledge, rules, disclosure)
    examples = ApprovedExampleService()
    planner = PlannerService(llm, tools, examples)
    local_planner = LocalPlanner(tools)
    decision_cache = DecisionCacheService()
    actor = ActorService(llm)
    guard = OutputGuard()
    orchestrator = StrangerOrchestrator(
        memory=memory,
        contexts=contexts,
        router=router,
        knowledge=knowledge,
        disclosure=disclosure,
        planner=planner,
        local_planner=local_planner,
        decision_cache=decision_cache,
        actor=actor,
        guard=guard,
    )
    return Runtime(
        llm=llm,
        locks=SceneLockManager(),
        characters=characters,
        presence=presence,
        knowledge=knowledge,
        orchestrator=orchestrator,
    )
