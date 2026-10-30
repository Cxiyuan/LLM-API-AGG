from __future__ import annotations

import pytest

from catalogwatch.util import html_to_text, parse_since, text_diff


def test_html_to_text_strips_scripts_and_collapses_duplicates():
    html = """
    <html><body>
      <script>var secret = "1";</script>
      <style>.x { color: red; }</style>
      <h1>Rate limits</h1>
      <p>30 requests per minute</p>
      <p>30 requests per minute</p>
      <p>1,000 requests per day</p>
    </body></html>
    """
    text = html_to_text(html)
    assert "secret" not in text
    assert "color: red" not in text
    assert text.splitlines() == ["Rate limits", "30 requests per minute", "1,000 requests per day"]


def test_html_to_text_selector():
    html = '<html><body><nav>menu</nav><main><p>quota</p></main></body></html>'
    assert html_to_text(html, "main") == "quota"
    with pytest.raises(ValueError):
        html_to_text(html, "#missing")


def test_text_diff_counts_changed_lines():
    old = "30 RPM\n1000 RPD\n"
    new = "60 RPM\n1000 RPD\n50 TPM\n"
    diff, added, removed = text_diff(old, new)
    assert "+60 RPM" in diff
    assert "+50 TPM" in diff
    assert "-30 RPM" in diff
    assert added == 2
    assert removed == 1


def test_parse_since_relative_and_iso():
    assert parse_since("24h").endswith("+00:00")
    assert parse_since("2026-01-01T00:00:00Z").startswith("2026-01-01T00:00:00")
    with pytest.raises(ValueError):
        parse_since("not-a-time")
