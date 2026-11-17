"""Comprehensive unit tests for U1: Settings, config_schema, and ConfigBus."""

from __future__ import annotations

import copy
from dataclasses import asdict, fields, FrozenInstanceError
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest

from trading_system.config import Settings, collect_settings_invariant_errors, parse_env_file
from trading_system.config_bus import (
    ConfigBus,
    ConfigConflictError,
    ConfigSnapshot,
    ConfigValidationError,
    FrozenMapping,
)
from trading_system.config_schema import (
    ALLOWLISTED_SCREENER_METRICS,
    BUILTIN_PROFILES,
    ENV_ONLY_KEYS,
    MAX_DYNAMIC_PARAMETERS,
    MAX_DYNAMIC_STRING_LENGTH,
    SCHEMA_BY_KEY,
    SETTINGS_FIELD_NAMES,
    SNAPSHOT_METADATA_KEYS,
    DynamicParameterSpec,
    ScreenerMetricGate,
    evaluate_screener_metric_gate,
    export_config_schema,
    validate_builtin_profiles,
)


def _valid_dynamic_spec(key: str = "CUSTOM_RSI_MIN", **overrides) -> dict:
    payload = {
        "key": key,
        "label": "Custom RSI Floor",
        "type": "float",
        "default": 35.0,
        "group": "screener",
        "unit": "pts",
        "min": 10.0,
        "max": 80.0,
        "step": 1.0,
        "reload_mode": "hot_reload",
        "revalidation_scope": "abort_stale_scan",
        "impact_hint": "Filters out oversold setups below threshold",
        "screener_metric_gate": {
            "metric": "rsi14",
            "op": ">=",
            "threshold": 35.0,
            "on_missing": "reject",
        },
    }
    payload.update(overrides)
    return payload


