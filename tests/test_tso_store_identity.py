"""Regression tests for final-config TSO store identity."""

from __future__ import annotations

import configparser
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from generate_fs_effective_target import tso_store_pipeline  # noqa: E402
from validate_fs_oracle_identity import (  # noqa: E402
    tso_store_identity_mismatches,
)


CORES = ("board.processor.switch0.core", "board.processor.switch1.core")


def config(
    *,
    mode: str | None = None,
    buffer_size: int | None = None,
    needs_tso: bool = True,
) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None)
    ini.optionxform = str
    for section in CORES:
        ini.add_section(section)
        ini.set(section, "needsTSO", str(needs_tso).lower())
        if mode is not None:
            ini.set(section, "tsoStore", mode)
        if buffer_size is not None:
            ini.set(section, "tsoStoreBufferSize", str(buffer_size))
    return ini


def buffer_documents(size: int = 114):
    target = {
        "SQEntries": size,
        "tsoStore": "buffer",
        "tsoStoreBufferSize": size,
        "tsoStoreBufferActive": True,
        "needsTSO": True,
    }
    profile = {
        "tso_store": "buffer",
        "tso_store_buffer_size": size,
        "tso_store_buffer_active": True,
        "sq_entries": size,
    }
    request = {
        "sampling": {
            "tso_store": "buffer",
            "tso_store_buffer_size": size,
            "tso_store_buffer_active": True,
        }
    }
    return target, profile, request


class TSOStoreEffectiveTargetTests(unittest.TestCase):
    def test_legacy_config_defaults_to_inactive_serial(self):
        self.assertEqual(
            tso_store_pipeline(config(), CORES, 32),
            {
                "tsoStore": "serial",
                "tsoStoreBufferSize": 114,
                "tsoStoreBufferActive": False,
                "needsTSO": True,
            },
        )

    def test_serial_buffer_size_does_not_change_effective_sq(self):
        pipeline = tso_store_pipeline(
            config(mode="serial", buffer_size=7), CORES, 32
        )
        self.assertEqual(pipeline["tsoStore"], "serial")
        self.assertEqual(pipeline["tsoStoreBufferSize"], 7)
        self.assertFalse(pipeline["tsoStoreBufferActive"])

    def test_buffer_requires_tso_and_matching_sq_capacity(self):
        expected = tso_store_pipeline(
            config(mode="buffer", buffer_size=114), CORES, 114
        )
        self.assertTrue(expected["tsoStoreBufferActive"])
        with self.assertRaisesRegex(ValueError, "needsTSO=true"):
            tso_store_pipeline(
                config(mode="buffer", buffer_size=114, needs_tso=False),
                CORES,
                114,
            )
        with self.assertRaisesRegex(ValueError, "SQEntries to match"):
            tso_store_pipeline(
                config(mode="buffer", buffer_size=114), CORES, 32
            )


class TSOStoreIdentityGateTests(unittest.TestCase):
    def test_legacy_serial_identity_remains_compatible(self):
        self.assertEqual(
            tso_store_identity_mismatches(
                {"SQEntries": 32, "needsTSO": True},
                {},
                {},
            ),
            [],
        )

    def test_complete_buffer_identity_passes(self):
        target, profile, request = buffer_documents()
        self.assertEqual(
            tso_store_identity_mismatches(target, profile, request), []
        )

    def test_buffer_identity_fails_closed(self):
        target, profile, request = buffer_documents()
        target["SQEntries"] = 32
        target["needsTSO"] = False
        profile["tso_store_buffer_active"] = False
        request["sampling"].pop("tso_store_buffer_active")
        fields = {
            mismatch["field"]
            for mismatch in tso_store_identity_mismatches(
                target, profile, request
            )
        }
        self.assertIn("core.pipeline.needsTSO", fields)
        self.assertIn("core.pipeline.SQEntries", fields)
        self.assertIn("core.tso_store_buffer_active", fields)
        self.assertIn("core.sq_entries", fields)
        self.assertIn("request.tso_store_buffer_active", fields)

    def test_request_cannot_relabel_serial_result_as_buffer(self):
        target, profile, request = buffer_documents()
        target.update(
            {
                "SQEntries": 32,
                "tsoStore": "serial",
                "tsoStoreBufferActive": False,
            }
        )
        profile.update(
            {
                "tso_store": "serial",
                "tso_store_buffer_active": False,
                "sq_entries": 32,
            }
        )
        fields = {
            mismatch["field"]
            for mismatch in tso_store_identity_mismatches(
                target, profile, request
            )
        }
        self.assertIn("request.tso_store", fields)


if __name__ == "__main__":
    unittest.main()
