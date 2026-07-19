from faervell_npc.services.characters import CharacterRegistryService, CharacterSheetParser

SHEET = """
[Начальный персонаж]
0.0 Игрок: <@123456789>
1.1 Имя персонажа: Йорик.
1.2 Прозвище: Хряк.
1.3 Раса персонажа и ее подвид: Гоблин.
2.1 Возраст: 17 лет.
2.2 Пол: Мужской.
2.3 Рост: 95 сантиметров.

Внешность:
Йорик — низкий серокожий гоблин с короткими чёрными волосами,
зелёными глазами, длинными ушами и ожогом на левой ладони.
Изображение
"""


def test_character_sheet_parser_extracts_visible_identity() -> None:
    parsed = CharacterSheetParser.parse(SHEET)
    assert parsed is not None
    assert parsed.canonical_name == "Йорик"
    assert parsed.aliases == ["Йорик", "Хряк"]
    assert parsed.race == "Гоблин"
    assert parsed.height_cm == 95.0
    assert "зелёными глазами" in parsed.appearance
    assert "Изображение" not in parsed.appearance


def test_presentation_detection_supports_name_and_appearance() -> None:
    named = CharacterRegistryService.extract_presentation("Я Йорик. Ищу работу.")
    assert named.presented_name == "Йорик"
    assert named.is_presentation

    described = CharacterRegistryService.extract_presentation(
        "Я гоблин ростом 95 сантиметров, с зелёными глазами и ожогом на ладони."
    )
    assert described.presented_name is None
    assert described.has_appearance
    assert described.is_presentation

    ordinary = CharacterRegistryService.extract_presentation("Я хочу узнать цену трав.")
    assert not ordinary.is_presentation