class TestConfigSchemaAndSettings(unittest.TestCase):
    def test_schema_partitions_all_settings_fields_without_secrets(self) -> None:
        settings_names = {f.name for f in fields(Settings)}
        self.assertEqual(SETTINGS_FIELD_NAMES, frozenset(settings_names))
        self.assertEqual(set(SCHEMA_BY_KEY) & ENV_ONLY_KEYS, set())
        self.assertEqual(
            (set(SCHEMA_BY_KEY) | ENV_ONLY_KEYS) - {"ACTIVE_TRADES_FILE"},
            settings_names,
        )

        # Explicitly verify secrets, USE_LIVE, URLs, ports, and file paths stay env-only.
        expected_env_only = {
            "TRADING212_API_KEY",
            "TRADING212_API_SECRET",
            "OPENAI_API_KEY",
            "FMP_API_KEY",
            "TIINGO_API_KEY",
            "POLYGON_API_KEY",
            "ALPHA_VANTAGE_API_KEY",
            "FINNHUB_API_KEY",
            "OLOSTEP_API_KEY",
            "NIXTLA_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "DISCORD_WEBHOOK_URL",
            "DASHBOARD_AUTH_TOKEN",
            "PANIC_FLATTEN_TOKEN",
            "USE_LIVE",
            "TRADING212_BASE_URL",
            "TRADING212_LIVE_URL",
            "DASHBOARD_PORT",
            "ENABLE_EMBEDDED_DASHBOARD",
            "CANDLE_DB_PATH",
            "TRADES_LOG_PATH",
            "ACTIVE_TRADES_PATH",
            "NOTIFICATIONS_LOG_PATH",
            "ACTIVE_TRADES_FILE",
        }
        self.assertEqual(expected_env_only, ENV_ONLY_KEYS)
        for key in expected_env_only:
            self.assertNotIn(key, SCHEMA_BY_KEY)

    def test_new_settings_fields_and_snapshot_defaults_match(self) -> None:
        s = Settings()
        s.TRADING212_API_KEY = "test-key"
        self.assertAlmostEqual(s.ETP_MIN_SL_PCT, 0.025)
        self.assertAlmostEqual(s.ETP_MAX_SL_PCT, 0.080)
        self.assertAlmostEqual(s.TSL_DECAY_DAY1_MULT, 2.20)
        self.assertAlmostEqual(s.TSL_DECAY_STEP_MULT, 0.35)
        self.assertAlmostEqual(s.TSL_DECAY_FLOOR_MULT, 1.15)
        self.assertAlmostEqual(s.TSL_DECAY_RUNNER_MULT, 2.00)
        self.assertEqual(s.SCREENER_MAX_PER_CATEGORY, 2)
        self.assertAlmostEqual(s.FALLBACK_SCREENER_MIN_ADX, 20.0)
        s.validate()

        with tempfile.TemporaryDirectory() as tmp:
            bus = ConfigBus(data_dir=tmp, ignore_process_env=True)
            snap = bus.active_snapshot()
            default_snap = ConfigSnapshot()
            for key in SCHEMA_BY_KEY:
                self.assertEqual(
                    getattr(snap, key),
                    getattr(default_snap, key),
                    f"ConfigSnapshot default mismatch for '{key}'",
                )

    def test_builtin_profiles_valid_and_exported(self) -> None:
        validate_builtin_profiles()
        self.assertEqual(
            set(BUILTIN_PROFILES.keys()),
            {"balanced", "conservative", "momentum_strict"},
        )
        exported = export_config_schema()
        self.assertIn("parameters", exported)
        self.assertIn("builtin_profiles", exported)
        self.assertIn("allowlisted_screener_metrics", exported)
        for profile_id, profile in BUILTIN_PROFILES.items():
            candidate = Settings()
            for key, val in profile["values"].items():
                self.assertIn(key, SCHEMA_BY_KEY)
                setattr(candidate, key, val)
            self.assertEqual(
                collect_settings_invariant_errors(candidate),
                [],
                f"Profile '{profile_id}' failed Settings invariants",
            )

    def test_allowlisted_screener_metrics_and_gate_evaluation(self) -> None:
        for required_metric in (
            "adx",
            "plus_di",
            "minus_di",
            "rsi14",
            "ema20_slope_5d",
            "atr_ratio",
            "atr_spike_ratio",
            "realized_vol",
            "earnings_in_days",
        ):
            self.assertIn(required_metric, ALLOWLISTED_SCREENER_METRICS)
        self.assertNotIn("trend", ALLOWLISTED_SCREENER_METRICS)

        gate = ScreenerMetricGate(metric="rsi14", op=">=", threshold=40.0, on_missing="reject")
        passed, val, eff, reason = evaluate_screener_metric_gate(
            gate,
            {"rsi14": 45.0},
            active_value=50.0,
        )
        self.assertFalse(passed)
        self.assertEqual(val, 45.0)
        self.assertEqual(eff, 50.0)
        self.assertEqual(reason, "failed:rsi14(45.0)>=50.0")

        passed_missing, val_m, _, reason_m = evaluate_screener_metric_gate(
            ScreenerMetricGate(metric="rsi14", op=">=", threshold=40.0, on_missing="skip_gate"),
            {},
        )
        self.assertTrue(passed_missing)
        self.assertIsNone(val_m)
        self.assertEqual(reason_m, "missing_metric_skipped")

    def test_dynamic_parameter_spec_validation_rules(self) -> None:
        # Valid spec succeeds
        spec = DynamicParameterSpec.from_dict(_valid_dynamic_spec())
        self.assertEqual(spec.key, "CUSTOM_RSI_MIN")

        # Missing or invalid gate rejected
        bad_no_gate = _valid_dynamic_spec()
        del bad_no_gate["screener_metric_gate"]
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(bad_no_gate)

        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(
                _valid_dynamic_spec(
                    screener_metric_gate={
                        "metric": "nonexistent_indicator",
                        "op": ">",
                        "threshold": 1.0,
                        "on_missing": "reject",
                    }
                )
            )

        # Identifier regex enforcement
        for bad_key in ("1BAD", "A", "HAS-HYPHEN", "A" * 65, "HAS SPACE"):
            with self.assertRaises(ValueError):
                DynamicParameterSpec.from_dict(_valid_dynamic_spec(key=bad_key))

        # Collisions with Settings keys & metadata keys forbidden
        for reserved in (
            "MIN_ADX_TREND",
            "TRADING212_API_KEY",
            "config_epoch_id",
            "generation",
            "committed_at",
            "pending_restart_keys",
            "dynamic_parameters",
        ):
            with self.assertRaises(ValueError):
                DynamicParameterSpec.from_dict(_valid_dynamic_spec(key=reserved))

        # NaN / Infinity rejected in default, bounds, and gate threshold
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(_valid_dynamic_spec(default=float("nan")))
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(
                _valid_dynamic_spec(
                    screener_metric_gate={
                        "metric": "rsi14",
                        "op": ">=",
                        "threshold": float("inf"),
                        "on_missing": "reject",
                    }
                )
            )

        # String length limit (256 chars)
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(
                _valid_dynamic_spec(label="x" * (MAX_DYNAMIC_STRING_LENGTH + 1))
            )

        # Bare string or non-sequence values must not coerce into character tuples
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(_valid_dynamic_spec(gate_tags="my_tag"))
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(
                _valid_dynamic_spec(
                    type="str",
                    default="fast",
                    min=None,
                    max=None,
                    step=None,
                    allowed_values="fast",
                )
            )
        with self.assertRaises(ValueError):
            DynamicParameterSpec.from_dict(_valid_dynamic_spec(gate_tags=123))


