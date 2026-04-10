"""Unit tests for Synapto decay system."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from synapto.decay.scoring import calculate_decay_score


class TestDecayScoring:
    def _now(self):
        return datetime.now(UTC)

    def test_core_never_decays(self):
        old_time = self._now() - timedelta(days=365)
        score = calculate_decay_score("core", old_time, old_time, 0, self._now())
        assert score == 1.0

    def test_core_with_access_boost(self):
        old_time = self._now() - timedelta(days=365)
        score = calculate_decay_score("core", old_time, old_time, 10, self._now())
        assert score > 1.0
        assert score <= 1.3

    def test_ephemeral_decays_fast(self):
        now = self._now()
        # after 6 hours (half-life), score should be ~0.5
        six_hours_ago = now - timedelta(hours=6)
        score = calculate_decay_score("ephemeral", six_hours_ago, six_hours_ago, 0, now)
        assert 0.45 <= score <= 0.55

    def test_ephemeral_after_24h_is_very_low(self):
        now = self._now()
        day_ago = now - timedelta(hours=24)
        score = calculate_decay_score("ephemeral", day_ago, day_ago, 0, now)
        assert score < 0.1

    def test_working_after_one_week(self):
        now = self._now()
        week_ago = now - timedelta(days=7)
        score = calculate_decay_score("working", week_ago, week_ago, 0, now)
        assert 0.45 <= score <= 0.55

    def test_stable_barely_decays_in_a_week(self):
        now = self._now()
        week_ago = now - timedelta(days=7)
        score = calculate_decay_score("stable", week_ago, week_ago, 0, now)
        assert score > 0.9

    def test_access_refreshes_decay(self):
        now = self._now()
        created = now - timedelta(days=30)
        # accessed recently
        recently_accessed = now - timedelta(hours=1)
        score = calculate_decay_score("working", created, recently_accessed, 5, now)
        assert score > 0.9

    def test_access_boost_is_capped(self):
        now = self._now()
        score = calculate_decay_score("core", now, now, 1000, now)
        assert score <= 1.3

    def test_fresh_memory_has_full_score(self):
        now = self._now()
        score = calculate_decay_score("working", now, now, 0, now)
        assert score == 1.0
