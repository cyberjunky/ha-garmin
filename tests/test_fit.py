"""Tests for FIT file generation."""

from datetime import datetime

from ha_garmin.fit import FitEncoderWeight


def _body_composition_fit(**fields) -> bytes:
    """Build a body composition FIT file the way add_body_composition does."""
    timestamp = datetime(2024, 1, 15, 8, 30, 0)
    encoder = FitEncoderWeight()
    encoder.write_file_info()
    encoder.write_file_creator()
    encoder.write_device_info(timestamp)
    encoder.write_weight_scale(timestamp=timestamp, **fields)
    encoder.finish()
    return encoder.getvalue()


class TestFitGeneration:
    """Tests for FIT file creation."""

    def test_body_composition_fit_minimal(self):
        """A weight-only file has a valid FIT header and size."""
        fit_data = _body_composition_fit(weight=82.3)

        assert fit_data[0] in (12, 14)  # header size
        assert fit_data[8:12] == b".FIT"
        data_size = int.from_bytes(fit_data[4:8], "little")
        assert len(fit_data) == fit_data[0] + data_size + 2  # + file CRC

    def test_body_composition_fit_full(self):
        """All body composition fields fit in one weight_scale record."""
        minimal = _body_composition_fit(weight=82.3)
        fit_data = _body_composition_fit(
            weight=82.3,
            percent_fat=23.6,
            percent_hydration=51.2,
            muscle_mass=35.5,
            bone_mass=3.2,
            visceral_fat_mass=2.5,
            basal_met=1851.76,
            active_met=2500.0,
            metabolic_age=37,
            physique_rating=5,
            visceral_fat_rating=11,
            bmi=24.7,
        )

        # Every field is always written, unset ones as FIT "invalid" (0xFF
        # bytes), so the layout is the same and only the values differ.
        assert fit_data[8:12] == b".FIT"
        assert len(fit_data) == len(minimal)
        assert fit_data.count(0xFF) < minimal.count(0xFF)
