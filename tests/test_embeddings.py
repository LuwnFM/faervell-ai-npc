from faervell_npc.services.embeddings import HashingEmbedder


def test_hashing_embedding_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbedder(64)
    first = embedder.embed("Серебролист растёт у северных склонов")
    second = embedder.embed("Серебролист растёт у северных склонов")
    assert first == second
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9