class TestConfigSnapshot(unittest.TestCase):
    def test_snapshot_is_deeply_immutable_and_roundtrips(self) -> None:
        spec = DynamicParameterSpec.from_dict(_valid_dynamic_spec())
        snap = ConfigSnapshot(
            WATCHLIST=("AAPL", "NVDA"),
            dynamic_parameters=FrozenMapping({"CUSTOM_RSI_MIN": 40.0}),
            dynamic_parameter_specs=(spec,),
            config_epoch_id="epoch-3",
            generation=3,
        )
        with self.assertRaises(FrozenInstanceError):
            snap.MIN_ADX_TREND = 25.0  # type: ignore[misc]
        with self.assertRaises(TypeError):
            snap.dynamic_parameters["CUSTOM_RSI_MIN"] = 45.0  # type: ignore[index]
        with self.assertRaises(TypeError):
            snap.dynamic_parameters.extra = 1  # type: ignore[attr-defined]
        self.assertIsInstance(snap.WATCHLIST, tuple)
        self.assertEqual(snap.get("MIN_ADX_TREND"), 18.0)
        self.assertEqual(snap.get("CUSTOM_RSI_MIN"), 40.0)
        self.assertEqual(snap.get("config_epoch_id"), "epoch-3")
        self.assertEqual(snap.get("DOES_NOT_EXIST", "fallback"), "fallback")

        # copy.deepcopy & dataclasses.asdict must work cleanly
        copied = copy.deepcopy(snap)
        self.assertEqual(copied, snap)
        as_dict = asdict(snap)
        self.assertEqual(as_dict["MIN_ADX_TREND"], 18.0)

        roundtrip = ConfigSnapshot.from_dict(snap.to_dict())
        self.assertEqual(roundtrip, snap)


