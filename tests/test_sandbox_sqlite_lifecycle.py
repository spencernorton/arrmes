"""Owned fixture databases must close before archive, hash, reset or deletion.

Keep strong references to each real connection to exercise resource ownership
without relying on garbage collection or the host OS's open-file unlink rules.
"""

from contextlib import closing
import shutil
import sqlite3

import pytest

from arrmes.sandbox import NetworkIsolationGuard
from arrmes.sandbox import sanitize


@pytest.fixture
def retained_connections(monkeypatch):
    real_connect = sqlite3.connect
    opened = []
    fault = {"statement": None}

    class RetainedConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == fault["statement"]:
                raise RuntimeError("injected fixture database failure")
            return super().execute(statement, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = real_connect(*args, factory=RetainedConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sanitize.sqlite3, "connect", connect)
    yield opened, fault, real_connect
    for connection in opened:
        connection.close()


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_build_sanitize_and_remove_close_all_owned_connections(tmp_path, retained_connections):
    opened, _, real_connect = retained_connections
    raw, restored = tmp_path / "raw", tmp_path / "restored"
    with NetworkIsolationGuard(allowed_ports=set()):
        sanitize.build_raw_offline_backup_fixture(raw)
        assert_closed(opened)
        assert not (raw / "_staging").exists()
        receipt = sanitize.sanitize_and_restore(raw, restored)
    assert_closed(opened)
    assert receipt["zero_residual_secrets_verified"] is True
    with closing(real_connect(restored / "sonarr/sonarr.db")) as check:
        assert check.execute("SELECT COUNT(*) FROM Series").fetchone()[0] > 0
        assert check.execute("SELECT COUNT(*) FROM Indexers").fetchone()[0] == 0
        assert check.execute("SELECT Value FROM Config WHERE Key='analyticsenabled'").fetchone()[0] == "False"
    # Windows rejects these removals if any retained connection remains open.
    shutil.rmtree(raw)
    shutil.rmtree(restored)


def test_builder_failure_closes_connection_and_rolls_back_pending_rows(tmp_path, retained_connections):
    opened, _, real_connect = retained_connections

    class FailingEpisodes(sqlite3.Connection):
        def executemany(self, statement, parameters):
            if statement.startswith("INSERT INTO Episodes"):
                super().executemany(statement, list(parameters)[:1])
                raise RuntimeError("injected episode insert failure")
            return super().executemany(statement, parameters)

    # Use a real transaction and retain the connection across the raised error.
    connection = real_connect(tmp_path / "sonarr.db", factory=FailingEpisodes)
    opened.append(connection)
    from unittest.mock import patch
    with patch.object(sanitize.sqlite3, "connect", return_value=connection):
        with pytest.raises(RuntimeError, match="episode insert"):
            sanitize._build_sonarr_db(tmp_path / "sonarr.db")
    assert_closed(opened)
    with closing(real_connect(tmp_path / "sonarr.db")) as check:
        assert check.execute("SELECT COUNT(*) FROM Series").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM Episodes").fetchone()[0] == 0


def test_sanitizer_failure_closes_connection_and_preserves_rollback(tmp_path, retained_connections):
    opened, fault, real_connect = retained_connections
    raw, restored = tmp_path / "raw", tmp_path / "restored"
    sanitize.build_raw_offline_backup_fixture(raw)
    fault["statement"] = "UPDATE RootFolders SET Path = REPLACE(Path, '/legacy_storage/media', '/sandbox/media')"
    with pytest.raises(RuntimeError, match="fixture database failure"):
        sanitize.sanitize_and_restore(raw, restored)
    assert_closed(opened)
    with closing(real_connect(restored / "sonarr/sonarr.db")) as check:
        assert check.execute("SELECT COUNT(*) FROM Indexers").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM DownloadClients").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM Config").fetchone()[0] == 2
    shutil.rmtree(raw)
    shutil.rmtree(restored)
