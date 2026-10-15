from qsft.probe.commands import IDENTIFY_ANGULAR_RAD_S, config_role, moving_serials


def test_only_shaken_trackers_count_fastest_first():
    peaks = {"LHR-REST": 0.2, "LHR-WORN": 1.5, "LHR-SHAKE": 9.0, "LHR-TWIST": IDENTIFY_ANGULAR_RAD_S + 0.5}
    assert moving_serials(peaks) == ["LHR-SHAKE", "LHR-TWIST"]
    assert moving_serials({}) == []


def test_config_roles():
    cfg = {"trackers": ["LHR-A"], "calibration_tracker": "LHR-C"}
    assert config_role("LHR-A", cfg) == "Person B tracker"
    assert config_role("LHR-C", cfg) == "calibration tracker"
    assert config_role("LHR-Z", cfg) == "not in config"
    assert config_role("LHR-A", {}) == "not in config"