class TestConfigBusPersistenceAndPrecedence(unittest.TestCase):
    def test_boot_precedence_defaults_lt_dotenv_lt_env_lt_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "MIN_ADX_TREND=21.0",
                        "MAX_RISK_PER_TRADE=0.015",
                        "SCREENER_TOP_N=25",
                        "WATCHLIST=AAPL,MSFT",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            overrides_path = root / "config_overrides.json"
            overrides_path.write_text(
                json.dumps({"SCREENER_TOP_N": 30, "TSL_DECAY_DAY1_MULT": 2.4}),
                encoding="utf-8",
            )
            # parse_env_file helper handles quotes/comments cleanly
            parsed_env = parse_env_file(env_path)
            self.assertEqual(parsed_env["WATCHLIST"], "AAPL,MSFT")

            bus = ConfigBus(
                data_dir=root,
                env_file=env_path,
                environ={"MAX_RISK_PER_TRADE": "0.022", "MIN_ADX_TREND": "23.5"},
            )
            snap = bus.active_snapshot()
            # Default remains untouched where unset
            self.assertAlmostEqual(snap.ATR_SL_MULTIPLIER, 2.2)
            # .env beats default
            self.assertEqual(snap.WATCHLIST, ("AAPL", "MSFT"))
            # process environ beats .env
            self.assertAlmostEqual(snap.MIN_ADX_TREND, 23.5)
            self.assertAlmostEqual(snap.MAX_RISK_PER_TRADE, 0.022)
            # config_overrides.json beats environ and .env
            self.assertEqual(snap.SCREENER_TOP_N, 30)
            self.assertAlmostEqual(snap.TSL_DECAY_DAY1_MULT, 2.4)

    def test_atomic_commit_persists_across_restarts_and_reverts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = ConfigBus(data_dir=root, ignore_process_env=True)
            self.assertEqual(bus.active_epoch_id, "epoch-0")

            snap1 = bus.commit_patch(
                {"MIN_ADX_TREND": 24.0, "ETP_MAX_SL_PCT": 0.075, "WATCHLIST": ["NVDA", "AMD"]},
                expected_epoch_id="epoch-0",
                author="alice",
                reason="tighten adx",
            )
            self.assertEqual(snap1.config_epoch_id, "epoch-1")
            self.assertEqual(snap1.generation, 1)
            self.assertEqual(snap1.committed_by, "alice")
            self.assertEqual(snap1.commit_reason, "tighten adx")
            self.assertAlmostEqual(snap1.MIN_ADX_TREND, 24.0)
            self.assertEqual(snap1.WATCHLIST, ("NVDA", "AMD"))
            self.assertEqual(list(root.glob("*.tmp")), [])

            # Restart ConfigBus pointing to same files
            bus2 = ConfigBus(data_dir=root, ignore_process_env=True)
            snap2 = bus2.active_snapshot()
            self.assertEqual(snap2.config_epoch_id, "epoch-1")
            self.assertAlmostEqual(snap2.MIN_ADX_TREND, 24.0)
            self.assertAlmostEqual(snap2.ETP_MAX_SL_PCT, 0.075)
            self.assertEqual(snap2.WATCHLIST, ("NVDA", "AMD"))

            # Reverting MIN_ADX_TREND back to default prunes it from overrides file
            snap3 = bus2.commit_patch(
                {"MIN_ADX_TREND": 18.0},
                expected_epoch_id="epoch-1",
            )
            self.assertEqual(snap3.config_epoch_id, "epoch-2")
            saved_overrides = json.loads((root / "config_overrides.json").read_text(encoding="utf-8"))
            self.assertNotIn("MIN_ADX_TREND", saved_overrides)
            self.assertEqual(saved_overrides["ETP_MAX_SL_PCT"], 0.075)

            # Profile application works via CAS
            snap4 = bus2.apply_profile("conservative", expected_epoch_id="epoch-2", author="ops")
            self.assertEqual(snap4.config_epoch_id, "epoch-3")
            self.assertAlmostEqual(
                snap4.MAX_RISK_PER_TRADE,
                BUILTIN_PROFILES["conservative"]["values"]["MAX_RISK_PER_TRADE"],
            )

    def test_env_5x_leverage_parity_without_max_leverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Enabling 5x in env without MAX_LEVERAGE promotes MAX_LEVERAGE to 5 like Settings
            bus = ConfigBus(data_dir=root, environ={"ENABLE_5X_LEVERAGE": "true"})
            snap = bus.active_snapshot()
            self.assertTrue(snap.ENABLE_5X_LEVERAGE)
            self.assertEqual(snap.MAX_LEVERAGE, 5)
            preview = bus.preview_patch({"MIN_ADX_TREND": 20.0}, expected_epoch_id="epoch-0")
            self.assertTrue(preview["valid"])
            committed = bus.commit_patch({"MIN_ADX_TREND": 20.0}, expected_epoch_id="epoch-0")
            self.assertAlmostEqual(committed.MIN_ADX_TREND, 20.0)

            # Reverse parity: .env enables 5x (+prefer_5x), process env disables ENABLE_5X_LEVERAGE
            env_path = root / ".env"
            env_path.write_text(
                "ENABLE_5X_LEVERAGE=true\nPREFER_5X_LEVERAGE=true\n",
                encoding="utf-8",
            )
            bus_rev = ConfigBus(
                data_dir=root / "rev",
                env_file=env_path,
                environ={"ENABLE_5X_LEVERAGE": "false"},
            )
            snap_rev = bus_rev.active_snapshot()
            self.assertFalse(snap_rev.ENABLE_5X_LEVERAGE)
            self.assertFalse(snap_rev.PREFER_5X_LEVERAGE)
            self.assertEqual(snap_rev.MAX_LEVERAGE, 3)
            self.assertTrue(
                bus_rev.preview_patch({"MIN_ADX_TREND": 21.0}, expected_epoch_id="epoch-0")["valid"]
            )


