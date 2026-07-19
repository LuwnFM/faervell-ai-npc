from types import SimpleNamespace

from faervell_npc.config import Settings
from faervell_npc.discord_bot import FaervellBot, ResponseFeedbackView
from faervell_npc.services.characters import CharacterRegistryService
from faervell_npc.services.ingest import SourceIngestor
from faervell_npc.services.knowledge import KnowledgeService
from faervell_npc.services.stagecraft import choose_activity, choose_opener


def test_fandom_localised_api_endpoint() -> None:
    api, prefix = SourceIngestor._fandom_api_url(
        "https://faervellrp.fandom.com/ru/wiki/Королевство_Ивелтин"
    )
    assert api == "https://faervellrp.fandom.com/ru/api.php"
    assert prefix == "/ru"


def test_fandom_infobox_is_indexed() -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        """
        <div class="mw-parser-output">
          <aside class="portable-infobox">
            <div class="pi-data" data-source="ruler">
              <h3 class="pi-data-label">Правитель</h3>
              <div class="pi-data-value">Король Тестовый</div>
            </div>
            <div class="pi-data" data-source="date">
              <h3 class="pi-data-label">Текущая дата</h3>
              <div class="pi-data-value">19 день Жатвы, 124 год</div>
            </div>
          </aside>
          <p>Королевство находится на западном побережье.</p>
        </div>
        """,
        "lxml",
    )
    root = soup.select_one(".mw-parser-output")
    assert root is not None
    sections = SourceIngestor._sections_from_html(root)
    joined = "\n".join(body for _, body in sections)
    assert "Правитель: Король Тестовый" in joined
    assert "Текущая дата: 19 день Жатвы, 124 год" in joined
    assert "западном побережье" in joined


def test_russian_title_stem_handles_iveltin_genitive() -> None:
    terms = KnowledgeService._query_terms("Кто король Королевства Ивелтин и где находится Ивелтина?")
    assert "ивелтин" in terms
    assert "королевств" in terms


def test_plain_full_name_is_a_presentation() -> None:
    presentation = CharacterRegistryService.extract_presentation("Лука Дер Вадре")
    assert presentation.is_presentation
    assert presentation.presented_name == "Лука Дер Вадре"


def test_stagecraft_avoids_recent_action_and_old_buckle_loop() -> None:
    recent = ["Странник проверяет застёжки дорожной сумы."]
    activity = choose_activity("traveler", recent, "scene:1")
    assert activity not in recent[0]
    assert "пряжк" not in activity.casefold()
    opener = choose_opener(["Странник выслушивает слова до конца, лишь затем поднимая глаза."], "scene:2")
    assert "выслушивает слова до конца" not in opener


def test_v07_startup_lock_defaults_are_hard() -> None:
    settings = Settings()
    assert settings.traveler_enforce_startup_lock
    assert settings.traveler_startup_lock_channel_id == 1488544832950374481


def test_footer_is_only_on_last_split_message() -> None:
    class Dummy:
        settings = SimpleNamespace(
            discord_reply_hint_text="Ответьте или упомяните Странника.",
            discord_model_footer_enabled=True,
        )
        _split_message = staticmethod(FaervellBot._split_message)

    parts = FaervellBot._response_parts(
        Dummy(),
        "слово " * 800,
        enabled=True,
        model="test/model",
    )
    assert len(parts) > 1
    assert all("Модель:" not in part for part in parts[:-1])
    assert "||Ответьте или упомяните Странника.||" in parts[-1]
    assert "-# Модель: `test/model`" in parts[-1]


def test_feedback_view_has_like_dislike_and_regenerate_buttons() -> None:
    view = ResponseFeedbackView(SimpleNamespace(), "00000000-0000-0000-0000-000000000000")
    labels = {getattr(child, "label", None) for child in view.children}
    assert {"Нравится", "Не нравится", "Перегенерировать"}.issubset(labels)


def test_lore_query_expansion_targets_infobox_fields() -> None:
    expanded = KnowledgeService._expand_query(
        "Кто сейчас король Ивелтина, где он находится и какое сейчас число?"
    )
    assert "нынешний глава" in expanded
    assert "расположение" in expanded
    assert "текущая дата" in expanded


def test_paid_model_with_fixed_request_fee_is_rejected_when_limit_is_zero() -> None:
    from faervell_npc.services.llm import CatalogModel, OpenRouterClient

    client = OpenRouterClient.__new__(OpenRouterClient)
    client.settings = Settings(
        openrouter_allow_paid_fallback=True,
        openrouter_max_prompt_price_per_million=0.20,
        openrouter_max_completion_price_per_million=0.20,
        openrouter_max_request_price_usd=0.0,
    )
    model = CatalogModel(
        id="vendor/cheap-but-fixed",
        prompt_per_million=0.10,
        completion_per_million=0.10,
        request_price=0.01,
        context_length=1000,
        supported_parameters=set(),
        free=False,
    )
    assert not client._catalog_allowed(model)
