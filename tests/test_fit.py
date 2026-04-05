"""Tests for FIT file generation."""

from datetime import datetime

from ha_garmin.fit import create_body_composition_fit


class TestFitGeneration:
    """Tests for FIT file creation."""

    def test_create_body_composition_fit_minimal(self):
        """Test creating body composition FIT with minimal data."""
        timestamp = datetime(2024, 1, 15, 8, 30, 0)
        fit_data = create_body_composition_fit(
            weight=82.3,
            timestamp=timestamp,
        )

        assert isinstance(fit_data, bytes)
        assert len(fit_data) > 0
        # FIT files start with header size byte (usually 12 or 14)
        assert fit_data[0] in (12, 14)

    def test_create_body_composition_fit_full(self):
        """Test creating body composition FIT with all fields."""
        timestamp = datetime(2024, 1, 15, 8, 30, 0)
        fit_data = create_body_composition_fit(
            weight=82.3,
            timestamp=timestamp,
            percent_fat=23.6,
            percent_hydration=51.2,
            muscle_mass=35.5,
            bone_mass=3.2,
            visceral_fat_mass=2.5,
            metabolic_age=37,
            physique_rating=5,
            bmi=24.7,
        )

        assert isinstance(fit_data, bytes)
        assert len(fit_data) > 100  # Should be larger with more data
