# -*- coding: utf-8 -*-
"""test_config_dsh — config_store 顶层 dsh 段存活回归（v1.0.300）。

背景（2026-09-10 实测事故）：dsh 段未声明进 DEFAULT_CONFIG，load_config 只按
DEFAULT_CONFIG 的键合并 → 磁盘上用户填过的 dsh 段在任意一次 save_config
（含明文敏感字段自愈重写）后被静默抹掉，凭据永远存不住。与 judge_model_id
当年同款坑（config_store.py 内注释）。

用例：
1. DEFAULT_CONFIG 必须含 dsh 段（4 键齐全）
2. load→save 往返：磁盘 dsh 段（含 api_key 明文）→ load 内存解密 → save 落盘
   dsh 段仍在、api_key 加密（enc: 前缀）、非敏感字段原样
3. 二次 load 幂等：加密后的 dsh 再 load 仍解密回原文，不再触发自愈重写
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config_store  # noqa: E402
from core.secrets import _ENCRYPT_PREFIX  # noqa: E402

DSH = {
    "base_url": "http://localhost:1234/v1",
    "api_key": "sk-test-config-123",
    "cwd": "",
    "extra_args": "--foo bar",
}


from conftest import _patch_config_path  # noqa: E402


def test_default_has_dsh():
    d = config_store.DEFAULT_CONFIG.get("dsh")
    assert isinstance(d, dict), "dsh 段必须声明进 DEFAULT_CONFIG（否则磁盘段会被 load/save 抹掉）"
    for k in ("base_url", "api_key", "cwd", "extra_args"):
        assert k in d, f"dsh 段缺键 {k}"
    print("PASS DEFAULT_CONFIG 含 dsh 段（4 键齐全）")


def test_dsh_roundtrip_survives_save():
    tmpdir = tempfile.mkdtemp(prefix="wec_cfg_dsh_")
    cfg_file = os.path.join(tmpdir, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "models": [], "dsh": dict(DSH)}, f, ensure_ascii=False)

    orig = _patch_config_path(cfg_file)
    try:
        # 第一次 load：磁盘 api_key 是明文 → 触发自愈加密重写（load 内 save_config(merged)）
        cfg = config_store.load_config()
        assert cfg["dsh"]["base_url"] == DSH["base_url"], cfg["dsh"]
        assert cfg["dsh"]["api_key"] == DSH["api_key"], "内存中应为解密后明文"
        assert cfg["dsh"]["extra_args"] == DSH["extra_args"], cfg["dsh"]

        # 显式 save（UI 改配置的典型路径）
        config_store.save_config(cfg)
        raw = json.load(open(cfg_file, encoding="utf-8"))
        assert "dsh" in raw, "save 后磁盘 dsh 段丢失（v1.0.300 修的 bug 复现）"
        assert raw["dsh"]["base_url"] == DSH["base_url"], raw["dsh"]
        assert raw["dsh"]["extra_args"] == DSH["extra_args"], raw["dsh"]
        disk_key = raw["dsh"]["api_key"]
        assert disk_key != DSH["api_key"], "api_key 明文落盘"
        assert disk_key.startswith(_ENCRYPT_PREFIX), f"api_key 应为加密态：{disk_key[:20]}"

        # 二次 load 幂等：解密回原文，dsh 仍在
        cfg2 = config_store.load_config()
        assert cfg2["dsh"]["api_key"] == DSH["api_key"], "二次 load 解密失败"
        assert cfg2["dsh"]["base_url"] == DSH["base_url"]
        print("PASS dsh 段 load→save 往返存活 + api_key 加密落盘 + 二次 load 幂等")
    finally:
        config_store.config_path = orig


if __name__ == "__main__":
    test_default_has_dsh()
    test_dsh_roundtrip_survives_save()
    print("\n== 全部通过：config dsh 段存活回归 ==")
