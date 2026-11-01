import pytest

from sim_bim.drawing.units import fit_chain, fit_levels, height_m, parse_length


@pytest.mark.parametrize(
    ("label", "metres"),
    [
        ("24'-4 1/2\"", 7.4295),
        ("6'-6\"", 1.9812),
        ('5 1/2"', 0.1397),
        ("28\N{PRIME}-6 1/2\N{DOUBLE PRIME}", 8.6995),
        ("4200", 4.2),
        ("2.5 m", 2.5),
    ],
)
def test_parse_length(label: str, metres: float) -> None:
    assert parse_length(label) == pytest.approx(metres, abs=1e-4)


def test_parse_length_rejects_text() -> None:
    with pytest.raises(ValueError, match="unrecognised"):
        parse_length("about four metres")


def test_fit_chain_recovers_scale_and_flags_misread_label() -> None:
    scale = fit_chain([100.0, 520.0, 860.0], ["4200", "3400"])
    assert scale.px_per_m == pytest.approx(100.0)
    assert scale.to_m(520.0) == pytest.approx(4.2)

    misread = fit_chain([100.0, 520.0, 860.0], ["4200", "4300"])
    assert misread.residual_m > 0.2


def test_fit_levels_measures_heights_upwards() -> None:
    scale = fit_levels([1000.0, 616.0], ["0'-0\"", "12'-7\""])
    assert height_m(scale, 616.0) == pytest.approx(3.8354, abs=1e-3)
    assert height_m(scale, 1000.0) == pytest.approx(0.0, abs=1e-6)
