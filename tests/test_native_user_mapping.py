"""Only operator configuration can bind an Arrmes subject to a Seerr user."""
import json
import pytest

from arrmes.services.native import ServiceConfig, load_service_configs


def test_seerr_identity_map_is_exact_and_immutable(tmp_path):
    original = {"local:alice": 2}
    config = ServiceConfig("http://localhost:5055", user_ids=original)
    original["local:alice"] = 1
    assert config.user_ids["local:alice"] == 2
    assert "LOCAL:ALICE" not in config.user_ids
    with pytest.raises(TypeError):
        config.user_ids["local:alice"] = 1
    registry = tmp_path / "services.json"
    registry.write_text(json.dumps({"services": {"seerr": {
        "base_url": "http://localhost:5055", "user_ids": {"local:alice": 2}}}}))
    assert load_service_configs(registry)["seerr"].user_ids["local:alice"] == 2


@pytest.mark.parametrize("mapping", [None, [], {"local:alice": True}, {"local:alice": 0},
                                     {"local:alice": 2.0}, {" local:alice": 2},
                                     {"local:alice": 2_147_483_648}, {"": 2}])
def test_invalid_operator_identities_are_refused(mapping):
    with pytest.raises(ValueError):
        ServiceConfig("http://localhost:5055", user_ids=mapping)


def test_another_service_cannot_silently_accept_a_user_map(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": {"sonarr": {
        "base_url": "http://localhost:8989", "user_ids": {}}}}))
    with pytest.raises(ValueError):
        load_service_configs(path)
