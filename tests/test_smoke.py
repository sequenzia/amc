"""Bootstrap smoke test — confirms the `amc` package is importable."""


def test_package_imports() -> None:
    import amc

    assert amc is not None
