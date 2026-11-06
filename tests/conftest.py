import json
from functools import lru_cache
from pathlib import Path

import pytest

import seismic_response as sr

DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=None)
def load_fixture(relative: str):
    """Parse (and cache) a fixture from tests/data."""
    return sr.read_response(DATA / relative)


@pytest.fixture(scope="session")
def golden():
    return json.loads((DATA / "golden" / "golden.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def anmo():
    """The two ANMO BHZ epochs (a real response: poles/zeros + FIR stages)."""
    return load_fixture("real/RESP.IU.ANMO.00.BHZ")
