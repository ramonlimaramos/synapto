"""Holographic Reduced Representations (HRR) with phase encoding.

HRRs are a Vector Symbolic Architecture for encoding compositional structure into
fixed-width distributed representations. This module uses *phase vectors*: each
concept is a vector of angles in [0, 2pi). The algebraic operations are:

    bind   -- circular convolution (phase addition)  -- associates two concepts
    unbind -- circular correlation (phase subtraction) -- retrieves a bound value
    bundle -- superposition (circular mean)           -- merges multiple concepts

Phase encoding is numerically stable, avoids the magnitude collapse of traditional
complex-number HRRs, and maps cleanly to cosine similarity.

Atoms are generated deterministically from SHA-256 so representations are identical
across processes, machines, and language versions.

Design pattern: Strategy -- the HRR core provides interchangeable encoding strategies
(bag-of-words via encode_text, structured via encode_fact) behind a uniform interface
of phase vectors and algebraic operations.

References:
    Plate (1995) -- Holographic Reduced Representations
    Gayler (2004) -- Vector Symbolic Architectures answer Jackendoff's challenges
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct

import numpy as np

logger = logging.getLogger("synapto.hrr.core")

_TWO_PI = 2.0 * math.pi
DEFAULT_DIM = 1024


def encode_atom(word: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Deterministic phase vector via SHA-256 counter blocks.

    Algorithm:
    - Generate SHA-256 blocks by hashing ``f"{word}:{i}"`` for i=0,1,...
    - Interpret digests as uint16 values, scale to [0, 2pi)
    - O(dim) time, O(dim) space
    """
    values_per_block = 16  # 32 bytes / 2 bytes per uint16
    blocks_needed = math.ceil(dim / values_per_block)

    uint16_values: list[int] = []
    for i in range(blocks_needed):
        digest = hashlib.sha256(f"{word}:{i}".encode()).digest()
        uint16_values.extend(struct.unpack("<16H", digest))

    return np.array(uint16_values[:dim], dtype=np.float64) * (_TWO_PI / 65536.0)


def bind(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Circular convolution = element-wise phase addition.

    Binding associates two concepts into a single composite vector.
    The result is quasi-orthogonal to both inputs.
    O(dim) time, O(dim) space.
    """
    return (a + b) % _TWO_PI


def unbind(memory: np.ndarray, key: np.ndarray) -> np.ndarray:
    """Circular correlation = element-wise phase subtraction.

    Retrieves the value associated with a key from a memory vector.
    ``unbind(bind(a, b), a) ~ b`` (up to superposition noise).
    O(dim) time, O(dim) space.
    """
    return (memory - key) % _TWO_PI


def bundle(*vectors: np.ndarray) -> np.ndarray:
    """Superposition via circular mean of complex exponentials.

    Merges multiple vectors into one that is similar to each input.
    Capacity: O(sqrt(dim)) items before similarity degrades.
    O(n * dim) time where n = number of vectors.
    """
    complex_sum = np.sum([np.exp(1j * v) for v in vectors], axis=0)
    return np.angle(complex_sum) % _TWO_PI


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Phase cosine similarity in [-1, 1].

    Returns 1.0 for identical vectors, ~0.0 for unrelated, -1.0 for anti-correlated.
    O(dim) time.
    """
    return float(np.mean(np.cos(a - b)))


def encode_text(text: str, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Bag-of-words encoding: bundle of atom vectors for each token.

    Tokenizes by lowercasing, splitting on whitespace, stripping punctuation.
    Returns bundle of all token atom vectors, or a sentinel for empty text.
    """
    tokens = [
        token.strip(".,!?;:\"'()[]{}")
        for token in text.lower().split()
    ]
    tokens = [t for t in tokens if t]

    if not tokens:
        return encode_atom("__hrr_empty__", dim)

    return bundle(*(encode_atom(token, dim) for token in tokens))


def encode_fact(content: str, entities: list[str], dim: int = DEFAULT_DIM) -> np.ndarray:
    """Structured encoding: content bound to ROLE_CONTENT, entities bound to ROLE_ENTITY.

    Enables algebraic extraction:
        ``unbind(fact, bind(entity, ROLE_ENTITY)) ~ content_vector``
    """
    role_content = encode_atom("__hrr_role_content__", dim)
    role_entity = encode_atom("__hrr_role_entity__", dim)

    components: list[np.ndarray] = [bind(encode_text(content, dim), role_content)]
    for entity in entities:
        components.append(bind(encode_atom(entity.lower(), dim), role_entity))

    return bundle(*components)


def phases_to_bytes(phases: np.ndarray) -> bytes:
    """Serialize phase vector to bytes (float64 tobytes -- 8 KB at dim=1024)."""
    return phases.tobytes()


def bytes_to_phases(data: bytes) -> np.ndarray:
    """Deserialize bytes back to phase vector."""
    return np.frombuffer(data, dtype=np.float64).copy()


def snr_estimate(dim: int, n_items: int) -> float:
    """Signal-to-noise ratio estimate for holographic storage.

    SNR = sqrt(dim / n_items). Falls below 2.0 when n_items > dim / 4,
    meaning retrieval errors become likely.
    """
    if n_items <= 0:
        return float("inf")

    snr = math.sqrt(dim / n_items)
    if snr < 2.0:
        logger.warning(
            "HRR near capacity: SNR=%.2f (dim=%d, items=%d) — "
            "consider increasing dim or reducing stored items",
            snr, dim, n_items,
        )
    return snr
