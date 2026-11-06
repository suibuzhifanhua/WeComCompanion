# -*- coding: utf-8 -*-
"""buildlib — build.py 的按域拆分包（C 档：1606 行 12 类职责 → 分文件）。

拆分原则：函数体/注释**逐字搬迁**（注释里是事故复盘，是锁的理由，不许改写），
build.py 只留 main() 编排、CLI 解析与门禁调用（_run_gate 接线锁要求留在 main）。

模块职责：
  env.py           路径基准 / pyinstaller 探测 / 沙箱会话变量
  spec_refresh.py  hiddenimports 自动生成与写回、onedir spec 派生、冒烟模块清单
  versioning.py    版本读取 / 递增 / version_info.txt 生成
  backup.py        dist 运行时备份还原、上一版 exe 回滚备份、旧布局迁移
  chatlog.py       历史对话归档（不清断点）、断点新鲜度守卫
  ocr_sync.py      OCR 运行时旁挂同步（dist/ocr/）
  pii.py           dist PII 分发风险扫描
  gates.py         打包门禁与棘轮（F821/开发期目录/巨型函数/静默 pass/静默 except）
  release.py       --release/--lean 剥离、--prune-release 清理
  verify.py        包内前端资源一致性核对
  patching.py      pywebview 右键菜单补丁
  orchestration.py 杀进程 / PyInstaller 执行 / 冒烟 / 中间产物清理
"""
