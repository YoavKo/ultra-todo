from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_license_is_present_and_declared():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Copyright (c)" in text and "Permission is hereby granted" in text
    assert 'license = "MIT"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_bundled_mermaid_keeps_its_own_license():
    vendor = ROOT / "src" / "static" / "vendor"
    assert "MIT" in (vendor / "mermaid.LICENSE").read_text(encoding="utf-8")
    assert (vendor / "mermaid.min.js").stat().st_size > 1_000_000
    assert "mermaid.LICENSE" in (ROOT / "LICENSE").read_text(encoding="utf-8")  # the notice points at it
