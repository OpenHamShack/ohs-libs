import json
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "shack_pad" / "examples"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


@pytest.fixture
def nb():
    return example("qo100nb-pad-actions.json"), example("qo100nb-pad-layout.json")


@pytest.fixture
def wb():
    return example("qo100wb-pad-actions.json"), example("qo100wb-pad-layout.json")