class TestConfigBusValidationAndCAS(unittest.TestCase):
    def test_preview_patch_reports_diff_classification_and_non_simulatable_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ConfigBus(data_dir=tmp, ignore_process_env=True)
            preview = bus.preview_patch(
                {
                    "MIN_ADX_TREND": 22.0,
                    "MAX_RISK_PER_TRADE": 0.025,
                    "UNIVERSE_SIZE": 75,
                },
                expected_epoch_id="epoch-0",
            )
            self.assertTrue(preview["valid"])
            self.assertFalse(preview["conflict"])
            self.assertEqual(preview["classification"]["reload_mode"], "restart_required")
            self.assertEqual(preview["classification"]["revalidation_scope"], "abort_stale_scan")
            self.assertIn("UNIVERSE_SIZE", preview["classification"]["restart_required_keys"])
            self.assertIn("MIN_ADX_TREND", preview["classification"]["hot_reload_keys"])
            self.assertEqual(
                set(preview["non_simulatable_keys"]),
                {"MAX_RISK_PER_TRADE", "UNIVERSE_SIZE"},
            )
            self.assertEqual(preview["pending_restart_keys_after"], ["UNIVERSE_SIZE"])

            # Stale expected_epoch_id sets conflict flag in preview
            stale_preview = bus.preview_patch({"MIN_ADX_TREND": 22.0}, expected_epoch_id="epoch-99")
            self.assertTrue(stale_preview["conflict"])
            self.assertFalse(stale_preview["valid"])
            self.assertTrue(stale_preview["validation_ok"])

    def test_cas_conflict_raises_without_partial_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = ConfigBus(data_dir=root, ignore_process_env=True)
            bus.commit_patch({"MIN_ADX_TREND": 22.0}, expected_epoch_id="epoch-0")

            before_overrides = (root / "config_overrides.json").read_text(encoding="utf-8")
            before_epochs = (root / "config_epochs.jsonl").read_text(encoding="utf-8")

            with self.assertRaises(ConfigConflictError) as ctx:
                bus.commit_patch({"MIN_ADX_TREND": 30.0}, expected_epoch_id="epoch-0")

            self.assertEqual(ctx.exception.expected_epoch_id, "epoch-0")
            self.assertEqual(ctx.exception.actual_epoch_id, "epoch-1")
            self.assertIsNotNone(ctx.exception.latest_snapshot)
            self.assertEqual(ctx.exception.latest_snapshot.MIN_ADX_TREND, 22.0)

            self.assertAlmostEqual(bus.active_snapshot().MIN_ADX_TREND, 22.0)
            self.assertEqual((root / "config_overrides.json").read_text(encoding="utf-8"), before_overrides)
            self.assertEqual((root / "config_epochs.jsonl").read_text(encoding="utf-8"), before_epochs)

    def test_validation_rejects_nan_secrets_bad_types_and_cross_field_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ConfigBus(data_dir=tmp, ignore_process_env=True)

            bad_patches = [
                {"MIN_ADX_TREND": float("nan")},
                {"MIN_ADX_TREND": float("inf")},
                {"MIN_ADX_TREND": float("-inf")},
                {"MIN_ADX_TREND": True},  # bool rejected for float
                {"SCREENER_TOP_N": True},  # bool rejected for int
                {"MIN_ADX_TREND": 999.0},  # out of bounds
                {"ACTIVE_STRATEGY": "nonexistent_strategy"},
                {"TRADING212_API_KEY": "secret"},
                {"DASHBOARD_AUTH_TOKEN": "secret"},
                {"PANIC_FLATTEN_TOKEN": "secret"},
                {"USE_LIVE": True},
                {"CANDLE_DB_PATH": "/tmp/evil.db"},
                {"TRADING212_BASE_URL": "https://evil.example"},
                {"ACTIVE_TRADES_FILE": "/tmp/trades.json"},
                {"DASHBOARD_PORT": 9999},
                {"config_epoch_id": "epoch-999"},
                {"dynamic_parameters": {}},
                {"COMPLETELY_UNKNOWN_KEY": 1},
                # Cross-field violations
                {"MAX_RISK_PER_TRADE": 0.08, "MAX_POSITION_PCT": 0.05},
                {"ENABLE_5X_LEVERAGE": True, "MAX_LEVERAGE": 3},
                {"PREFER_5X_LEVERAGE": True, "ENABLE_5X_LEVERAGE": False},
                {"ETP_MIN_SL_PCT": 0.07, "ETP_MAX_SL_PCT": 0.05},
                {"MIN_SL_PCT": 0.05, "MAX_SL_PCT": 0.03},
                {"TP1_R_MULTIPLE": 3.5, "TP2_R_MULTIPLE": 3.0},
                {"TP1_TRIM_PCT": 0.50, "TP2_TRIM_PCT": 0.50, "TP3_TRIM_PCT": 0.50},
            ]

            for patch in bad_patches:
                preview = bus.preview_patch(patch, expected_epoch_id="epoch-0")
                self.assertFalse(preview["valid"], f"Expected rejection for patch: {patch}")
                self.assertTrue(preview["validation_errors"], f"Missing errors for patch: {patch}")
                with self.assertRaises(ConfigValidationError):
                    bus.commit_patch(patch, expected_epoch_id="epoch-0")

            # Epoch must remain completely untouched after rejections
            self.assertEqual(bus.active_epoch_id, "epoch-0")


