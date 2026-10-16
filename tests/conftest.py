from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def sfnc_camera_spec():
    """A small SFNC-compliant fake camera."""
    return {
        "PixelFormat": {"kind": "enum", "value": "Mono8", "symbolics": ["Mono8", "Mono12p", "RGB8"]},
        "Width": {"kind": "int", "value": 640, "min": 16, "max": 1920, "inc": 16},
        "Height": {"kind": "int", "value": 480, "min": 16, "max": 1200, "inc": 2},
        "OffsetX": {"kind": "int", "value": 0, "min": 0, "max": 1280, "inc": 16},
        "ExposureAuto": {"kind": "enum", "value": "Continuous", "symbolics": ["Off", "Continuous"]},
        "ExposureTime": {"kind": "float", "value": 5000.0, "min": 20.0, "max": 1e6, "unit": "us",
                         "quantize": lambda v: round(v / 3.0) * 3.0},
        "GainSelector": {"kind": "enum", "value": "All", "symbolics": ["All", "Red"],
                         "selected": ["Gain"]},
        "Gain": {"kind": "float", "value": 1.0, "min": 0.0, "max": 24.0, "unit": "dB", "per": "GainSelector"},
        "DeviceModelName": {"kind": "str", "value": "FakeCam", "access": "RO"},
        "SensorWidth": {"kind": "int", "value": 1920, "access": "RO", "min": 1920, "max": 1920},
        "TriggerSelector": {"kind": "enum", "value": "FrameStart", "symbolics": ["FrameStart"]},
        "TriggerMode": {"kind": "enum", "value": "Off", "symbolics": ["Off", "On"]},
        "TriggerSource": {"kind": "enum", "value": "Line1", "symbolics": ["Line1", "Software"]},
        "TriggerSoftware": {"kind": "cmd", "value": None, "access": "WO"},
        "AcquisitionMode": {"kind": "enum", "value": "Continuous", "symbolics": ["Continuous"]},
        "ChunkModeActive": {"kind": "bool", "value": False},
        "ChunkSelector": {"kind": "enum", "value": "Timestamp", "symbolics": ["Timestamp"],
                          "selected": ["ChunkEnable"]},
        "ChunkEnable": {"kind": "bool", "value": False, "per": "ChunkSelector"},
        "ChunkTimestamp": {"kind": "int", "value": 123, "access": "RO"},
    }


@pytest.fixture
def spec():
    return sfnc_camera_spec()
