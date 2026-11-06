# -*- coding: utf-8 -*-
"""v1.0.297：PC 操控工具（pc_*）单元/控制流测试。

原则：
- pc_files 用真实临时目录（tempfile）跑六操作，delete 顶替 SHFileMoveW 验证
  「只进回收站、永不硬删」的 FOF_ALLOWUNDO 口径。
- 剪贴板/截图：沙箱（LiteSandbox 钩子）拦截 OpenClipboard / 截屏，monkeypatch
  底层（_clip_get/_clip_set、PIL.ImageGrab.grab）只测路由与参数。
- pc_run 用 shell=True echo（沙箱内实测可用）；pc_apps list 跑真实 tasklist；
  kill 顶替 subprocess.run 只验证命令构造。
- 非 Windows 优雅降级：IS_WIN=False 时全部返回结构化错误不崩。
运行：python tests/test_os_ops.py
"""
import os
import sys
import shutil
import tempfile
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.result import is_err, err_msg  # noqa: E402
import agent_tools.os_ops as os_ops
from agent_tools import build_agent_tools, dispatch_tool

TMP = tempfile.mkdtemp(prefix="os_ops_test_")


def _cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


# ---------- 装配 ----------
def test_build_agent_tools_pc_mounted():
    cfg = {"enabled_skills": None, "skill_overrides": {}, "connectors": {}, "wecom": {}}
    tools, prompt = build_agent_tools(cfg)
    names = {t["function"]["name"] for t in tools}
    expect = {"pc_screenshot", "pc_files", "pc_run", "pc_apps", "pc_clipboard", "pc_system_info"}
    missing = expect - names
    assert not missing, f"pc-ops 工具未全挂载：{missing}"
    assert "pc_system_info" in prompt, "pc-ops 技能 prompt 未注入"
    print("PASS build_agent_tools：6 个 pc_* 工具 + pc-ops prompt 全挂载")


# ---------- pc_files ----------
def test_pc_files_write_read_list():
    d = os.path.join(TMP, "wd")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "t.txt")
    r = os_ops.pc_files("write", p, None, "hello 中文")
    assert r.get("ok") and "新建文件" in r.get("note", ""), r
    r = os_ops.pc_files("read", p)
    assert r.get("ok") and r.get("content") == "hello 中文", r
    r = os_ops.pc_files("list", d)
    assert r.get("ok") and any(e["name"] == "t.txt" for e in r["entries"]), r
    r = os_ops.pc_files("read", p)
    assert r.get("encoding") in ("utf-8", "gbk"), r
    print("PASS pc_files write/read/list：落盘→读回→列目录→编码识别")


def test_pc_files_copy_move():
    d = os.path.join(TMP, "cm")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "a.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("data")
    cp = os.path.join(d, "b.txt")
    r = os_ops.pc_files("copy", src, cp)
    assert r.get("ok") and os.path.exists(cp) and os.path.exists(src), r
    mv = os.path.join(d, "c.txt")
    r = os_ops.pc_files("move", cp, mv)
    assert r.get("ok") and os.path.exists(mv) and not os.path.exists(cp), r
    print("PASS pc_files copy/move：复制留原件、移动移走原件")


