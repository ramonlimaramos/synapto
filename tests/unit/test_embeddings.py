"""Unit tests for Synapto embedding providers."""

from __future__ import annotations

import pytest

from synapto.embeddings.base import EmbeddingProvider
from synapto.embeddings.registry import get_provider, list_providers
from synapto.embeddings.sentence_transformer import SentenceTransformerProvider


class FakeVector(list):
    def tolist(self) -> list[float]:
        return list(self)


class FakeSentenceTransformerModel:
    def __init__(self) -> None:
        self.encode_calls: list[dict[str, object]] = []

    def get_embedding_dimension(self) -> int:
        return 384

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool,
        show_progress_bar: bool,
    ) -> list[FakeVector]:
        self.encode_calls.append({
            "texts": texts,
            "normalize_embeddings": normalize_embeddings,
            "show_progress_bar": show_progress_bar,
        })
        return [_fake_vector(text) for text in texts]


def _fake_vector(text: str) -> FakeVector:
    lower_text = text.lower()
    if "cat" in lower_text:
        index = 0
    elif "quantum" in lower_text:
        index = 1
    elif "chocolate" in lower_text or "cake" in lower_text:
        index = 2
    else:
        index = sum(ord(char) for char in lower_text) % 384

    vector = FakeVector([0.0] * 384)
    vector[index] = 1.0
    return vector


@pytest.fixture(autouse=True)
def fake_sentence_transformer_model(monkeypatch):
    model = FakeSentenceTransformerModel()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SYNAPTO_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setattr("synapto.embeddings.sentence_transformer._load_model", lambda _model_name, _device: model)
    return model


@pytest.fixture
def provider():
    return SentenceTransformerProvider()


class TestSentenceTransformerProvider:
    async def test_embed_returns_correct_dimension(self, provider):
        vectors = await provider.embed(["hello world"])
        assert len(vectors) == 1
        assert len(vectors[0]) == provider.dimension

    async def test_embed_multiple_texts(self, provider):
        texts = ["first text", "second text", "third text"]
        vectors = await provider.embed(texts)
        assert len(vectors) == 3
        for vec in vectors:
            assert len(vec) == provider.dimension

    async def test_embed_one(self, provider):
        vec = await provider.embed_one("single text")
        assert len(vec) == provider.dimension

    async def test_embed_normalizes_embeddings(self, provider, fake_sentence_transformer_model):
        await provider.embed(["hello world"])
        assert fake_sentence_transformer_model.encode_calls == [{
            "texts": ["hello world"],
            "normalize_embeddings": True,
            "show_progress_bar": False,
        }]

    def test_uses_explicit_device(self):
        provider = SentenceTransformerProvider(device="cpu")

        assert provider.device == "cpu"

    def test_uses_environment_device(self, monkeypatch):
        monkeypatch.setenv("SYNAPTO_EMBEDDING_DEVICE", "cpu")

        provider = SentenceTransformerProvider()

        assert provider.device == "cpu"

    def test_dimension_is_384(self, provider):
        assert provider.dimension == 384

    def test_name(self, provider):
        assert "sentence-transformers" in provider.name

    async def test_similar_texts_have_close_embeddings(self, provider):
        import numpy as np

        vecs = await provider.embed(["the cat sat on the mat", "a cat was sitting on a rug"])
        similarity = np.dot(vecs[0], vecs[1])
        assert similarity > 0.7

    async def test_different_texts_have_distant_embeddings(self, provider):
        import numpy as np

        vecs = await provider.embed(["quantum physics equations", "chocolate cake recipe"])
        similarity = np.dot(vecs[0], vecs[1])
        assert similarity < 0.5


class TestRegistry:
    def test_get_provider_default(self):
        provider = get_provider()
        assert isinstance(provider, EmbeddingProvider)
        assert provider.dimension > 0

    def test_get_provider_by_name(self):
        provider = get_provider("sentence-transformers")
        assert isinstance(provider, SentenceTransformerProvider)

    def test_get_provider_forwards_device(self):
        provider = get_provider("sentence-transformers", device="cpu")

        assert isinstance(provider, SentenceTransformerProvider)
        assert provider.device == "cpu"

    def test_get_openai_provider_drops_sentence_transformer_device(self, monkeypatch):
        class FakeOpenAIProvider:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        monkeypatch.setattr(
            "synapto.embeddings.openai_provider.OpenAIProvider",
            FakeOpenAIProvider,
        )

        provider = get_provider("openai", model_name="text-embedding-3-small", device="cpu")

        assert provider.kwargs == {"model": "text-embedding-3-small"}

    def test_get_provider_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            get_provider("nonexistent-provider")

    def test_list_providers(self):
        providers = list_providers()
        assert "sentence-transformers" in providers
        assert "openai" in providers
