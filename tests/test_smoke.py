"""Bootstrap smoke test — confirms the `amg` package is importable."""


def test_package_imports() -> None:
    import amg

    assert amg is not None