def test_pc_files_delete_recycle_only():
    """delete 必须走 SHFileOperationW 且带 FOF_ALLOWUNDO（回收站），绝不硬删。"""
    d = os.path.join(TMP, "del")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "victim.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("x")

    captured = {}

    def _fake_shfileop(ptr):
        # 真实调用传 ctypes.byref(so) → Python 侧收到 CArgObject，._obj 是被引用结构体
        if hasattr(ptr, "_obj"):
            addr = ctypes.addressof(ptr._obj)
        elif isinstance(ptr, ctypes.c_void_p):
            addr = ptr.value
        else:
            addr = int(ptr)
        # 从指针重建结构体，验证 pFrom/fFlags/wFunc
        so = os_ops.SHFILEOPSTRUCTW.from_buffer_copy(
            ctypes.string_at(addr, ctypes.sizeof(os_ops.SHFILEOPSTRUCTW)))
        captured["pFrom"] = so.pFrom
        captured["flags"] = so.fFlags
        captured["wFunc"] = so.wFunc
        # 双 null 验证：从结构体字节直接取 pFrom 指针字段值（ctypes.cast(str) 会错读
        # str 对象自身地址，不可用），再解引用读原始字节 = path(utf-16le) + 00 00 + 00 00
        import struct
        off = os_ops.SHFILEOPSTRUCTW.pFrom.offset
        pfrom_addr = int.from_bytes(
            ctypes.string_at(addr, off + 8)[off:off + 8], "little")
        raw = ctypes.string_at(pfrom_addr, (len(p) + 2) * 2)
        captured["dbl_null"] = raw == p.encode("utf-16-le") + b"\x00" * 4
        return 0  # 成功（测试桩不真删，验证「不硬删」）

    orig = ctypes.windll.shell32.SHFileOperationW
    ctypes.windll.shell32.SHFileOperationW = _fake_shfileop
    try:
        r = os_ops.pc_files("delete", p)
    finally:
        ctypes.windll.shell32.SHFileOperationW = orig
    assert r.get("ok") and "回收站" in r.get("note", ""), r
    assert captured.get("pFrom") == p, captured
    assert captured.get("dbl_null") is True, "pFrom 未双 null 结尾（SHFileOperation 要求）"
    assert captured["wFunc"] == 3, captured             # FO_DELETE
    # FOF_ALLOWUNDO(0x40) | FOF_NOCONFIRMATION(0x10) | FOF_SILENT(0x4) | FOF_NOERRORUI(0x400)
    assert captured["flags"] & 0x40, f"缺 FOF_ALLOWUNDO（回收站标志）：{captured['flags']:#x}"
    assert os.path.exists(p), "测试桩未真删文件——若此断言失败说明走了硬删路径！"
    # 不存在路径
    r = os_ops.pc_files("delete", os.path.join(d, "nope.txt"))
    assert is_err(r), r
    # 非法 op
    r = os_ops.pc_files("hack", p)
    assert is_err(r), r
    print("PASS pc_files delete：SHFileOperationW+FOF_ALLOWUNDO 进回收站，无硬删路径")


# ---------- pc_run ----------
def test_pc_run_echo_and_clamp():
    r = os_ops.pc_run("echo hello_os_297", timeout=15)
    assert r.get("ok") and r.get("exit_code") == 0, r
    assert "hello_os_297" in r.get("stdout", ""), r
    # timeout 下限夹取（1 → 5），不影响正常命令
    r = os_ops.pc_run("echo clamp_ok", timeout=1)
    assert r.get("ok") and "clamp_ok" in r.get("stdout", ""), r
    # 非法 shell
    r = os_ops.pc_run("echo x", shell="bash")
    assert is_err(r), r
    # 缺 command
    r = os_ops.pc_run("")
    assert is_err(r), r
    print("PASS pc_run：echo 输出/超时夹取/非法 shell 拦截")


# ---------- pc_apps ----------
def test_pc_apps_list_real():
    r = os_ops.pc_apps("list")
    assert r.get("ok") and r.get("total", 0) > 10, r
    top = r["top"]
    assert all("name" in x and "pid" in x and "mem_mb" in x for x in top[:3]), top[:3]
    # 内存降序
    mems = [x["mem_mb"] for x in top[:5]]
    assert mems == sorted(mems, reverse=True), mems
    print(f"PASS pc_apps list：真实 tasklist {r['total']} 进程，Top 按内存降序")


def test_pc_apps_kill_command_build():
    calls = []

    class _R:
        returncode = 0
        stdout = b"SUCCESS: process killed"
        stderr = b""

    def _fake_run(args, **kw):
        calls.append(list(args))
        return _R()

    orig = os_ops.subprocess.run
    os_ops.subprocess.run = _fake_run
    try:
        r1 = os_ops.pc_apps("kill", "chrome")
        r2 = os_ops.pc_apps("kill", "12345")
    finally:
        os_ops.subprocess.run = orig
    assert len(calls) == 2, calls
    assert r1.get("ok") and calls[0] == ["taskkill", "/F", "/IM", "chrome.exe"], calls
    assert r2.get("ok") and calls[1] == ["taskkill", "/F", "/PID", "12345"], calls
    assert "不可逆" in r1.get("note", ""), r1
    # 缺 name
    r = os_ops.pc_apps("kill")
    assert is_err(r), r
    print("PASS pc_apps kill：进程名自动补 .exe / PID 走 /PID，标注不可逆")


def test_pc_apps_unknown_op():
    r = os_ops.pc_apps("hack", "x")
    assert is_err(r), r
    print("PASS pc_apps 未知 op 拦截")


