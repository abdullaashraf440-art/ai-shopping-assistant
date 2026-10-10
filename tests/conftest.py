import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A fresh, seeded database in a temp dir for every test."""
    import config
    import db
    import setup_db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    setup_db.create_database()
    return db
