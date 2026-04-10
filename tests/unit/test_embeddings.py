"""Unit tests for Synapto embedding providers."""

from __future__ import annotations

import pytest

from synapto.embeddings.base import EmbeddingProvider
from synapto.embeddings.registry import get_provider, list_providers
from synapto.embeddings.sentence_transformer import SentenceTransformerProvider


class TestSentenceTransformerProvider:
    @pytest.fixture
    def provider(self):
        return SentenceTransformerProvider()

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

    def test_get_provider_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            get_provider("nonexistent-provider")

    def test_list_providers(self):
        providers = list_providers()
        assert "sentence-transformers" in providers
        assert "openai" in providers
