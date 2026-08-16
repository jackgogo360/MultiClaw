import importlib
import importlib.util


def test_storage_public_surface_exports_scoped_database_interfaces_only() -> None:
    storage = importlib.import_module("multiclaw.storage")

    assert storage.Database.__name__ == "Database"
    assert storage.MySQLDialect.__name__ == "MySQLDialect"
    assert storage.SQLiteDialect.__name__ == "SQLiteDialect"
    assert storage.AuthUnitOfWork.__name__ == "AuthUnitOfWork"
    assert storage.TenantUnitOfWork.__name__ == "TenantUnitOfWork"
    assert storage.DeletionUnitOfWork.__name__ == "DeletionUnitOfWork"

    assert not hasattr(storage, "Repository")
    assert not hasattr(storage, "SqliteRepository")
    assert not hasattr(storage, "SqliteConfig")


def test_storage_public_surface_all_excludes_legacy_names() -> None:
    storage = importlib.import_module("multiclaw.storage")

    assert sorted(storage.__all__) == [
        "AuthUnitOfWork",
        "Database",
        "DeletionUnitOfWork",
        "MySQLDialect",
        "SQLiteDialect",
        "TenantUnitOfWork",
    ]


def test_legacy_storage_modules_are_absent() -> None:
    assert importlib.util.find_spec("multiclaw.storage.repository") is None
    assert importlib.util.find_spec("multiclaw.storage.sqlite") is None
    assert importlib.util.find_spec("multiclaw.sqlite_utils") is None
