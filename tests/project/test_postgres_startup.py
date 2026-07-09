import pytest

from project.config.settings import PlatformSettings
from project.database.postgres import PostgresUnavailableError, parse_postgres_connection_info, sanitize_postgres_error
from project.main import RAILWAY_LIGHT_MISSING_DATABASE_URL, build_postgres


def test_parse_postgres_connection_info_hides_credentials():
    info = parse_postgres_connection_info("postgresql://user:secret@example.railway.internal:5432/railway")

    assert info.host == "example.railway.internal"
    assert info.port == 5432
    assert info.database == "railway"
    assert "secret" not in info.display()
    assert "user" not in info.display()


def test_railway_light_requires_database_url():
    settings = PlatformSettings(run_mode="RAILWAY_LIGHT", database_url="")

    with pytest.raises(PostgresUnavailableError, match=RAILWAY_LIGHT_MISSING_DATABASE_URL):
        build_postgres(settings)


def test_sanitize_postgres_error_masks_password():
    database_url = "postgresql://user:secret@example.railway.internal:5432/railway"
    message = f"could not connect using {database_url}; password secret rejected"

    sanitized = sanitize_postgres_error(message, database_url)

    assert "secret" not in sanitized
    assert "user:***@example.railway.internal:5432" in sanitized
