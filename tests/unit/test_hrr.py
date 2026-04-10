"""Unit tests for HRR (Holographic Reduced Representations) core operations."""

from __future__ import annotations

import numpy as np

from synapto.hrr.core import (
    bind,
    bundle,
    bytes_to_phases,
    encode_atom,
    encode_fact,
    encode_text,
    phases_to_bytes,
    similarity,
    snr_estimate,
    unbind,
)


class TestEncodeAtom:
    def test_deterministic(self):
        a1 = encode_atom("hello")
        a2 = encode_atom("hello")
        np.testing.assert_array_equal(a1, a2)

    def test_different_words_different_vectors(self):
        a = encode_atom("hello")
        b = encode_atom("world")
        assert similarity(a, b) < 0.3

    def test_dimension(self):
        a = encode_atom("test", dim=512)
        assert a.shape == (512,)

    def test_values_in_range(self):
        a = encode_atom("test")
        assert np.all(a >= 0)
        assert np.all(a < 2 * np.pi)

    def test_default_dim_is_1024(self):
        a = encode_atom("test")
        assert a.shape == (1024,)


class TestBindUnbind:
    def test_bind_produces_dissimilar_vector(self):
        a = encode_atom("cat")
        b = encode_atom("mat")
        bound = bind(a, b)
        assert similarity(bound, a) < 0.3
        assert similarity(bound, b) < 0.3

    def test_unbind_roundtrip(self):
        a = encode_atom("key")
        b = encode_atom("value")
        bound = bind(a, b)
        recovered = unbind(bound, a)
        assert similarity(recovered, b) > 0.8

    def test_unbind_wrong_key_fails(self):
        a = encode_atom("key")
        b = encode_atom("value")
        wrong = encode_atom("wrong")
        bound = bind(a, b)
        recovered = unbind(bound, wrong)
        assert similarity(recovered, b) < 0.3


class TestBundle:
    def test_bundle_similar_to_inputs(self):
        a = encode_atom("alpha")
        b = encode_atom("beta")
        bundled = bundle(a, b)
        assert similarity(bundled, a) > 0.3
        assert similarity(bundled, b) > 0.3

    def test_bundle_single_vector(self):
        a = encode_atom("solo")
        bundled = bundle(a)
        assert similarity(bundled, a) > 0.95

    def test_bundle_many_vectors(self):
        vectors = [encode_atom(f"word_{i}") for i in range(10)]
        bundled = bundle(*vectors)
        # should still have some similarity to each component
        for v in vectors:
            assert similarity(bundled, v) > 0.0


class TestSimilarity:
    def test_identical_vectors(self):
        a = encode_atom("same")
        assert abs(similarity(a, a) - 1.0) < 1e-10

    def test_random_vectors_near_zero(self):
        a = encode_atom("aaa")
        b = encode_atom("zzz")
        sim = similarity(a, b)
        assert abs(sim) < 0.2

    def test_range(self):
        a = encode_atom("x")
        b = encode_atom("y")
        sim = similarity(a, b)
        assert -1.0 <= sim <= 1.0


class TestEncodeText:
    def test_similar_texts(self):
        a = encode_text("the cat sat on the mat")
        b = encode_text("the cat sat on the mat today")
        assert similarity(a, b) > 0.3

    def test_different_texts(self):
        a = encode_text("python programming language")
        b = encode_text("ocean waves crashing shore")
        assert similarity(a, b) < 0.3

    def test_empty_text(self):
        a = encode_text("")
        assert a.shape == (1024,)


class TestEncodeFact:
    def test_structured_encoding(self):
        fact = encode_fact("hermes uses outbox pattern", ["hermes", "outbox"])
        assert fact.shape == (1024,)

    def test_entity_extraction_via_unbind(self):
        content = "kafka handles messaging"
        entities = ["kafka"]
        fact = encode_fact(content, entities)
        role_entity = encode_atom("__hrr_role_entity__")
        entity_vec = encode_atom("kafka")
        probe = bind(entity_vec, role_entity)
        residual = unbind(fact, probe)
        role_content = encode_atom("__hrr_role_content__")
        content_vec = bind(encode_text(content), role_content)
        # the residual should have some similarity to the content signal
        sim = similarity(residual, content_vec)
        assert sim > 0.0

    def test_no_entities(self):
        fact = encode_fact("standalone fact", [])
        assert fact.shape == (1024,)


class TestSerialization:
    def test_roundtrip(self):
        original = encode_atom("serialize_me")
        data = phases_to_bytes(original)
        recovered = bytes_to_phases(data)
        np.testing.assert_array_equal(original, recovered)

    def test_bytes_size(self):
        a = encode_atom("test", dim=1024)
        data = phases_to_bytes(a)
        assert len(data) == 1024 * 8  # float64 = 8 bytes


class TestSNR:
    def test_empty_storage(self):
        assert snr_estimate(1024, 0) == float("inf")

    def test_low_capacity(self):
        snr = snr_estimate(1024, 10)
        assert snr > 2.0

    def test_near_capacity_warning(self):
        snr = snr_estimate(1024, 300)
        assert snr < 2.0

    def test_formula(self):
        import math

        snr = snr_estimate(1024, 16)
        assert abs(snr - math.sqrt(1024 / 16)) < 1e-10
