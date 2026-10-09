"""Headshot kural: kemik adi 'Bip01_Head' gibi onekli gelir."""
from __future__ import annotations

import pytest

from sqreader.squad.snapshot import is_head_bone


@pytest.mark.parametrize("bone,expected", [
    ("Bip01_Head", True),
    ("head", True),
    ("Head", True),
    ("Bip01_Spine2", False),
    ("Bip01_Neck", False),
    ("Bip01_R_UpperArm", False),
    ("", False),
    (None, False),
])
def test_is_head_bone(bone, expected):
    assert is_head_bone(bone) is expected
