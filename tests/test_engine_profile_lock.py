"""A killed container leaves Chromium's lock files behind; the next start must
clear them or the browser refuses to open the profile."""

import os

from app import engine as engine_module
from app.engine import Engine


def test_stale_singleton_files_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "PROFILE_DIR", tmp_path)
    (tmp_path / "SingletonCookie").write_text("x")
    # Chromium writes SingletonLock as a dangling symlink to "host-pid".
    os.symlink("0795c45eb131-861", tmp_path / "SingletonLock")
    (tmp_path / "Preferences").write_text("{}")   # real profile data stays

    Engine._clear_stale_profile_lock()

    assert not (tmp_path / "SingletonLock").is_symlink()
    assert not (tmp_path / "SingletonCookie").exists()
    assert (tmp_path / "Preferences").exists()
