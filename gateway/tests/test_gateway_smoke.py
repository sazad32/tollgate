"""Smoke test: the package is installed from the workspace and importable."""

import tollgate_gateway


def test_package_is_installed() -> None:
    assert isinstance(tollgate_gateway.__version__, str)
    assert tollgate_gateway.__version__
