"""Every log in one folder (~/Downloads/Logs by default), a sub-folder per day, deleted after 30 days."""
from types import SimpleNamespace

import pytest

from argus import config, logkeeper


def keeper(tmp_path, now):
    src = tmp_path / "argus-logs"
    src.mkdir(exist_ok=True)
    return src, logkeeper.LogKeeper(src, tmp_path / "Downloads" / "Logs", 30, clock=lambda: now[0])


def test_new_lines_go_to_todays_folder_once(tmp_path):
    now = [1_762_900_000.0]                                        # 2025-11-11, a fixed day
    src, k = keeper(tmp_path, now)
    (src / "audit.jsonl").write_text('{"event": "started"}\n')
    (src / "voice.err.log").write_text("Argus is already running.\n")
    day = k.sync()
    assert (day / "audit.jsonl").read_text() == '{"event": "started"}\n'
    assert (day / "voice.err.log").read_text() == "Argus is already running.\n"
    with open(src / "audit.jsonl", "a") as fh:
        fh.write('{"event": "turn"}\n')
    k.sync()
    assert (day / "audit.jsonl").read_text() == '{"event": "started"}\n{"event": "turn"}\n'   # nothing twice
    (src / "argus.log").write_text("big\n")
    k.sync()
    (src / "argus.log").write_text("x\n")                          # rotated: starts again from the top
    k.sync()
    assert (day / "argus.log").read_text() == "big\nx\n"
    assert oct(day.stat().st_mode & 0o777) == "0o700" and oct((day / "audit.jsonl").stat().st_mode & 0o777) == "0o600"
    assert oct(k.folder.stat().st_mode & 0o777) == "0o700"


def test_days_older_than_30_are_deleted_and_nothing_else(tmp_path):
    now = [1_762_900_000.0]
    src, k = keeper(tmp_path, now)
    (src / "audit.jsonl").write_text("a\n")
    old = k.sync()
    keep = k.folder / "notes"                                      # not a day folder: never touched
    keep.mkdir()
    (k.folder / "2020-01-01.txt").write_text("a file, not a folder")
    now[0] += 31 * 86400
    with open(src / "audit.jsonl", "a") as fh:
        fh.write("b\n")
    new = k.sync()
    assert not old.exists() and (new / "audit.jsonl").read_text() == "b\n"
    assert keep.exists() and (k.folder / "2020-01-01.txt").exists()
    now[0] += 29 * 86400
    k.sync()
    assert new.exists()


def test_a_folder_it_cant_write_is_said_once_and_argus_carries_on(tmp_path, caplog):
    blocker = tmp_path / "Downloads"
    blocker.write_text("a file where the folder should be")
    k = logkeeper.LogKeeper(tmp_path, blocker / "Logs")
    assert k.sync() is None and k.sync() is None
    assert caplog.text.count("couldn't keep the logs") == 1


def test_it_runs_in_the_background_and_on_stop(tmp_path):
    now = [1_762_900_000.0]
    src, k = keeper(tmp_path, now)
    k.interval = 0.05
    (src / "crash.log").write_text("--- Argus started ---\n")
    k.start()
    k.stop()
    assert any(k.folder.glob("*/crash.log"))


def test_settings_and_argusctl_logs(tmp_path, monkeypatch, capsys):
    from argus.__main__ import main
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "home"))
    s = config.Settings()
    assert s.logs_folder == "~/Downloads/Logs" and s.logs_keep_days == 30
    assert logkeeper.from_settings(SimpleNamespace(logs_folder="", audit_path=tmp_path / "a")) is None
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text("[logs]\nkeep_days = 0\n")
    with pytest.raises(ValueError, match="logs.keep_days"):
        config.load()
    (tmp_path / "home" / "config.toml").write_text(f'[logs]\nfolder = "{tmp_path / "L"}"\nkeep_days = 7\n')
    s = config.load()
    config.ensure_private_dirs(s)
    (s.audit_path).write_text("x\n")
    assert main(["logs"]) == 0 and "days older than 7 are deleted" in capsys.readouterr().out
    assert any((tmp_path / "L").glob("*/audit.jsonl"))
    (tmp_path / "home" / "config.toml").write_text('[logs]\nfolder = ""\n')
    assert main(["logs"]) == 1 and "is off" in capsys.readouterr().out
    (tmp_path / "Blocked").write_text("x")
    (tmp_path / "home" / "config.toml").write_text(f'[logs]\nfolder = "{tmp_path / "Blocked" / "L"}"\n')
    assert main(["logs"]) == 1 and "Files and Folders" in capsys.readouterr().out
