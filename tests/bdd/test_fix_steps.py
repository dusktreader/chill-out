"""Bind the fix.feature scenarios.

All step definitions are shared and live in conftest.py.
"""

from __future__ import annotations

import pytest
from pytest_bdd import scenarios

scenarios("features/fix.feature")

pytestmark = pytest.mark.bdd
