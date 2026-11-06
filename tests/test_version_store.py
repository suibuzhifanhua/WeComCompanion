# -*- coding: utf-8 -*-
"""version_store tests (v1.0.358): record/prune/list/switch/pending-switch.

All filesystem operations run inside a tmp root patched over VS._APP_ROOT;
git is disabled by patching _git_exe -> None (source restore skipped).
"""
import json
import os

import pytest

from services import version_store as VS


@pytest.fixture()
def vs_root(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "version.py").write_text('VERSION = "v1.0.999"\n', encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "WeComCompanion.exe").write_bytes(b"EXE-999")
    (root / "build" / "_rollback").mkdir(parents=True)
    monkeypatch.setattr(VS, "_APP_ROOT", str(root))
    monkeypatch.setattr(VS, "_git_exe", lambda: None)
    return root


def test_record_and_list(vs_root):
    e1 = VS.record_version(note="first")
    assert e1["version"] == "v1.0.999"
    assert os.path.isfile(os.path.join(vs_root, "build", "_rollback", "WeComCompanion_v1.0.999.exe"))
    # bump version + new exe -> second record, newest first
    (vs_root / "version.py").write_text('VERSION = "v1.0.1000"\n', encoding="utf-8")
    (vs_root / "dist" / "WeComCompanion.exe").write_bytes(b"EXE-1000")
    VS.record_version(note="second")
    d = VS.list_versions()
    assert [r["version"] for r in d["versions"]] == ["v1.0.1000", "v1.0.999"]
    assert d["current"] == "v1.0.1000"
    # dedupe: record same version again -> no duplicate
    VS.record_version(note="again")
    assert len(VS.list_versions()["versions"]) == 2
    print("PASS record/dedupe/list")


def test_prune_keeps_last_N(vs_root):
    (vs_root / "dist" / "WeComCompanion.exe").write_bytes(b"EXE")
    for i in range(10):
        (vs_root / "version.py").write_text('VERSION = "v1.0.9%02d"\n' % i, encoding="utf-8")
        VS.record_version(note="n%02d" % i)
        # fake exe backup per record so prune has something to delete
        recs = VS._load()
        p = os.path.join(vs_root, "build", "_rollback", "WeComCompanion_v1.0.9%02d.exe" % i)
        with open(p, "wb") as f:
            f.write(b"X")
        recs[0]["exe"] = "build/_rollback/WeComCompanion_v1.0.9%02d.exe" % i
        VS._save(recs)
    d = VS.list_versions()
    keep = VS._KEEP   # v1.0.373：8→3（回滚备份瘦身，用户确认）
    assert len(d["versions"]) == keep, len(d["versions"])
    assert d["versions"][0]["version"] == "v1.0.909"
    # exe backups of pruned records are deleted (earliest ones gone)
    assert not os.path.exists(os.path.join(vs_root, "build", "_rollback", "WeComCompanion_v1.0.900.exe"))
    assert os.path.exists(os.path.join(vs_root, "build", "_rollback", "WeComCompanion_v1.0.909.exe"))
    print("PASS prune to last %d + exe cleanup" % keep)


def test_switch_restores_exe(vs_root):
    (vs_root / "dist" / "WeComCompanion.exe").write_bytes(b"EXE-V1")
    (vs_root / "version.py").write_text('VERSION = "v1.0.1"\n', encoding="utf-8")
    VS.record_version(note="v1")
    (vs_root / "dist" / "WeComCompanion.exe").write_bytes(b"EXE-V2")
    (vs_root / "version.py").write_text('VERSION = "v1.0.2"\n', encoding="utf-8")
    VS.record_version(note="v2")
    r = VS.switch_version("v1.0.1")
    assert r["ok"], r
    assert (vs_root / "dist" / "WeComCompanion.exe").read_bytes() == b"EXE-V1"
    # simulate the source-side switch (git disabled here, so version.py is ours)
    (vs_root / "version.py").write_text('VERSION = "v1.0.1"\n', encoding="utf-8")
    # switching to current version is rejected
    r2 = VS.switch_version("v1.0.1")
    assert not r2["ok"]
    print("PASS switch restores exe + same-version rejected")


def test_apply_pending_switch(vs_root):
    # simulate a staged swap left by switch while the exe was locked
    (vs_root / "dist" / "WeComCompanion.exe").write_bytes(b"OLD")
    (vs_root / "dist" / "WeComCompanion.exe.next").write_bytes(b"NEW")
    (vs_root / "dist" / ".version_switch").write_text('{"version":"v1.0.1"}', encoding="utf-8")
    assert VS.apply_pending_switch() is True
    assert (vs_root / "dist" / "WeComCompanion.exe").read_bytes() == b"NEW"
    assert not os.path.exists(vs_root / "dist" / "WeComCompanion.exe.next")
    assert not os.path.exists(vs_root / "dist" / ".version_switch")
    # no marker -> no-op
    assert VS.apply_pending_switch() is False
    print("PASS pending switch applied at startup")
