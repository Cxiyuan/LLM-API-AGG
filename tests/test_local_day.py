from catalogwatch.util import local_day


def test_local_day_uses_the_beijing_calendar():
    # UTC 16:00 is Beijing midnight; daily quotas must follow Beijing, not UTC.
    assert local_day("2026-11-01T15:59:59+00:00") == "2026-11-01"
    assert local_day("2026-11-01T16:00:00+00:00") == "2026-11-02"
    assert local_day("2026-11-02T00:00:00Z") == "2026-11-02"
