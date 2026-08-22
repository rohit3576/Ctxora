"""Unit tests for config loading: YAML parse + strict validation."""

from pathlib import Path

import pytest

from config.settings import DEFAULT_CONFIG_PATH, ConfigError, Settings, load_app_config

VALID_YAML = """
stores:
  telemetry:
    adapter: postgres
    mapping:
      table: "{tenant}_telemetry"
      timestamp: event_time
      entity_id: device_id
      key: metric
      value: reading
  events:
    enabled: false
agent:
  default_time_window: today
  row_cap: 500
  query_timeout_s: 30
  aggregation_defaults:
    "*": average
flags:
  streaming: true
routing:
  sql_indicators: [average]
  rag_indicators: [manual]
"""


def write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return path


class TestLoadAppConfigWhenDefaultsFile:
    def test_loads_shipped_defaults_into_valid_config(self) -> None:
        config = load_app_config(DEFAULT_CONFIG_PATH)

        assert config.stores.telemetry.adapter in ("clickhouse", "postgres")
        assert config.flags.streaming is True
        assert config.flags.correction_loop is False
        assert config.agent.row_cap > 0
        assert config.routing.sql_indicators
        assert config.routing.rag_indicators

    def test_telemetry_mapping_has_all_column_names(self) -> None:
        config = load_app_config(DEFAULT_CONFIG_PATH)
        mapping = config.stores.telemetry.mapping

        for column in (mapping.timestamp, mapping.entity_id, mapping.key, mapping.value):
            assert column != ""


class TestLoadAppConfigWhenYamlInvalid:
    def test_unknown_top_level_key_is_rejected(self, tmp_path: Path) -> None:
        path = write_yaml(tmp_path, VALID_YAML + "\nmystery_section: {}\n")

        with pytest.raises(ConfigError, match="mystery_section"):
            load_app_config(path)

    def test_missing_required_column_is_rejected(self, tmp_path: Path) -> None:
        broken = VALID_YAML.replace("      value: reading\n", "")
        path = write_yaml(tmp_path, broken)

        with pytest.raises(ConfigError, match="value"):
            load_app_config(path)

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        broken = VALID_YAML.replace("  row_cap: 500", '  row_cap: "many"')
        path = write_yaml(tmp_path, broken)

        with pytest.raises(ConfigError, match="row_cap"):
            load_app_config(path)

    def test_unknown_adapter_is_rejected(self, tmp_path: Path) -> None:
        broken = VALID_YAML.replace("adapter: postgres", "adapter: mongodb")
        path = write_yaml(tmp_path, broken)

        with pytest.raises(ConfigError, match="adapter"):
            load_app_config(path)

    def test_malformed_yaml_is_rejected(self, tmp_path: Path) -> None:
        path = write_yaml(tmp_path, "stores: [unclosed")

        with pytest.raises(ConfigError, match="parse"):
            load_app_config(path)


class TestSettingsEnvMapping:
    def test_env_var_binds_to_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("METADATA_DB_PORT", "6543")

        settings = Settings()

        assert settings.metadata_db_port == 6543

    def test_defaults_are_local_development_friendly(self) -> None:
        settings = Settings()

        assert settings.metadata_db_host == "localhost"
        assert settings.metadata_db_name == "querypulse"
