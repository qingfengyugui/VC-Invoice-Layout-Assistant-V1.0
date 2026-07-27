import pytest

from invoice_layout.config import Settings


def test_settings_reject_unsafe_margins() -> None:
    try:
        Settings(page_margin_mm=5)
    except ValueError as exc:
        assert "12" in str(exc)
    else:
        raise AssertionError("page_margin_mm=5 must fail")


def test_provider_auto_selects_host_only_with_supplied_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    manifest = tmp_path / "observations.json"
    manifest.write_text("{}", "utf-8")

    assert Settings(provider="auto", host_manifest=manifest).resolved_provider() == "host"
    assert Settings(provider="auto").resolved_provider() == "local"


@pytest.mark.parametrize("provider", ["openai", "invalid"])
def test_provider_rejects_unsupported_values(provider: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        Settings(provider=provider)
