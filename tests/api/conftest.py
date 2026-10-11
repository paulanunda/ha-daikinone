"""Fixtures for the standalone client tests."""

from __future__ import annotations

import pytest

from custom_components.daikinone.api import DaikinOneClient
from tests.fake_daikin import API_KEY, EMAIL, INTEGRATOR_TOKEN, FakeDaikin


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def daikin() -> FakeDaikin:
    return FakeDaikin()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(daikin: FakeDaikin, clock: FakeClock) -> DaikinOneClient:
    return DaikinOneClient(
        daikin.session(),
        email=EMAIL,
        api_key=API_KEY,
        integrator_token=INTEGRATOR_TOKEN,
        monotonic=clock,
    )
