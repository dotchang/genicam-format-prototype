import pytest

from gcr.apply import apply_settings, unit_factor
from gcr.models import Conversion, Profile, ProfileMatch, Setting
from gcr.nodemap import FakeNodeMap
from gcr.profile import Resolver, match_profile


def run(spec, settings, profile=None):
    nm = FakeNodeMap(spec)
    report = apply_settings(nm, [Setting(**s) for s in settings], Resolver(profile))
    return nm, report


def by_key(report):
    return {r.key: r for r in report.records}


def test_order_and_values(spec):
    nm, rep = run(spec, [
        {"node": "ExposureAuto", "value": "Off"},
        {"node": "ExposureTime", "value": 10, "unit": "ms", "tolerance": "1%"},
    ])
    assert rep.ok
    assert [w[0] for w in nm.writes] == ["ExposureAuto", "ExposureTime"]
    assert nm.get("ExposureTime") == 9999.0  # quantised to 3 us steps
    assert by_key(rep)["ExposureTime"].readback == pytest.approx(9.999)


def test_default_float_tolerance_flags_quantisation(spec):
    spec["ExposureTime"]["quantize"] = lambda v: round(v / 100.0) * 100.0
    _, rep = run(spec, [{"node": "ExposureTime", "value": 1050, "unit": "us"}])
    assert by_key(rep)["ExposureTime"].status == "failed"
    _, rep = run(spec, [{"node": "ExposureTime", "value": 1050, "unit": "us", "level": "preferred"}])
    assert by_key(rep)["ExposureTime"].status == "approx"


def test_enum_candidates(spec):
    nm, rep = run(spec, [{"node": "PixelFormat", "value": ["Mono12", "Mono12p", "Mono8"]}])
    r = by_key(rep)["PixelFormat"]
    assert r.status == "ok" and r.applied == "Mono12p" and "candidate" in r.message
    _, rep = run(spec, [{"node": "PixelFormat", "value": ["Mono16"]}])
    assert by_key(rep)["PixelFormat"].status == "failed"
    assert not rep.ok


def test_special_values_and_alignment(spec):
    nm, rep = run(spec, [
        {"node": "Width", "value": "@max"},
        {"node": "OffsetX", "value": "@center"},
        {"node": "Height", "value": 481},
    ])
    assert nm.get("Width") == 1920 and nm.get("OffsetX") == 640
    h = by_key(rep)["Height"]
    assert h.applied in (480, 482) and h.status == "ok"  # within one increment


def test_missing_nodes_by_level(spec):
    _, rep = run(spec, [
        {"node": "Gamma", "value": 1.0, "level": "optional"},
        {"node": "BalanceRatio", "value": 1.0, "level": "preferred"},
    ])
    assert rep.ok and [r.status for r in rep.records] == ["skipped", "skipped"]
    _, rep = run(spec, [{"node": "Gamma", "value": 1.0}])
    assert not rep.ok


def test_selector(spec):
    nm, rep = run(spec, [{"node": "Gain", "selector": {"GainSelector": "Red"}, "value": 3.0, "unit": "dB"}])
    assert rep.ok and by_key(rep)["Gain[GainSelector=Red]"].readback == 3.0
    nm.set("GainSelector", "All")
    assert nm.get("Gain") == 1.0


def test_clamp(spec):
    _, rep = run(spec, [{"node": "Gain", "value": 40.0, "level": "preferred"}])
    r = by_key(rep)["Gain"]
    assert r.applied == 24 and r.status == "approx" and "clamped" in r.message


def test_read_only_already_correct(spec):
    _, rep = run(spec, [{"node": "SensorWidth", "value": 1920}, {"node": "DeviceModelName", "value": "FakeCam"}])
    assert rep.ok, rep.records


def test_execute(spec):
    nm, rep = run(spec, [{"node": "TriggerSoftware", "execute": True}])
    assert rep.ok and nm.executed == ["TriggerSoftware"]


def test_profile_alias_and_conversion():
    spec = {
        "ExposureTimeAbs": {"kind": "float", "value": 100.0, "min": 10.0, "max": 1e6},
        "GainRaw": {"kind": "int", "value": 0, "min": 0, "max": 500, "inc": 1},
    }
    prof = Profile(name="legacy", match=ProfileMatch(vendor="Acme"), aliases={"ExposureTime": "ExposureTimeAbs"},
                   conversions={"Gain": Conversion(node="GainRaw", scale=0.0359, unit="dB")})
    nm, rep = run(spec, [
        {"node": "ExposureTime", "value": 2, "unit": "ms"},
        {"node": "Gain", "value": 6.0, "unit": "dB", "tolerance": 0.05},
    ], prof)
    assert rep.ok, rep.records
    assert nm.get("ExposureTimeAbs") == 2000.0  # SFNC default unit us
    assert nm.get("GainRaw") == 167
    assert by_key(rep)["Gain"].node == "GainRaw"


def test_profile_matching():
    generic = Profile(name="g", match=ProfileMatch(vendor="Basler", model="*"))
    specific = Profile(name="s", match=ProfileMatch(vendor="basler", model="acA1920-*"))
    assert match_profile([generic, specific], "Basler", "acA1920-40gm").name == "s"
    assert match_profile([generic, specific], "Basler", "a2A").name == "g"
    assert match_profile([generic], "FLIR", "x") is None


def test_unit_factor():
    assert unit_factor("ms", "us") == pytest.approx(1000)
    assert unit_factor("kHz", "Hz") == pytest.approx(1000)
    assert unit_factor("ms", "Hz") is None
    assert unit_factor("dB", "dB") == 1.0
