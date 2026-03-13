from pathlib import Path

from aspire_orchestrator.services.provider_secret_registry import (
    get_provider_secret_alias_map,
    get_provider_secret_registry,
    is_registry_provider_configured,
)


def test_provider_secret_registry_has_unique_provider_ids() -> None:
    registry = get_provider_secret_registry()
    providers = [item["provider"] for item in registry]
    assert len(providers) == len(set(providers))


def test_provider_secret_registry_alias_map_contains_quickbooks_alias() -> None:
    aliases = get_provider_secret_alias_map()
    assert aliases["qbo"] == "quickbooks"


def test_provider_secret_registry_automated_entries_have_adapter_names() -> None:
    automated = [item for item in get_provider_secret_registry() if item["rotation_mode"] == "automated"]
    assert automated
    for item in automated:
        assert item["adapter_type"] == "aws_rotation_lambda"
        assert item["adapter_name"]
        assert item["verification_source"] == "aws_step_functions"


def test_provider_secret_registry_contains_internal_automation_group() -> None:
    registry = {item["provider"]: item for item in get_provider_secret_registry()}
    internal = registry["internal"]
    assert internal["rotation_mode"] == "automated"
    assert internal["adapter_name"] == "internal"


def test_provider_secret_registry_configuration_groups_require_all_groups() -> None:
    registry = {item["provider"]: item for item in get_provider_secret_registry()}
    assert is_registry_provider_configured(
        registry["twilio"],
        {
            "ASPIRE_TWILIO_ACCOUNT_SID": "AC123",
            "ASPIRE_TWILIO_AUTH_TOKEN": "secret",
        },
    )
    assert not is_registry_provider_configured(
        registry["twilio"],
        {"ASPIRE_TWILIO_ACCOUNT_SID": "AC123"},
    )


def test_provider_secret_registry_file_exists() -> None:
    registry_path = Path(__file__).resolve().parents[2] / "config" / "provider_secret_registry.json"
    assert registry_path.exists()
