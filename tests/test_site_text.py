"""What the site says about itself has to match the code and the data."""

import re
from pathlib import Path

from wastewater.sources import SOURCES

ROOT = Path(__file__).resolve().parent.parent


def test_the_sites_country_names_match_the_sources():
    """The track record names a country that has dropped out of today's data from its own list."""
    text = (ROOT / "site/js/track.js").read_text()
    block = re.search(r"COUNTRY_NAMES = \{(.*?)\};", text, re.S).group(1)
    names = dict(re.findall(r'"?([a-z-]+)"?: "([^"]+)"', block))
    assert names == {cid: cls.info.name for cid, cls in SOURCES.items()}


def test_the_back_test_note_says_it_is_a_best_case():
    """The ranges, the fallback and the inputs were tuned on the back-test weeks, so the note under
    the back-test table mustn't say the model never saw them."""
    app = (ROOT / "site/js/app.js").read_text()
    assert "never saw" not in app
    assert "were tuned on them, so read this as a best case" in app
    assert "which inputs it uses were tuned on them" in (ROOT / "site/index.html").read_text()
