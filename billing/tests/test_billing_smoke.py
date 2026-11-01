"""Smoke test: the package is installed from the workspace and importable."""

import tollgate_billing


def test_package_is_installed() -> None:
    assert isinstance(tollgate_billing.__version__, str)
    assert tollgate_billing.__version__