class TestConfigBusDynamicParameters(unittest.TestCase):
    def test_add_override_delete_pruning_and_cap_64(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = ConfigBus(data_dir=root, ignore_process_env=True)

            # Reject missing gate, unallowlisted metric, and >256-char string via bus
            with self.assertRaises(ConfigValidationError):
                bad = _valid_dynamic_spec()
                del bad["screener_metric_gate"]
                bus.add_dynamic_parameter(bad)
            with self.assertRaises(ConfigValidationError):
                bus.add_dynamic_parameter(
                    _valid_dynamic_spec(
                        screener_metric_gate={
                            "metric": "evil_expr",
                            "op": ">",
                            "threshold": 1.0,
                            "on_missing": "reject",
                        }
                    )
                )

            # Add valid dynamic parameter
            snap1 = bus.add_dynamic_parameter(_valid_dynamic_spec("CUSTOM_RSI_MIN"))
            self.assertEqual(snap1.config_epoch_id, "epoch-1")
            self.assertEqual(snap1.dynamic_parameters["CUSTOM_RSI_MIN"], 35.0)
            self.assertEqual(snap1.get("CUSTOM_RSI_MIN"), 35.0)
            self.assertEqual(len(bus.list_dynamic_parameters()), 1)
            schema_keys = {p["key"] for p in bus.get_schema()["parameters"]}
            self.assertIn("CUSTOM_RSI_MIN", schema_keys)

            # Commit an override for the dynamic parameter + a normal setting
            snap2 = bus.commit_patch(
                {"CUSTOM_RSI_MIN": 48.0, "MIN_ADX_TREND": 21.0},
                expected_epoch_id="epoch-1",
            )
            self.assertEqual(snap2.dynamic_parameters["CUSTOM_RSI_MIN"], 48.0)

            # Restart while dynamic parameter + override are active on disk
            bus_mid = ConfigBus(data_dir=root, ignore_process_env=True)
            self.assertEqual(bus_mid.active_epoch_id, "epoch-2")
            self.assertEqual(bus_mid.active_snapshot().dynamic_parameters["CUSTOM_RSI_MIN"], 48.0)
            self.assertEqual(len(bus_mid.list_dynamic_parameters()), 1)

            # Reject invalid dynamic parameter override (out of bounds / NaN)
            with self.assertRaises(ConfigValidationError):
                bus_mid.commit_patch({"CUSTOM_RSI_MIN": 95.0}, expected_epoch_id="epoch-2")
            with self.assertRaises(ConfigValidationError):
                bus_mid.commit_patch({"CUSTOM_RSI_MIN": float("nan")}, expected_epoch_id="epoch-2")

            # Deleting dynamic parameter prunes orphaned override and advances epoch
            snap3 = bus_mid.delete_dynamic_parameter("CUSTOM_RSI_MIN", expected_epoch_id="epoch-2")
            self.assertEqual(snap3.config_epoch_id, "epoch-3")
            self.assertNotIn("CUSTOM_RSI_MIN", snap3.dynamic_parameters)
            saved_overrides = json.loads((root / "config_overrides.json").read_text(encoding="utf-8"))
            self.assertNotIn("CUSTOM_RSI_MIN", saved_overrides)
            self.assertEqual(saved_overrides["MIN_ADX_TREND"], 21.0)

            # Restart cleanly preserves deletion and remaining override
            bus_restarted = ConfigBus(data_dir=root, ignore_process_env=True)
            self.assertNotIn("CUSTOM_RSI_MIN", bus_restarted.active_snapshot().dynamic_parameters)
            self.assertAlmostEqual(bus_restarted.active_snapshot().MIN_ADX_TREND, 21.0)

            # Enforce max 64 custom parameters + 256-char dynamic string override limit
            bus_restarted.add_dynamic_parameter(
                _valid_dynamic_spec(
                    key="CUSTOM_TAG_FILTER",
                    type="str",
                    default="ok",
                    min=None,
                    max=None,
                    step=None,
                )
            )
            with self.assertRaises(ConfigValidationError):
                bus_restarted.commit_patch(
                    {"CUSTOM_TAG_FILTER": "x" * (MAX_DYNAMIC_STRING_LENGTH + 1)},
                    expected_epoch_id=bus_restarted.active_epoch_id,
                )
            for idx in range(1, MAX_DYNAMIC_PARAMETERS):
                bus_restarted.add_dynamic_parameter(_valid_dynamic_spec(key=f"DYN_CAP_{idx:02d}"))
            self.assertEqual(len(bus_restarted.list_dynamic_parameters()), MAX_DYNAMIC_PARAMETERS)
            with self.assertRaises(ConfigValidationError):
                bus_restarted.add_dynamic_parameter(_valid_dynamic_spec(key="DYN_CAP_OVERFLOW"))


class TestConfigBusResilienceAndEpochs(unittest.TestCase):
    def test_corrupt_overrides_and_dynamic_params_quarantined_without_crash(self) -> None:
        bad_override_payloads = [
            "{not valid json",
            '{"MIN_ADX_TREND": NaN}',
            '{"TRADING212_API_KEY": "sneaky"}',
            '{"UNKNOWN_SETTING": 42}',
            '{"MAX_RISK_PER_TRADE": 0.15, "MAX_POSITION_PCT": 0.05}',
        ]
        for payload in bad_override_payloads:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config_overrides.json").write_text(payload, encoding="utf-8")
                bus = ConfigBus(data_dir=root, ignore_process_env=True)
                snap = bus.active_snapshot()
                self.assertEqual(snap.MIN_ADX_TREND, 18.0)
                self.assertGreaterEqual(len(snap.config_quarantine_warnings), 1)
                self.assertFalse((root / "config_overrides.json").exists())
                self.assertEqual(len(list(root.glob("config_overrides.json.corrupt.*"))), 1)

                # Persistent quarantine warnings remain visible across commits in this process
                snap_after = bus.commit_patch({"MIN_ADX_TREND": 20.0}, expected_epoch_id=snap.config_epoch_id)
                self.assertEqual(snap_after.config_quarantine_warnings, snap.config_quarantine_warnings)

                # Next clean restart has empty quarantine warnings
                bus_clean = ConfigBus(data_dir=root, ignore_process_env=True)
                self.assertEqual(bus_clean.get_quarantine_warnings(), ())

        # Also test corrupt dynamic_parameters.json quarantine
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dynamic_parameters.json").write_text(
                json.dumps([{"key": "MISSING_GATE", "label": "Bad", "type": "float", "default": 1.0}]),
                encoding="utf-8",
            )
            bus = ConfigBus(data_dir=root, ignore_process_env=True)
            self.assertEqual(bus.list_dynamic_parameters(), ())
            self.assertGreaterEqual(len(bus.get_quarantine_warnings()), 1)
            self.assertFalse((root / "dynamic_parameters.json").exists())
            self.assertEqual(len(list(root.glob("dynamic_parameters.json.corrupt.*"))), 1)

    def test_pending_restart_keys_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = ConfigBus(data_dir=root, ignore_process_env=True)
            self.assertEqual(bus.active_snapshot().pending_restart_keys, ())

            # Hot-reload key does not add pending_restart_keys
            snap1 = bus.commit_patch({"MIN_ADX_TREND": 25.0}, expected_epoch_id="epoch-0")
            self.assertEqual(snap1.pending_restart_keys, ())

            # restart_required key adds to pending_restart_keys
            snap2 = bus.commit_patch({"UNIVERSE_SIZE": 80, "LLM_DEEP_MODEL": "gpt-4.1"}, expected_epoch_id="epoch-1")
            self.assertEqual(snap2.pending_restart_keys, ("LLM_DEEP_MODEL", "UNIVERSE_SIZE"))

            # Reverting one removes it from pending_restart_keys
            snap3 = bus.commit_patch({"LLM_DEEP_MODEL": "gpt-4o"}, expected_epoch_id="epoch-2")
            self.assertEqual(snap3.pending_restart_keys, ("UNIVERSE_SIZE",))

            # Fresh process restart clears pending_restart_keys while keeping committed override active
            bus_restarted = ConfigBus(data_dir=root, ignore_process_env=True)
            restarted_snap = bus_restarted.active_snapshot()
            self.assertEqual(restarted_snap.UNIVERSE_SIZE, 80)
            self.assertEqual(restarted_snap.pending_restart_keys, ())

    def test_bounded_epoch_retention_and_historical_reload_from_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = ConfigBus(data_dir=root, max_epoch_history=5, ignore_process_env=True)
            for idx in range(1, 9):
                bus.commit_patch(
                    {"MIN_ADX_TREND": 18.0 + idx},
                    expected_epoch_id=bus.active_epoch_id,
                    reason=f"step-{idx}",
                )

            lines = [
                ln
                for ln in (root / "config_epochs.jsonl").read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            self.assertEqual(len(lines), 5)

            # Force reload from JSONL by clearing in-memory epoch cache
            with bus._lock:
                bus._epoch_cache.clear()
            hist_snap = bus.snapshot_for_epoch("epoch-5")
            self.assertIsNotNone(hist_snap)
            self.assertAlmostEqual(hist_snap.MIN_ADX_TREND, 23.0)
            self.assertEqual(hist_snap.commit_reason, "step-5")

            # Pruned older epoch returns None
            self.assertIsNone(bus.snapshot_for_epoch("epoch-1"))

            # New ConfigBus restart continues epoch numbers monotonically and reads history
            bus2 = ConfigBus(data_dir=root, max_epoch_history=5, ignore_process_env=True)
            self.assertEqual(bus2.active_epoch_id, "epoch-8")
            self.assertAlmostEqual(bus2.snapshot_for_epoch("epoch-6").MIN_ADX_TREND, 24.0)  # type: ignore[union-attr]
            self.assertEqual(len(bus2.epoch_history(limit=10)), 5)

    def test_concurrent_readers_and_cas_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = ConfigBus(data_dir=tmp, ignore_process_env=True)
            errors: list[Exception] = []
            successful_commits = 0
            counter_lock = threading.Lock()

            def writer(worker_id: int) -> None:
                nonlocal successful_commits
                try:
                    for step in range(5):
                        while True:
                            snap = bus.active_snapshot()
                            try:
                                bus.commit_patch(
                                    {"SCREENER_TOP_N": 10 + ((worker_id * 5 + step) % 40)},
                                    expected_epoch_id=snap.config_epoch_id,
                                    author=f"w{worker_id}",
                                )
                                with counter_lock:
                                    successful_commits += 1
                                break
                            except ConfigConflictError:
                                continue
                except Exception as exc:
                    errors.append(exc)

            def reader() -> None:
                try:
                    last_gen = -1
                    for _ in range(40):
                        snap = bus.active_snapshot()
                        self.assertGreaterEqual(snap.generation, last_gen)
                        last_gen = snap.generation
                        self.assertGreaterEqual(snap.SCREENER_TOP_N, 1)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            threads.extend(threading.Thread(target=reader) for _ in range(4))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(successful_commits, 20)
            self.assertEqual(bus.active_snapshot().generation, 20)
            self.assertEqual(bus.active_epoch_id, "epoch-20")


if __name__ == "__main__":
    unittest.main()