# ---------- pc_clipboard（底层 monkeypatch：沙箱拦 OpenClipboard） ----------
def test_pc_clipboard_routing():
    state = {"clip": "old_value", "set_calls": []}

    def _fake_get():
        return state["clip"]

    def _fake_set(text):
        state["set_calls"].append(text)
        state["clip"] = text

    orig_get, orig_set = os_ops._clip_get, os_ops._clip_set
    os_ops._clip_get, os_ops._clip_set = _fake_get, _fake_set
    try:
        r = os_ops.pc_clipboard("get")
        assert r.get("ok") and r.get("text") == "old_value", r
        r = os_ops.pc_clipboard("set", "new_value")
        assert r.get("ok") and r.get("chars") == len("new_value"), r
        r = os_ops.pc_clipboard("get")
        assert r.get("text") == "new_value", r
        # 空剪贴板
        state["clip"] = None
        r = os_ops.pc_clipboard("get")
        assert r.get("ok") and r.get("text") == "" and "无文本" in r.get("note", ""), r
        # set 缺 text
        r = os_ops.pc_clipboard("set")
        assert is_err(r), r
        # 未知 op
        r = os_ops.pc_clipboard("hack")
        assert is_err(r), r
    finally:
        os_ops._clip_get, os_ops._clip_set = orig_get, orig_set
    print("PASS pc_clipboard get/set 路由：读写/空剪贴板/缺参/未知 op")


# ---------- pc_system_info ----------
def test_pc_system_info_fields():
    r = os_ops.pc_system_info()
    assert r.get("ok"), r
    for k in ("hostname", "os", "cpu_count", "ram_total_gb", "local_ip", "uptime_hours"):
        assert k in r, f"缺字段 {k}: {list(r.keys())}"
    assert r["cpu_count"] and r["cpu_count"] > 0
    assert r["ram_total_gb"] > 0
    assert isinstance(r.get("disks"), list) and r["disks"], "无磁盘信息"
    assert r["disks"][0]["total_gb"] > 0
    assert isinstance(r.get("top_processes"), list) and len(r["top_processes"]) <= 10
    print(f"PASS pc_system_info：{r['ram_total_gb']}GB/{r['cpu_count']}核/ip={r['local_ip']}/磁盘{len(r['disks'])}块")


# ---------- pc_screenshot（monkeypatch ImageGrab：沙箱内截屏受限） ----------
def test_pc_screenshot_save():
    import PIL.ImageGrab as IG
    import PIL.Image as PI

    os_ops.workspace_root = lambda: TMP  # 不污染真实产物库
    orig_grab = IG.grab

    def _fake_grab(bbox=None, **kw):
        return PI.new("RGB", (10, 8), (200, 30, 30))

    IG.grab = _fake_grab
    try:
        r = os_ops.pc_screenshot()
    finally:
        IG.grab = orig_grab
    assert r.get("ok"), r
    assert os.path.isfile(r["path"]) and r["path"].endswith(".png"), r
    assert r["width"] == 10 and r["height"] == 8, r
    assert r["bytes"] > 0
    # 找不到窗口 → 全屏 + 注明
    r = os_ops.pc_screenshot(window="不存在的窗口标题_xyz")
    assert r.get("ok") and "未找到" in r.get("window", ""), r
    print("PASS pc_screenshot：落盘 PNG + 尺寸/字节 + 窗口未命中回退全屏并注明")


# ---------- 非 Windows 降级 ----------
def test_non_win_graceful():
    orig = os_ops.IS_WIN
    os_ops.IS_WIN = False
    try:
        for fn, args in [
            (os_ops.pc_screenshot, ()),
            (os_ops.pc_files, ("list", TMP)),
            (os_ops.pc_run, ("echo x",)),
            (os_ops.pc_apps, ("list",)),
            (os_ops.pc_clipboard, ("get",)),
            (os_ops.pc_system_info, ()),
        ]:
            r = fn(*args)
            assert is_err(r) and "Windows" in err_msg(r), (fn.__name__, r)
    finally:
        os_ops.IS_WIN = orig
    print("PASS 非 win32 降级：6 工具全部结构化报错不崩")


# ---------- dispatch 层 ----------
def test_dispatch_tool_pc():
    cfg = {"enabled_skills": None, "skill_overrides": {}, "connectors": {}, "wecom": {}}
    os_ops.workspace_root = lambda: TMP
    r = dispatch_tool("pc_files", {"op": "write", "path": os.path.join(TMP, "disp.txt"),
                                   "content": "via_dispatch"}, cfg)
    assert r.get("ok"), r
    r = dispatch_tool("pc_files", {"op": "read", "path": os.path.join(TMP, "disp.txt")}, cfg)
    assert r.get("content") == "via_dispatch", r
    # 参数校验：缺必填
    r = dispatch_tool("pc_files", {}, cfg)
    assert is_err(r) and "op" in err_msg(r), r
    print("PASS dispatch_tool：pc_files 经 dispatch 层读写 + 必填参数校验")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\n== 全部通过：{len(tests)} 个 PC 操控工具测试 ==")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
