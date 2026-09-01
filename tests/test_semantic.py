import pytest

from chongzu import paths
from chongzu.semantic import LLMConfig


def test_example_llm_config_is_provider_neutral_and_inactive() -> None:
    config = LLMConfig.load(paths.PROJECT_ROOT / "config" / "llm.example.json")
    assert config.model == "Qwen3.6-35B-A3B"
    assert config.timeout_seconds == 60
    with pytest.raises(ValueError, match="base_url"):
        config.validate_for_use()


def test_llm_config_does_not_force_one_model() -> None:
    config = LLMConfig.from_mapping(
        {"base_url": "http://127.0.0.1:9000/v1", "api_key": "configured", "model": "another-model"}
    )
    config.validate_for_use()
    assert config.model == "another-model"
