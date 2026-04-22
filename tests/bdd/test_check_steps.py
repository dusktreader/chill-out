"""Bind the check.feature scenarios.

All step definitions are shared and live in conftest.py.
"""

from __future__ import annotations

import pytest
from pytest_bdd import scenarios

scenarios("features/check.feature")

pytestmark = pytest.mark.bdd
