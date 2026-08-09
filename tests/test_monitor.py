from beacon_monitor import DriftTracker


def test_drift_tracker_only_compares_carrier_measurements():
    tracker = DriftTracker()

    assert tracker.update("CARRIER", 100_000) is None
    assert tracker.update("Q65", 150_000) is None
    assert tracker.update("CW", 125_000) is None
    assert tracker.update("CARRIER", 100_125) == 125
