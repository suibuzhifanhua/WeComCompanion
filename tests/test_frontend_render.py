# -*- coding: utf-8 -*-
"""tests/test_frontend_render.py — 按域归并的回归锁合集

v1.0.648 由若干"按版本号起的文件"按域合并而来。每条 class/函数的 docstring 保留
原始出处与事故现场——版本编号是这些锁的**理由**，抹掉等于把锁降级成断言。
版本出处见各段上方的 `# ---- 来自 xxx ----` 标记。
"""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ============================================================ 来自 test_v556_close_and_toast.py（v1.0.556）
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import types


# ===================== ① Toast 来源名 =====================

def test_aumid_registered_and_used_in_toast(monkeypatch):
    """① native_toast 必须注册自有 AUMID（HKCU）并在 PowerShell 命令中使用——
    原固定用 PowerShell 的 AUMID，通知横幅来源显示成「Windows PowerShell」。
    注意：ModuleType 子类的类属性对实例不可见（Python 怪癖），fake 全部挂实例属性。"""
    import core.notify as N

    calls = {}
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0x80000001
    fake.REG_SZ = "REG_SZ"

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _create_key(root, path):
        calls["path"] = path
        return _FakeKey()

    def _set_value(k, name, reserved, typ, val):
        calls.setdefault("values", {})[name] = val

    fake.CreateKey = _create_key
    fake.SetValueEx = _set_value
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(N, "_AUMID_STATE", {"done": False, "ok": False})

    class _FakeProc:
        pass

    monkeypatch.setattr("subprocess.Popen",
                        lambda cmd, **k: calls.update(cmd=cmd) or _FakeProc())

    ok = N.native_toast("软件已在运行", "已为你切换到正在运行的窗口")
    assert ok is True
    assert calls.get("path", "").endswith(N._AUMID), f"应注册自有 AUMID：{calls.get('path')}"
    assert calls.get("values", {}).get("DisplayName") == "企业微信伴侣"
    ps_cmd = calls["cmd"][-1]
    assert N._AUMID in ps_cmd, "CreateToastNotifier 应使用自有 AUMID（而非 PowerShell 的）"
    assert "WindowsPowerShell" not in ps_cmd, "不应再回退 PowerShell AUMID"


def test_aumid_fallback_when_registry_denied(monkeypatch):
    """① 边界：注册失败（注册表被策略锁定）→ 回退 PowerShell AUMID，Toast 不中断。"""
    import core.notify as N

    def _boom(*a, **k):
        raise OSError("denied")

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0x80000001
    fake.REG_SZ = "REG_SZ"
    fake.CreateKey = _boom
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(N, "_AUMID_STATE", {"done": False, "ok": False})

    calls = {}

    class _FakeProc:
        pass

    monkeypatch.setattr("subprocess.Popen",
                        lambda cmd, **k: calls.update(cmd=cmd) or _FakeProc())
    assert N.native_toast("t", "m") is True
    ps_cmd = calls["cmd"][-1]
    assert "WindowsPowerShell" in ps_cmd, "注册失败应回退 PowerShell AUMID"


# ===================== ② 关闭动作记忆 =====================

def test_close_action_roundtrip(monkeypatch):
    """② 勾选「下次不再提示」后的持久化读写：minimize/exit 双值 + 非法值兜底 None。"""
    import backend
    import core.config_store as CS

    cfg = {"ui": {}}
    monkeypatch.setattr(CS, "update_config", lambda m: (m(cfg), True)[1])
    monkeypatch.setattr(CS, "load_config", lambda: cfg)

    assert backend._saved_close_action() is None, "未记忆时应返回 None（每次弹窗）"

    backend._save_close_action(True)
    assert cfg["ui"]["close_action"] == "minimize"
    assert backend._saved_close_action() == "minimize"

    backend._save_close_action(False)
    assert cfg["ui"]["close_action"] == "exit"
    assert backend._saved_close_action() == "exit"

    cfg["ui"]["close_action"] = "bogus"   # 脏数据兜底
    assert backend._saved_close_action() is None


def test_close_guard_install_persistence_wiring():
    """② close_guard.install 必须接受 saved_action/save_action，且 WM_CLOSE 在弹窗前
    先查记住的动作（源码级护栏——对话框为 Win32 原生循环，无法在 pytest 内驱动）。"""
    import inspect
    from desktop import close_guard

    sig = inspect.signature(close_guard.install)
    assert "saved_action" in sig.parameters and "save_action" in sig.parameters
    src = inspect.getsource(close_guard.install)
    # 记住动作的三个分支：minimize 直接隐藏、exit 直接转发关闭、弹窗后按勾选持久化
    assert '_act == "minimize"' in src and '_act == "exit"' in src
    assert "save_action(bool(minimize))" in src
    # 复选框控件与读取
    dsrc = inspect.getsource(close_guard)
    assert "IDC_CHK_REMEMBER" in dsrc and "BS_AUTOCHECKBOX" in dsrc
    assert "BM_GETCHECK" in dsrc

# ============================================================ 来自 test_v601_thinking.py（v1.0.601）
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import with_thinking_override   # noqa: E402


def _base():
    return {"id": "m1", "name": "T", "model": "t", "enable_thinking": True,
            "reasoning_effort": "low", "thinking_budget": 4000}


def test_off_turns_thinking_off_and_clears_effort():
    m = with_thinking_override(_base(), "off")
    assert m["enable_thinking"] is False and m["reasoning_effort"] is None


def test_levels_set_effort_and_enable():
    for lv in ("low", "medium", "high", "minimal"):
        m = with_thinking_override(_base(), lv)
        assert m["reasoning_effort"] == lv and m["enable_thinking"] is True
    # 大小写/空格容错
    assert with_thinking_override(_base(), " HIGH ")["reasoning_effort"] == "high"


def test_original_dict_never_mutated():
    src = _base()
    snapshot = dict(src)
    for v in ("off", "high", "", None, "junk"):
        with_thinking_override(src, v)
    assert src == snapshot, "覆盖写回了原模型 dict——会把模型管理配置改脏"


def test_empty_and_unknown_mean_follow_model():
    src = _base()
    assert with_thinking_override(src, "") is src
    assert with_thinking_override(src, None) is src
    assert with_thinking_override(src, "全部思考") is src       # 未知值原样返回
    assert with_thinking_override(None, "off") is None          # 没有模型也不炸


def test_budget_and_extra_fields_survive():
    m = with_thinking_override(_base(), "high")
    assert m["thinking_budget"] == 4000 and m["id"] == "m1"


def test_agent_loop_passes_overridden_model_to_llm(tmp_path):
    """端到端：agent_loop(thinking="off") 之后，真正交给 LLM 客户端的那个 model
    必须是关思考的副本，而 cfg 里那条模型配置保持原样。"""
    import agent.loop as AL
    import core

    orig = (core.paths.workspace_root, AL.stream_llm_events, AL.dispatch_tool)
    core.paths.workspace_root = lambda: str(tmp_path)
    seen = {}
    AL.dispatch_tool = lambda n, a, c: {"ok": True}
    AL.stream_llm_events = lambda *a, **k: (
        seen.__setitem__("model", a[0] if a else None),
        iter([("done", {"text": "好", "usage": None, "tool_calls": []})]))[1]
    cfg = {"models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                       "api_key": "", "model": "t", "enabled": True,
                       "enable_thinking": True, "reasoning_effort": "high"}],
           "wecom": {}, "connectors": {}, "custom_skills": [], "skill_overrides": {},
           "enabled_skills": [], "feature_flags": {"skills": False}}
    try:
        for _ in AL.agent_loop([{"role": "user", "content": "t"}], cfg,
                              pane_id="thkLock", max_rounds=2, thinking="off"):
            pass
    finally:
        core.paths.workspace_root, AL.stream_llm_events, AL.dispatch_tool = orig

    m = seen.get("model") or {}
    assert m.get("enable_thinking") is False, "thinking=off 没传到 LLM 客户端：%s" % (m,)
    assert m.get("reasoning_effort") is None
    assert cfg["models"][0]["enable_thinking"] is True, "把 cfg 里的模型配置改脏了"
    assert cfg["models"][0]["reasoning_effort"] == "high"


def test_agent_loop_without_thinking_keeps_model_config(tmp_path):
    """不传 thinking（旧前端/后台任务）→ 行为与今天完全一致，不多发任何参数。"""
    import agent.loop as AL
    import core

    orig = (core.paths.workspace_root, AL.stream_llm_events, AL.dispatch_tool)
    core.paths.workspace_root = lambda: str(tmp_path)
    seen = {}
    AL.dispatch_tool = lambda n, a, c: {"ok": True}
    AL.stream_llm_events = lambda *a, **k: (
        seen.__setitem__("model", a[0] if a else None),
        iter([("done", {"text": "好", "usage": None, "tool_calls": []})]))[1]
    cfg = {"models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                       "api_key": "", "model": "t", "enabled": True,
                       "enable_thinking": True, "reasoning_effort": "high"}],
           "wecom": {}, "connectors": {}, "custom_skills": [], "skill_overrides": {},
           "enabled_skills": [], "feature_flags": {"skills": False}}
    try:
        for _ in AL.agent_loop([{"role": "user", "content": "t"}], cfg,
                              pane_id="thkNone", max_rounds=2):
            pass
    finally:
        core.paths.workspace_root, AL.stream_llm_events, AL.dispatch_tool = orig
    m = seen.get("model") or {}
    assert m.get("enable_thinking") is True and m.get("reasoning_effort") == "high"


def test_reasoning_params_payload_reflects_override():
    """覆盖后的模型 dict 走 _apply_reasoning_params 时，请求体要真的带上参数
    （off 还要带 chat_template_kwargs——Qwen3/vLLM 只认模板参数）。"""
    from llm_client import _apply_reasoning_params
    p = _apply_reasoning_params({"messages": []}, with_thinking_override(_base(), "off"))
    assert p["enable_thinking"] is False
    assert p.get("chat_template_kwargs", {}).get("enable_thinking") is False
    assert "reasoning_effort" not in p
    p2 = _apply_reasoning_params({"messages": []}, with_thinking_override(_base(), "high"))
    assert p2["reasoning_effort"] == "high" and p2["enable_thinking"] is True
    assert json.dumps(p2)


def test_timeouts_relax_for_a_model_turned_on(tmp_path):
    """开思考的模型要拿到放宽的超时——覆盖写在模型 dict 上，
    _model_timeouts 读同一个 dict，这条锁住这个副作用顺序。"""
    from llm_client import _model_timeouts
    plain = {"id": "x", "model": "x"}
    on = with_thinking_override(plain, "high")
    assert _model_timeouts(on)[0] > _model_timeouts(plain)[0]


# ============================================================ 来自 test_v598_tokens.py（v1.0.598）
import os
import re

CSS = os.path.join(ROOT, "css", "style.css")
JS_DIR = os.path.join(ROOT, "js")


def _read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def _src_files():
    out = [CSS, os.path.join(ROOT, "index.html")]
    for f in sorted(os.listdir(JS_DIR)):
        if f.endswith(".js"):
            out.append(os.path.join(JS_DIR, f))
    return out


# ---- WCAG 2.1 ----
def _lin(v):
    v = v / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _lum(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(a, b):
    l1, l2 = _lum(a), _lum(b)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _root_vars():
    """取 :root 块里的自定义属性（浅色主题），值为 hex 的才解析成 rgb。"""
    css = _read(CSS)
    m = re.search(r":root\s*\{(.*?)\n\s*\}", css, re.S)
    assert m, ":root 变量块没找到（结构变了就同步改这个测试）"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    vals = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
    return vals


def _rgb(hexstr):
    h = hexstr.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    assert len(h) >= 6, "非 hex 颜色：%s" % hexstr
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ---- A1 层级 ----
def test_zindex_uses_scale_not_bare_numbers():
    css = re.sub(r"/\*.*?\*/", "", _read(CSS), flags=re.S)
    bare = [ln.strip()[:70] for ln in css.split("\n") if re.search(r"z-index\s*:\s*\d", ln)]
    assert not bare, "CSS 里又出现裸 z-index 数字：%s" % bare
    for tok in ("--z-raised", "--z-chrome", "--z-menu", "--z-modal", "--z-toast", "--z-boot"):
        assert tok in css, "缺层级令牌 %s" % tok


def test_js_modals_share_modal_layer_not_9999():
    """JS 拼出来的弹窗以前各自内联 9999，把声明式 .overlay(100) 压在下面。"""
    for p in _src_files():
        s = re.sub(r"/\*.*?\*/", "", _read(p), flags=re.S)
        assert "z-index:9999" not in s, "%s 里还有内联 z-index:9999" % os.path.basename(p)


def test_layer_order_toast_above_modal_boot_above_all():
    css = re.sub(r"/\*.*?\*/", "", _read(CSS), flags=re.S)
    nums = dict((k, int(v)) for k, v in re.findall(r"(--z-[\w-]+)\s*:\s*(\d+)", css))
    assert nums["--z-menu"] < nums["--z-modal"] < nums["--z-toast"] < nums["--z-boot"], nums


# ---- A2/A3 文字对比度 ----
def test_no_semantic_color_used_as_text_color():
    """文字只准走 -fg/-ink 角色令牌：--brand/--ok/--accent 这些是"身份色"，
    白字压在上面 2.38:1，浅底上当文字也只有 2.38:1。"""
    rx = re.compile(r"(?<![-\w])color:(\s*)var\(--(?:brand2|brand|ok|accent|warn|err)(?:,[^)]*)?\)")
    rx2 = re.compile(r"(?<![-\w])color:(\s*)var\(--tx2,[^)]*\)")
    bad = []
    for p in _src_files():
        s = re.sub(r"/\*.*?\*/", "", _read(p), flags=re.S)
        for m in list(rx.finditer(s)) + list(rx2.finditer(s)):
            bad.append("%s: %s" % (os.path.basename(p), m.group(0)))
    assert not bad, "语义身份色当文字用（应改 -fg）：%s" % bad[:8]


def test_no_bare_semantic_token_in_js_inline_style():
    """style.color / style.background 里传裸令牌，CSS 侧的正则门禁看不见。"""
    bad = []
    for p in _src_files():
        if not p.endswith(".js"):
            continue
        s = re.sub(r"/\*.*?\*/", "", _read(p), flags=re.S)
        for m in re.finditer(r"style\.color\s*=\s*[\"']var\(--(?:brand2|brand|ok|accent|warn|err)\)[\"']", s):
            bad.append("%s: %s" % (os.path.basename(p), m.group(0)))
    assert not bad, "JS 内联 style.color 用了身份色（应走 -fg）：%s" % bad


def test_white_text_on_brand_fill_meets_aa():
    v = _root_vars()
    ink = _rgb(v["--brand-ink"])
    assert _ratio((255, 255, 255), ink) >= 4.5, "白字压 --brand-ink 只有 %.2f:1" % _ratio((255, 255, 255), ink)
    ink2 = _rgb(v["--accent-ink"])
    assert _ratio((255, 255, 255), ink2) >= 4.5
    # 曾经的身份色就是过不了这条
    assert _ratio((255, 255, 255), _rgb(v["--brand"])) < 3.0, "--brand 已够深，令牌分层可以合并（更新此测试说明原因）"


def test_muted_greys_meet_aa_on_light_surfaces():
    v = _root_vars()
    # --muted 会出现在描边色底上（.tag 就是 var(--muted) on var(--border)），
    # --muted2 只允许出现在面板/内容底上，所以两者的合格面不同。
    surfaces = {"--muted": ("--bg", "--panel", "--panel2", "--agent", "--border"),
                "--muted2": ("--bg", "--panel", "--panel2", "--agent")}
    for tok, surfs in surfaces.items():
        c = _rgb(v[tok])
        for surf in surfs:
            r = _ratio(c, _rgb(v[surf]))
            assert r >= 4.5, "%s on %s = %.2f:1（<4.5）" % (tok, surf, r)


def test_semantic_text_colors_meet_aa_in_both_themes():
    light = _root_vars()
    css = _read(CSS)
    m = re.search(r"body\.dark\s*\{(.*?)\n\s*\}", css, re.S)
    assert m
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    dark = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
    surfaces = {"light": ("--bg", "--panel", "--panel2"), "dark": ("--bg", "--panel", "--panel2")}
    for name, vars_ in (("light", light), ("dark", dark)):
        for tok in ("--brand-fg", "--accent-fg", "--warn-fg", "--err-fg", "--judge-fg"):
            raw = vars_.get(tok)
            assert raw, "%s 主题缺 %s" % (name, tok)
            if raw.startswith("var("):
                raw = vars_[re.match(r"var\((--[\w-]+)\)", raw).group(1)]
            for surf in surfaces[name]:
                r = _ratio(_rgb(raw), _rgb(vars_[surf]))
                assert r >= 4.5, "%s 主题 %s on %s = %.2f:1" % (name, tok, surf, r)


def test_no_undefined_token_fallback_in_text_colors():
    """var(--tx2,#999) 这类"看着像令牌、其实从没定义过"的写法：兜底值永久生效。
    声明集 = CSS 里的 `--x:` 定义 + JS/HTML 里 setProperty 的同名令牌（--sbw 是 JS 写的）。"""
    css = re.sub(r"/\*.*?\*/", "", _read(CSS), flags=re.S)
    used = set(re.findall(r"var\((--[\w-]+)", css))
    declared = set(re.findall(r"(--[\w-]+)\s*:", css))
    for p in _src_files():
        if p == CSS:
            continue
        declared |= set(re.findall(r"['\"](--[\w-]+)['\"]", _read(p)))
        declared |= set(re.findall(r"(--[\w-]+)\s*:", _read(p)))
    missing = sorted(t for t in used if t not in declared)
    assert not missing, "CSS 用了从未定义的令牌（一直吃兜底值）：%s" % missing


# ---- v1.0.614：内联 opacity 与裸 px 字号（扫描器补上"前景也合成 opacity"后暴露的一族）----
def _dark_vars():
    css = _read(CSS)
    m = re.search(r"body\.dark\s*\{(.*?)\n\s*\}", css, re.S)
    assert m, "body.dark 覆写块没找到（结构变了就同步改这个测试）"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))


def test_no_inline_opacity_on_text():
    """文字弱化只准走色阶令牌（.dim → --muted2），不准内联 opacity。
    实测代价：安全审计时间戳挂着 style="opacity:.6"，把 --muted 压到
    浅 2.56:1 / 深 3.25:1；此前扫描器只把 opacity 合成进底色、不合成进前景，所以全漏。"""
    rx = re.compile(r"""style=["'][^"']*opacity\s*:""")
    bad = []
    for path in _src_files():
        for n, line in enumerate(_read(path).splitlines(), 1):
            if rx.search(line):
                bad.append("%s:%d" % (os.path.basename(path), n))
    assert not bad, "内联 opacity 又用到文字上（改用 .dim 类）：%s" % bad


def test_no_bare_px_font_size_in_js():
    """JS 拼的 HTML 里字号必须走 --fs-*，否则「界面字号四档缩放」对它完全无效。"""
    rx = re.compile(r"font-size:\s*\d+(?:\.\d+)?px|fontSize\s*=\s*[\"']\d+(?:\.\d+)?px")
    bad = []
    for path in _src_files():
        if not path.endswith(".js"):
            continue
        for n, line in enumerate(_read(path).splitlines(), 1):
            if rx.search(line):
                bad.append("%s:%d" % (os.path.basename(path), n))
    assert not bad, "JS 里出现裸 px 字号（改用 var(--fs-*)）：%s" % bad


def test_dim_class_is_readable_in_both_themes():
    """.dim 必须真存在且两主题都过 AA——否则"改用令牌"只是换个写法而已。"""
    assert re.search(r"\.dim\s*\{[^}]*color:var\(--muted2\)", _read(CSS)), ".dim 未定义或未走 --muted2"
    for name, vars_ in (("light", _root_vars()), ("dark", _dark_vars())):
        raw = vars_.get("--muted2")
        assert raw, "%s 主题缺 --muted2" % name
        for surf in ("--bg", "--panel", "--panel2"):
            if surf not in vars_:
                continue
            r = _ratio(_rgb(raw), _rgb(vars_[surf]))
            assert r >= 4.5, "%s 主题 .dim 文字 on %s = %.2f:1（不足 4.5）" % (name, surf, r)

# ============================================================ 来自 test_v616_visibility.py（v1.0.616）
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile

import core.paths as cp            # noqa: E402
import core.config_store as cs     # noqa: E402

@pytest.fixture()
def wsroot(tmp_path, monkeypatch):
    root = tmp_path / "产物库"
    for sub in cp._WORKSPACE_SUBS + ("台账", "日志", "组件", "系统", "知识库", "日报", "备份"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / ".隐藏").mkdir(exist_ok=True)
    monkeypatch.setattr(cp, "workspace_root", lambda: str(root))
    return root


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "config_path", lambda: str(cfg))
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        yield c
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None


def test_tree_is_dynamic_new_dirs_show_up_without_code_change(wsroot):
    subs = cp.tree_subs()
    assert set(cp._WORKSPACE_SUBS) <= set(subs)
    for want in ("台账", "日志", "日报", "备份"):
        assert want in subs, want
    for hide in ("组件", "系统", "知识库", ".隐藏"):
        assert hide not in subs, "%s 不该出现在文件树里" % hide
    (wsroot / "新归档").mkdir()
    assert "新归档" in cp.tree_subs(), "新增目录仍要回来改代码 = 没做完"


def test_readonly_flag_is_writable_list_complement(wsroot, app_client, monkeypatch):
    import routes.workspace as RW
    monkeypatch.setattr(cp, "workspace_root", lambda: str(wsroot))
    d = app_client.get("/api/workspace").get_json()
    groups = {g["sub"]: g for g in d["tree"]}
    assert groups["备份"]["readonly"] is True and groups["产出"]["readonly"] is False
    assert groups["日报"]["readonly"] is True
    assert "系统" not in groups, "api_token.txt 所在段被列进了界面"


def test_hidden_segment_cannot_be_read_via_api(wsroot, app_client, monkeypatch):
    from urllib.parse import quote
    import routes.workspace as RW
    monkeypatch.setattr(cp, "workspace_root", lambda: str(wsroot))
    (wsroot / "系统" / "api_token.txt").write_text("secret-token", encoding="utf-8")
    (wsroot / "日报" / "工作小结_2026-10-24.md").write_text("今天做了事", encoding="utf-8")
    bad = app_client.get("/api/workspace/file?sub=%s&name=api_token.txt" % quote("系统"))
    assert bad.status_code == 400, "只读段动态化后必须仍然挡住隐藏段"
    ok = app_client.get("/api/workspace/file?sub=%s&name=%s" % (quote("日报"), quote("工作小结_2026-10-24.md")))
    assert ok.get_json()["content"] == "今天做了事"


def test_size_report_still_covers_everything(wsroot, monkeypatch):
    import core.workspace_io as wio
    monkeypatch.setattr(cp, "workspace_root", lambda: str(wsroot))
    (wsroot / "日报" / "a.md").write_bytes(b"x" * 1048576)
    wio.invalidate_size_cache()
    info = wio.workspace_size_info()
    assert info["dirs"].get("日报") == 1.0, info["dirs"]
    assert info["total_mb"] >= 1.0


# ---------------- 自修复：验证通过不等于可以烧掉退路 ----------------

def test_backup_is_kept_when_git_unavailable(tmp_path, monkeypatch):
    import services.self_repair as SR
    src = tmp_path / "mod.py"
    src.write_text("a = 1\n", encoding="utf-8")
    bak = tmp_path / "mod.py.__autofix_bak__"
    bak.write_text("a = 0  # 原内容\n", encoding="utf-8")
    monkeypatch.setattr(SR, "workspace_root", lambda: str(tmp_path))
    dst = SR._stash_backup(str(src), str(bak))
    assert not bak.exists(), "备份应被挪走而不是留在源码树里"
    assert os.path.isfile(dst)
    assert "自修复" in dst and "还原备份" in dst, dst
    assert io.open(dst, encoding="utf-8").read().startswith("a = 0")


def test_autofix_orders_commit_before_removing_backup():
    src = io.open(os.path.join(ROOT, "services", "self_repair.py"), encoding="utf-8").read()
    i = src.index("# ⑥ 验证通过")
    seg = src[i:i + 900]
    assert seg.index("_autofix_git_commit") < seg.index("os.remove(bak)"), \
        "又回到先删备份再试提交（本项目不是 git 仓库 = 每次都烧掉退路）"
    assert "_stash_backup" in seg


# ---------------- 剩下的"有配置没 UI"：dsh.cwd / extra_args / 轮询间隔 ----------------

def test_dsh_extra_fields_roundtrip(app_client):
    r = app_client.post("/api/personal/config", json={"dsh": {
        "cwd": r"D:\work\tasks", "extra_args": "  --timeout 600  "}})
    assert r.get_json()["ok"] is True
    d = cs.load_config()["dsh"]
    assert d["cwd"] == r"D:\work\tasks" and d["extra_args"] == "--timeout 600"
    got = app_client.get("/api/personal/config").get_json()["dsh"]
    assert got["cwd"] == d["cwd"] and got["extra_args"] == d["extra_args"]


def test_todo_interval_shares_the_consumer_formula(app_client):
    """界面显示的间隔必须与 services/todo_watch 实际用的算法一致，否则又是两套口径。"""
    from services import todo_watch
    app_client.post("/api/personal/config", json={"background": {"todo_watch": True,
                                                                 "todo_interval": 1}})
    cfg = cs.load_config()
    assert cfg["todo_watch"]["interval_min"] == 2, cfg["todo_watch"]     # 钳位与消费方同
    assert todo_watch._cfg(cfg)["interval_min"] == 2
    got = app_client.get("/api/personal/config").get_json()["background"]
    assert got["todo_interval"] == 2
    assert todo_watch._cfg(cfg)["enabled"] is True


def test_ui_has_the_new_fields():
    html = io.open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    for i in ("dsh_cwd", "dsh_extra_args", "bg_todo_interval"):
        assert 'id="%s"' % i in html, i
    assert "每 10 分钟读一次待办页" not in html, "间隔写死在文案里，改了配置就失真"

# ============================================================ 来自 test_v623_quicklinks.py（v1.0.623）
import os
import re

import pytest

JS_PATH = os.path.join(ROOT, "js", "02-nav-quicklinks.js")


def _src():
    with io.open(JS_PATH, encoding="utf-8") as f:
        return f.read()


def _prompt_of(entry_id):
    """把某条内置入口的 prompt 字面量按 JS 转义规则还原成纯文本。"""
    src = _src()
    m = re.search(r'\{id:"%s".*?\n' % re.escape(entry_id), src)
    assert m, "默认快捷入口清单里找不到 %s" % entry_id
    seg = m.group(0)
    key = 'prompt:"'
    i = seg.index(key) + len(key)
    out = []
    while i < len(seg):
        ch = seg[i]
        if ch == "\\":
            nxt = seg[i + 1] if i + 1 < len(seg) else ""
            out.append({"n": "\n", "t": "\t", '"': '"'}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out)
        out.append(ch)
        i += 1
    pytest.fail("%s 的 prompt 字面量没有闭合" % entry_id)


# ===================== ① 逐项记录统一走 append_progress =====================

@pytest.mark.parametrize("entry_id,key_token", [
    ("study_auto", 'append_progress(key="学习进度"'),
    ("exam_auto", 'append_progress(key="答题记录"'),
    ("web_form", 'append_progress(key="答题记录"'),
])
def test_item_ledger_uses_append_progress(entry_id, key_token):
    p = _prompt_of(entry_id)
    assert key_token in p, \
        "%s 仍没改用 append_progress 记单项——只有它会让系统「记一项精简一次上下文」，" \
        "无人值守长任务就是在这儿撞资源预算的" % entry_id


def test_item_ledger_no_longer_makes_the_model_write_files_per_item():
    """负向锁：逐项落盘的旧写法必须从这三条入口里彻底消失（不是"两种都留着"）。"""
    for entry_id in ("study_auto", "exam_auto", "web_form"):
        p = _prompt_of(entry_id)
        assert "每答一题 save_workspace_file" not in p, entry_id
        assert "save_workspace_file 追加进度" not in p, entry_id


# ===================== ② 有限次数的考试不许自动开考 =====================

def test_study_auto_respects_exam_attempt_budget():
    p = _prompt_of("study_auto")
    assert "剩余考试次数" in p or "剩余次数" in p, "study_auto 没读考试次数"
    assert "不自动开考" in p, \
        "study_auto 让模型对「剩 N 次」的考试也点「开始考试」= 白烧真实考试机会" \
        "（study_engine / study-solver 早已按 v1.0.290 禁止）"
    assert "防白烧次数" in p, "study_auto 缺跳过有限次数考试的理由，模型容易自作主张"


def test_study_auto_keeps_the_speed_limit_guard():
    p = _prompt_of("study_auto")
    assert "2x" in p and "风控" in p, \
        "视频倍速上限没了——异常倍速会被平台判无效学习（study_engine 同口径）"


# ===================== ③ 静音表达式不再在快捷入口里留第三份副本 =====================

def test_study_auto_does_not_inline_the_mute_js_copy():
    p = _prompt_of("study_auto")
    assert "__muteLoop=setInterval" not in p, \
        "快捷入口里又内嵌了一份静音表达式副本——三处（引擎/技能/入口）迟早改漏，以技能原文为准"
    assert "静音三件套" in p, "去掉副本的同时必须把「用哪份表达式」指清楚"


# ===================== ④ 台账读写路径接成同一条闭环 =====================

@pytest.mark.parametrize("entry_id", ["exam_auto", "web_form"])
def test_ledger_read_path_covers_progress_file(entry_id):
    p = _prompt_of(entry_id)
    assert "答题记录_进度.md" in p, \
        "%s 只读 知识库/题库/答题记录_*.md，但 append_progress 落在 产出/答题记录_进度.md——" \
        "同款题复用这条闭环没接上" % entry_id


# ===================== ⑤ RESULT 标记：DEAD 用于「重试无意义」 =====================

def test_auto_entries_emit_three_state_result():
    for entry_id in ("study_auto", "exam_auto"):
        p = _prompt_of(entry_id)
        assert "RESULT: PASS" in p and "RESULT: FAIL" in p, entry_id
        assert "RESULT: DEAD" in p, \
            "%s 缺 DEAD：平台侧已失效（0题/考试已结束）却被写成 FAIL，auto_loop 会白续跑十几分钟" % entry_id
        assert "正文里绝不再出现 RESULT 字样" in p, \
            "%s 没提醒「正文别出现 RESULT」——auto_loop 按字符串判定，正文里顺嘴一提就误触发续跑" % entry_id


def test_dead_does_not_trigger_resume():
    """口径落地的前提：auto_loop 只对 RESULT: FAIL 续跑，DEAD 必须和 PASS 一样停手。"""
    src = io.open(os.path.join(ROOT, "agent", "auto_loop.py"), encoding="utf-8").read()
    assert 'if "RESULT: FAIL" not in final_answer' in src, \
        "续跑判据变了——提示词里的 RESULT 三态口径要跟着重核"
    assert "DEAD" not in src, \
        "auto_loop 里出现了 DEAD：若它被并进续跑条件，提示词写 DEAD 就变成白烧轮次"


# ===================== ⑥ 提示词里点名的工具，必须真的存在 =====================

@pytest.mark.parametrize("entry_id", ["study_auto", "exam_auto", "web_form"])
def test_named_tools_exist(entry_id, monkeypatch):
    from agent_tools.registry import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    p = _prompt_of(entry_id)
    called = set(re.findall(r"\b([a-z][a-z0-9_]{4,})\(", p))
    assert called, "%s 提示词里一个工具名都没点名（口径无法核对）" % entry_id
    ghost = sorted(n for n in called if n not in names)
    assert not ghost, "%s 提示词点名了不存在的工具：%s" % (entry_id, ghost)


# ============================================================ 来自 test_v614_copy_truth.py（v1.0.614）
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _read_v614(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_rollback_count_copy_matches_code():
    """所有"近 N 版可回退"类文案里的 N 必须等于 version_store._KEEP。"""
    from services import version_store
    keep = version_store._KEEP
    bad = []
    for rel in ("index.html", "README.md"):
        # 先剥标签：`最近 <b>8 个版本</b>` 曾被下面的正则整个漏掉（"近"与数字之间夹了标签），
        # 于是帮助页写着 8 版、代码 _KEEP=3，这条锁却一路绿灯到 v1.0.615 才被查出。
        s = re.sub(r"<[^>]+>", "", _read_v614(rel))
        for m in re.finditer(r"(?:近|保留最近)\s*(\d+)\s*(?:个)?(?:版本|版)", s):
            if int(m.group(1)) != keep:
                bad.append("%s:%s版（代码 _KEEP=%d）" % (rel, m.group(1), keep))
    assert not bad, "回退版数文案与代码不一致：%s" % bad


def test_no_pointer_to_nonexistent_settings_ui():
    """体检/提示里指出的界面入口，界面上必须真找得到（曾经指向不存在的 dsh 配置区）。"""
    h = _read_v614("index.html") + _read_v614("js/11-workspace-kb-study.js")
    s = _read_v614("routes/b_ocr_admin.py")
    for m in re.finditer(r"设置\s*→\s*([^」，。；\n|）]{2,20})", s):
        name = m.group(1).strip()
        # 提示语常写成「设置 → 企微绑定里指定 exe 路径」——取能对上界面名的最长前缀，
        # 否则这条锁会被自己的措辞骗红（第一版就是这么红的）。
        hit = next((name[:n] for n in range(len(name), 3, -1) if name[:n] in h), None)
        assert hit, "提示指向『设置 → %s』，但界面里搜不到这个入口" % name


def test_component_footer_matches_one_click_set():
    """组件页脚注必须与「一键安装」的真实集合一致（不能再写 Git/Node 需自行下载）。"""
    h = _read_v614("index.html")
    i = h.find("「一键安装」")
    assert i > 0, "组件页脚注找不到了"
    seg = h[i:i + 260]
    assert "自行下载" not in seg, "脚注还在让用户自己下载：%s" % seg[:120]
    for k in ("OCR", "Git", "Node"):
        assert k in seg, "脚注漏了 %s" % k


def test_permission_copy_discloses_background_downgrade():
    """「每次询问」的文案必须披露：后台会话（定时/刷课）会自动降为自动审批。"""
    h = _read_v614("index.html")
    hits = [l for l in h.split("\n") if "每次询问" in l and "自动降" in l]
    assert hits, "权限档文案没披露后台会话的降级（agent/loop.py:98-102 会降）"


def test_silent_except_sites_now_log():
    """三处「吞异常只回传/只 pass」补日志的源码锁（同类缺口别再回到静默）。"""
    assert "工具执行异常" in _read_v614("agent/loop.py")
    lp = _read_v614("agent/loop.py")
    i = lp.find("def _exec_one")
    assert "traceback.format_exc()" in lp[i:i + 900], "_exec_one 的异常没落栈"
    b = _read_v614("backend.py")
    assert '[restart] 管理员重启看门狗异常' in b, "重启看门狗的整块 except 还是静默 pass"
    assert '[restart] 失败提示 toast 未发出' in b, "toast 失败分支仍无日志"
    c = _read_v614("services/conversations.py")
    assert "会话文件解析失败" in c, "list_all 解析失败仍是静默 continue"

# ============================================================ 来自 test_v616_frontend.py（v1.0.616）
import os
import re

def _read_v616(*rel):
    return io.open(os.path.join(ROOT, *rel), encoding="utf-8").read()


CSS_v616 = _read_v616("css", "style.css")
HTML = _read_v616("index.html")
CHAT = _read_v616("js", "10-chat.js")
WIN = _read_v616("js", "06-windows.js")
EXP = _read_v616("js", "03-experiences.js")
MOD = _read_v616("js", "07-usage-models.js")
WS = _read_v616("js", "11-workspace-kb-study.js")
CONN = _read_v616("js", "05-connectors-status.js")
CORE = _read_v616("js", "01-core.js")
INIT = _read_v616("js", "12-init.js")


def _lum_v616(hexcol):
    h = hexcol.lstrip("#")
    v = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]


def _ratio_v616(a, b):
    """v616 这段吃的是 hex 字符串（不是 rgb 三元组），别和上面的 _ratio 混用。"""
    l1, l2 = _lum_v616(a), _lum_v616(b)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def test_side_muted_token_meets_aa_on_the_always_dark_sidebar():
    m = re.search(r"--side-muted\s*:\s*(#[0-9a-fA-F]{6})", CSS_v616)
    assert m, "缺少 --side-muted 令牌"
    fg = m.group(1)
    bg = re.search(r"\.sidebar\s*\{[^}]*linear-gradient\(180deg,\s*(#[0-9a-fA-F]{6})", CSS_v616).group(1)
    r = _ratio_v616(fg, bg)
    assert r >= 4.5, "--side-muted %s 压在侧栏底 %s 上只有 %.2f:1" % (fg, bg, r)
    # 侧栏内的弱化文字必须改用这个令牌，不能再拿浅主题的 --muted2 顶
    for sel in (".side-group-head", ".side-group-edit"):
        i = CSS_v616.index(sel)
        assert "var(--side-muted)" in CSS_v616[i:i + 260], sel


def test_archived_rows_are_gone_from_the_sidebar_rail():
    """v1.0.642 口径变更：归档项**不再进侧栏任务列表**（原先是灰化排在末尾一段），
    落脚地改到「设置 → 已归档任务」。所以这条锁从"归档行不许用 opacity 弱化"
    改成"归档行在侧栏里根本不该再存在"——`.arch` 选择器与灰化色一起删掉，
    留着一个永不命中的选择器就是下一个"看着还在其实已失效"。"""
    assert ".tr-item.arch" not in CSS_v616 and ".arch" not in CSS_v616, \
        "侧栏已不再渲染归档行，`.arch` 规则要一起删（留着就是永不命中的死选择器）"


def test_focus_ring_covers_tabindex_rows():
    """v1.0.650：原来用「文件里第一个 :focus-visible 前后 ±200/600 字」取窗口——
    新增任何一条更早的 :focus-visible 规则都会把窗口挪走（本版本就中招一次）。
    改成按选择器本身找规则，判据不变：这两条都必须带可见 outline。"""
    for sel in ('[tabindex="0"]:focus-visible', ".tr-item:focus-visible"):
        i = CSS_v616.index(sel)
        assert "outline" in CSS_v616[i:i + 260], f"{sel} 丢了可见焦点环"


def test_clickable_rows_are_keyboard_reachable():
    assert 'div.setAttribute("role", "button")' in _read_v616("js", "02-nav-quicklinks.js")
    assert 'el.setAttribute("role","button")' in WIN
    rows = [l for l in CHAT.splitlines() if 'class="tr-item' in l]
    assert len(rows) >= 3, rows
    assert all('tabindex="0"' in l and 'role="button"' in l for l in rows), \
        [l[:60] for l in rows if 'tabindex="0"' not in l]
    assert 'e.key !== "Enter"' in INIT and "t.click()" in INIT, "缺 Enter/Space → click 转接"


def test_status_spans_are_announced():
    assert 'id="opState" aria-live="polite"' in HTML
    assert 'id="setState" aria-live="polite"' in HTML


def test_failure_paths_surface_something():
    assert "_winListFailed" in WIN and "窗口列表读取失败" in WIN
    i = WIN.index("async function refreshWindows")
    assert "_winListFailed(" in WIN[i:i + 400], "拉取失败又变回静默 return"
    assert "截图读取失败" in WIN
    i = MOD.index("async function loadModels")
    seg = MOD[i:i + 900]
    assert "Array.isArray" in seg and "模型列表读取失败" in seg
    assert "finally{ btn.disabled = false; }" in EXP, "保存按钮仍会在失败后永久禁用"


def test_escaping_is_consistent_across_surfaces():
    assert "escapeHtml(it.ts)" in WS, "记忆时间戳仍是不转义的模型产出"
    assert "${escapeHtml(c.icon||" in CONN, "连接器 icon 仍未转义"
    assert "jsStr(e.id)" in EXP, "经验 id 仍裸拼进 onclick"


def test_code_point_safe_truncation():
    assert "function safeSlice" in CORE and "Array.from" in CORE
    # 先剥 // 注释：本项目注释惯例是「WHY + 引用被替换掉的旧写法原文」，
    # 不剥就会把我自己写的 `t.slice(0,18)` 说明文字判成"残留未清"（本轮已踩三次）。
    win_code = re.sub(r"//[^\n]*", "", WIN)
    chat_code = re.sub(r"//[^\n]*", "", CHAT)
    assert ".slice(0,14)" not in win_code and "t.slice(0,18)" not in chat_code
    assert "safeSlice(t,14)" in CHAT and "safeSlice(pw.title" in WIN


def test_no_new_inline_hex_in_touched_spots():
    code = re.sub(r"//[^\n]*", "", WS)
    for hexcol in ("#fef3c7", "#b45309", "#d97706"):
        assert hexcol not in code, "%s 又回到内联硬编码" % hexcol
    assert ".tag.wrn" in CSS_v616 and "var(--warn-bg)" in CSS_v616[CSS_v616.index(".tag.wrn"):CSS_v616.index(".tag.wrn") + 160]
    for hexcol in ("#c0392b", "color:#888", "dashed #ccc"):
        assert hexcol not in HTML, hexcol

# ============================================================ 来自 test_v633_sidebar.py（v1.0.633）
import os
import re

def _src_v633(*rel):
    return io.open(os.path.join(ROOT, *rel), encoding="utf-8").read()


HTML_v633 = _src_v633("index.html")
CSS_v633 = _src_v633("css", "style.css")
JS02 = _src_v633("js", "02-nav-quicklinks.js")
JS10 = _src_v633("js", "10-chat.js")
JS11 = _src_v633("js", "11-workspace-kb-study.js")
JS12 = _src_v633("js", "12-init.js")


def _navmore_block():
    i = HTML_v633.find('id="navMore"')
    return HTML_v633[i:HTML_v633.find('data-view="help"', i)]


# ---------------------------------------------------------------- 结构
def test_quick_group_is_the_first_thing_in_the_nav():
    """v1.0.644（用户："快捷入口 放工作台按钮上面"）：分组是 .nav 的第一个子元素，
    不再寄存在「➕ 更多」里——它是"发起一次任务"的入口，不该埋在一个默认收起的折叠组下面。"""
    i = HTML_v633.find('<nav class="nav">')
    assert i > 0
    first = HTML_v633[i + len('<nav class="nav">'):].lstrip()
    # 允许紧跟一段注释，注释之后第一个元素必须是分组本身
    first = re.sub(r"^\s*<!--.*?-->\s*", "", first, flags=re.S)
    assert first.startswith('<div class="side-group collapsed" id="quickGroup">'), first[:120]
    assert 'id="quickLinksBox"' in HTML_v633[:HTML_v633.find('data-view="work"')], "分组体被留在了「更多」里"


def test_quick_group_is_out_of_nav_more_and_not_duplicated():
    """挪位置最容易留的尾：老副本没删干净 ⇒ 两个 id="quickGroup"，$() 只认第一个，
    于是「更多」里那份永远空着，用户以为快捷入口没了。"""
    assert HTML_v633.count('id="quickGroup"') == 1, HTML_v633.count('id="quickGroup"')
    assert HTML_v633.count('id="quickLinksBox"') == 1
    blk = _navmore_block()
    assert 'class="side-group' not in blk, "「更多」里还留着一份分组"
    assert "快捷入口" not in HTML_v633[HTML_v633.find('id="navMoreBtn"'):HTML_v633.find('id="navMore"')], \
        "「更多」的 title 还在承诺一个里面没有的入口"


def test_auto_task_group_is_gone():
    """v1.0.635（用户要求）：「🤖 自动任务」分组删除，auto_send 条目并回快捷入口。"""
    assert 'id="autoGroup"' not in HTML_v633 and 'id="autoLinksBox"' not in HTML_v633
    assert "renderAutoLinks" not in JS02


def test_quick_group_sits_above_every_view_item():
    blk = HTML_v633[HTML_v633.find('<nav class="nav">'):HTML_v633.find("</nav>")]
    i_q = blk.find('id="quickGroup"')
    order = ["work", "study", "chat", "knowledge", "settings"]
    pos = [blk.find('data-view="%s"' % v) for v in order]
    assert -1 < i_q < min(pos), (i_q, pos)


def test_no_side_group_left_outside_nav():
    """任务区与状态条之间不该再夹着常驻分组（用户要的就是这块位置）。"""
    i = HTML_v633.find('id="taskSec"')
    j = HTML_v633.find("</aside>", i)
    seg = HTML_v633[i:j]
    assert 'class="side-group"' not in seg, seg[:300]


def test_quick_group_is_collapsed_in_the_markup():
    """"默认折叠"要在 JS 跑之前就已成立：HTML_v633 自带 collapsed（首帧不会闪一下展开），
    JS 侧的默认判据是"没写过就等于折叠"——只有显式 "0" 才算用户点开过。"""
    i = HTML_v633.find('<div class="side-group collapsed" id="quickGroup">')
    assert i > 0, "分组首帧不是折叠态"
    caret = HTML_v633[i:i + 900]
    assert 'aria-expanded="false"' in caret, "折叠态箭头没有 aria-expanded，首帧指向错误"
    m = re.search(r'let __quickFolded = [^\n]*getItem\("__quickFold"\)\s*!==\s*"(\d)"', JS02)
    assert m, "读不到分组折叠态的默认判据"
    assert m.group(1) == "0", \
        '默认判据改成 !== "%s" 了：没写过偏好的人一开机就看见分组摊开——用户要的是默认折叠' % m.group(1)


def test_group_fold_button_uses_the_unified_primitive():
    """v1.0.633 撤掉折叠钮是为了"别两层折叠"；v1.0.635 按用户要求把快捷入口做成
    默认折叠——但折叠钮必须用统一原语（class 带 fold-ic、状态走 aria-expanded），
    别再各写一套换字的按钮。"""
    i = HTML_v633.find('id="quickFoldBtn"')
    assert i > 0, "快捷入口分组没有折叠钮（用户要求默认折叠）"
    tag = HTML_v633[HTML_v633.rfind("<button", 0, i):i + 220]
    assert "fold-ic" in tag and 'aria-expanded="false"' in tag, tag
    assert "autoFoldBtn" not in HTML_v633, "自动任务的折叠钮该随分组一起消失"


def test_dead_fold_functions_are_not_referenced_anywhere():
    """经典脚本里一个悬空调用只中断那一行，界面表现是"少了半块"且没有任何提示。"""
    for name, src in (("10", JS10), ("11", JS11), ("12", JS12)):
        for fn in ("applyAutoFold(", "applyQuickFold(", "toggleAutoFold(", "renderAutoLinks("):
            assert fn not in src, f"js/{name} 还在引用已删除的 {fn}"
    # 「更多」里的快捷入口折叠只允许存在于 02（定义 + 钮的 onclick）
    assert JS02.count("function applyQuickFold") == 1


def test_stowaway_preference_is_really_gone():
    """设置页那个「自动任务分组默认收起」开关已经不驱动任何东西——留着就是一句不再生效的承诺。"""
    assert "opt_auto_fold" not in HTML_v633
    assert "opt_auto_fold" not in JS11
    # 只盯"真的在读写这个键"，注释里提到它正是为了说明它没了
    assert 'getItem("__autoFold"' not in JS02 and 'setItem("__autoFold"' not in JS02


# ---------------------------------------------------------------- 空间真的让给任务区
def test_tasksec_no_longer_capped_at_38vh():
    """.tasksec 的 38vh 上限是"下面还压着两个常驻分组"时代的分配，不撤掉等于白挪。"""
    i = CSS_v633.find(".tasksec {")
    body = CSS_v633[i:CSS_v633.find("}", i)]
    assert "max-height:none" in body, body
    assert "38vh" not in body


def test_nav_yields_space_and_scrolls_instead_of_growing():
    i = CSS_v633.find(".nav {")
    body = CSS_v633[i:CSS_v633.find("}", i)]
    assert "flex:0 1 auto" in body, "导航还在 flex-grow，抢的就是任务列表的剩余空间"
    assert "max-height" in body and "overflow:auto" in body, "「更多」展开时导航要自己滚动，别顶穿侧栏"
    assert "min-height:158px" in body, "v1.0.591 的保底高度不能退（窗口一矮导航被挤到 16px 那次）"


def test_groups_inside_more_are_not_nested_scrollers():
    """v1.0.644 起分组直接装在已经滚动的 .nav 里：外层滚 + 内层 max-height:30vh/overflow
    就是双层滚动条（滚轮一卡就找不到列表）。30vh 那条帽必须留在基础规则之外。"""
    blk = CSS_v633[CSS_v633.find(".side-group {"):]
    blk = blk[:blk.find("}")]
    assert "max-height" not in blk and "overflow" not in blk, blk


# ---------------------------------------------------------------- 任务区显示
def test_running_rows_get_pinned_and_counted():
    # 先剥注释：本轮解释"为什么撤掉归档段"的注释里就原样引用着「已归档」，
    # 不剥的话这条锁会被自己的文档判红（本项目注释风格的老坑）。
    js10 = _code_only(JS10, "js")
    seg = js10[js10.find("function renderTaskRail"):]
    seg = seg[:seg.find("\nfunction ", 10)]
    assert "进行中" in seg, "运行中的任务没有单独成段/计数"
    # v1.0.642：归档段整段撤掉——归档即离开任务列表，落脚地是「设置 → 已归档任务」。
    assert "已归档" not in seg, "侧栏又在渲染归档段（用户要求归档后不在任务模块显示）"
    assert "m.archived" in seg, "没有把归档项挡在列表之外"
    assert "filter(_trRunning)" in seg


def test_day_headers_are_sticky_and_titles_wrap_two_lines():
    assert re.search(r"\.tr-day \{[^}]*position:sticky", CSS_v633, re.S), "长列表滚动时日期头要吸顶"
    i = CSS_v633.find(".tasksec .tr-t {")
    body = CSS_v633[i:CSS_v633.find("}", i)]
    assert "-webkit-line-clamp:2" in body, body
    assert "white-space:nowrap" not in body, "还是单行省略号＝两行截断没落地"


def test_render_task_rail_stays_under_the_giant_function_ratchet():
    """B9 棘轮的直接受益例子：这次给渲染加了三件事，靠抽 _trRow/_trSection 才没越线。"""
    seg = JS10[JS10.find("function renderTaskRail"):]
    seg = seg[:seg.find("\nfunction ", 10)]
    assert seg.count("\n") < 100, seg.count("\n")
    for helper in ("function _trRow(", "function _trSection(", "function _trRunning("):
        assert helper in JS10, helper


# ---------------------------------------------------------------- v1.0.635 状态条钉底
def _code_only(text, kind):
    """剥掉注释再扫——这些锁要管的是**代码**，而注释里恰恰会提到被删掉的旧写法
    （本轮就自己咬过自己：解释"为什么删 .side-fold.on{transform}"的注释把锁判红了）。"""
    if kind == "html":
        return re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    s = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if kind == "js":
        # 只剥整行注释：`//` 也出现在 "https://…" 字符串里，全局剥会把代码一起吞掉
        s = re.sub(r"(?m)^[ \t]*//.*$", " ", s)
    return s


def _rule(css, selector):
    """取该选择器**所有**规则体（.side-foot 在深浅主题下各有一条，只看第一条会测错东西）。"""
    return [m.group(0) for m in re.finditer(re.escape(selector) + r"\s*\{[^}]*\}", css)]


def test_status_bar_is_pinned_to_the_sidebar_bottom():
    """用户截图实锤：任务区一折叠，状态三圆点就飘到面板中间。
    侧栏是 flex 纵向列，剩余空间出现在状态条**之下**时它就往上走——只能靠 margin-top:auto 顶住。"""
    rules = [r for r in _rule(_code_only(CSS_v633, "css"), ".side-foot") if "padding" in r]
    assert rules, "找不到 .side-foot 的主规则"
    joined = " ".join(rules)
    assert "margin-top:auto" in joined, joined
    assert "flex:0 0 auto" in joined, joined


# ---------------------------------------------------------------- v1.0.635 折叠图标统一
def test_fold_primitive_exists_and_owns_the_glyph():
    """字形写在 CSS_v633 里、状态走 aria-expanded——这是"统一"的技术含义。"""
    css = _code_only(CSS_v633, "css")
    assert re.search(r"\.fold-ic::before\s*,\s*\.fold-caret::before\s*\{[^}]*content:\"▾\"", css, re.S), \
        "折叠字形没有收敛到一处"
    assert re.search(r"\[aria-expanded=\"false\"\][^{]*\{[^}]*rotate\(-90deg\)", css, re.S), \
        "收起态没通过 aria-expanded 旋转箭头"


def test_js_no_longer_swaps_fold_glyphs():
    """JS 里不许再出现 textContent=\"▶\"/\"▼\"/\"▸\"/\"▴\"/\"»\"/\"«\" 或「▾ 折叠」这类换字。
    换字就是"同一个动作五种画法"的根源，也让按钮在 JS 跑之前短暂显示错方向。"""
    banned = ('="▶"', '="▼"', '="▸"', '="▴"', '="»"', '="«"', "▾ 折叠", "▸ 展开")
    for name, src in (("02", JS02), ("04", _src_v633("js", "04-skills.js")),
                      ("06", _src_v633("js", "06-windows.js")), ("10", JS10),
                      ("11", JS11), ("12", JS12)):
        code = _code_only(src, "js")
        for b in banned:
            assert b not in code, f"js/{name} 仍在用换字表达折叠状态：{b}"


def test_set_fold_is_the_single_writer_of_fold_state():
    js = _src_v633("js", "02-nav-quicklinks.js")
    assert "function setFold(" in js, "没有统一入口"
    for name, src in (("10", JS10), ("11", JS11), ("04", _src_v633("js", "04-skills.js")),
                      ("06", _src_v633("js", "06-windows.js")), ("12", JS12)):
        assert "setFold(" in src or "aria-expanded" not in src, f"js/{name} 绕开了 setFold"


def test_active_state_no_longer_rotates_the_batch_button():
    """`.side-fold.on{transform:rotate(180deg)}` 本意是给"展开中的折叠钮"转向，
    但 .on 同时是 ☑ 批量管理的激活态——进入批量模式时那个对勾会上下颠倒。"""
    css = _code_only(CSS_v633, "css")
    assert not re.search(r"\.side-fold\.on\s*\{[^}]*transform", css, re.S), \
        "☑ 激活态又会被转 180° 了"
    assert not re.search(r"\.livecard\.collapsed \.foldbtn\s*\{[^}]*transform", css, re.S), \
        "折叠钮转向该由 aria-expanded 统一表达"


def test_nav_sub_items_share_the_top_level_font_size():
    """v1.0.643（用户："模板 技能中心 经验库一直到帮助 这几个按钮字的大小和工作台/学习中心/
    助手对话保持一致"）：原先同一列里三种字号（顶层 13px / 子项 12px / 组标题 11px），
    看着像这些入口低一等。层级交给缩进去表达，字号统一跟 `.nav-item`。
    v1.0.644：分组升到导航顶端后，组标题的选择器从 `.nav-more .side-group-head` 回到基础
    规则 `.side-group-head`——判据没变，只是它终于和邻居同级了。"""
    css = _code_only(CSS_v633, "css")
    top = re.search(r"\.nav-item \{[^}]*\}", css).group(0)
    assert "font-size:var(--fs-md)" in top, "顶层字号被改动过，下面两条比的就不是同一个基准"
    assert not re.search(r"\.nav-more \.nav-sub\s*\{[^}]*font-size", css), \
        "「更多」子项又被单独压小了"
    head = re.search(r"\.side-group-head \{[^}]*\}", css).group(0)
    assert "font-size:var(--fs-md)" in head, \
        "组标题还停在 --fs-xs：它是导航第一行，邻居全 13px 它留 11px 就是一列三种字号"


def test_nav_separator_is_actually_drawn():
    """v1.0.355 那句"设置移到导航最底部，加分隔线与功能视图区隔"一直没落地：
    `.nav-sep` 只有浅色主题的 border-top-color 和细条模式的 margin 两条覆盖规则，
    基础规则整条不存在 ⇒ 那根线从来没画出来过。快捷入口分组自己也要一根同款收尾线，
    顺手把标度定在一条地方。"""
    css = _code_only(CSS_v633, "css")
    sep = re.search(r"\.nav-sep \{([^}]*)\}", css).group(1)
    assert "border-top:1px solid" in sep, sep
    grp = re.search(r"\.side-group \{([^}]*)\}", css, re.S).group(1)
    assert "border-bottom:1px solid" in grp, grp


def test_help_text_no_longer_points_at_the_deleted_group():
    """口径同步：帮助/编辑器说明里若还写「🤖 自动任务」分组，用户会照着找一个不存在的东西。"""
    body = _code_only(HTML_v633, "html")
    assert "🤖 自动任务" not in body, "index.html 正文仍在指引已删除的分组"


# ---------------------------------------------------------------- v1.0.636 默认宽度 / 折叠留白
def test_folded_tasksec_releases_the_min_height():
    """用户第二张截图：任务区折叠后仍空一大片。
    `.tasksec.folded{flex:0 0 auto}` 只让它"不再长大"，基础规则的 min-height:140px
    仍然兜着——实测折叠态 139px 而标题行只有 39px。折叠必须连 min-height 一起撤。"""
    css = _code_only(CSS_v633, "css")
    folded = _rule(css, ".tasksec.folded")
    assert folded, "找不到 .tasksec.folded 规则"
    assert "min-height:0" in folded[0], folded[0]
    assert any("min-height:140px" in r for r in _rule(css, ".tasksec")), \
        "展开态的 140px 保底不能一起删（v1.0.591：窗口一矮列表被挤没那次）"


def test_default_sidebar_width_clears_the_tagged_rows():
    """192 不是拍脑袋：headless 扫过 168/176/184/192（build/_test_sidebar_v636.py），
    前三档下「学习中心」「持续答题」仍被截成省略号——图标 + 名字 + 行尾「即发/🔁」
    徽章一起吃宽度。低于这个值就是回归。"""
    m = re.search(r"\.sidebar\s*\{\s*width:var\(--sbw,\s*(\d+)px\)", _code_only(CSS_v633, "css"))
    assert m, "读不到侧栏默认宽"
    assert int(m.group(1)) >= 192, "侧栏默认宽退回 %s px，快捷入口条目又要截断" % m.group(1)


def test_reset_does_not_recopy_the_default_width():
    """「双击恢复默认」原先在 JS 里又写了一遍 168px——改 CSS_v633 默认值时它不会跟着改。
    正确做法是抹掉行内值与记忆，让 CSS_v633 的 var() 兜底成为唯一事实源。"""
    js = _code_only(JS02, "js")
    assert not re.search(r'setProperty\(\s*["\']--sbw["\']\s*,\s*["\']\d+px["\']', js), \
        "JS 里又手抄了一个默认宽度"
    assert not re.search(r'setItem\(\s*["\']sbw["\']\s*,\s*["\']\d+["\']', js), \
        "恢复默认不该把宽度值写回 localStorage"


def test_builtin_names_dropped_the_suffix_the_badge_already_says():
    """「(无人值守)」是 v1.0.251「🤖 自动任务」分组时代的唯一信号；v1.0.635 分组并回
    快捷入口、改由行尾「即发」徽章表达之后，它成了 5 个纯冗余字符，正好挤掉名字。"""
    js = _code_only(JS02, "js")
    seg = js[js.index("const QUICK_LINKS_DEFAULT"):js.index("let __quickLinks")]
    assert "(无人值守)" not in seg, "内置入口名还带着与「即发」重复的后缀"


def test_legacy_names_are_migrated_before_render():
    """改名只动了内置默认值——用户 config 里存的还是旧名，所以载入时必须按 id 换过来。
    只认旧默认名：用户自己改过的名字不许被覆盖。"""
    js = _code_only(JS02, "js")
    assert "function migrateBuiltinNames(" in js, "没有载入期改名这一步"
    assert '"学习中心(无人值守)": "学习中心"' in js.replace("  ", " "), "改名表少了学习中心"
    seg = js[js.index("async function loadQuickLinks"):]
    seg = seg[:seg.index("renderQuickLinks()")]
    assert "migrateBuiltinNames(__quickLinks)" in seg, "改名迁移没在渲染之前跑"


def test_help_text_points_at_labels_users_actually_see():
    body = _code_only(HTML_v633, "html")
    for old in ("学习中心(无人值守)", "持续答题(无人值守)"):
        assert old not in body, "帮助正文仍指引界面上已不存在的入口名：%s" % old

# ============================================================ 来自 test_v637_railfold.py（v1.0.637）
import os
import re

def _src_v637(*rel):
    return io.open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _code_only_v637(text, kind="js"):
    """剥注释再扫：本项目的注释风格是「WHY + 引用被替换掉的旧写法」，直接 grep 会自己咬自己。"""
    s = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if kind == "js":
        s = re.sub(r"(?m)^[ \t]*//.*$", " ", s)
    return s


CSS_v637 = _src_v637("css", "style.css")
HTML_v637 = _src_v637("index.html")
# ---------------------------------------------------------------- 唯一写入点
def test_apply_rail_fold_is_the_single_writer_of_the_three_effects():
    """折叠任务区同时是三件事：收列表、放宽导航上限、联动展开「更多」。
    分三处写就一定会漏一处——所以要求它们集中在 applyRailFold 一个函数里。"""
    i = JS10.index("function applyRailFold(")
    body = JS10[i:JS10.index("\nfunction ", i + 10)]
    for token in ('classList.toggle("folded"', 'classList.toggle("rail-folded"',
                  "setFold(", "forceNavMoreForRail("):
        assert token in body, "applyRailFold 少了这一件事：%s" % token


def test_no_other_place_writes_the_tasksec_fold_classes():
    """别处再写一次任务区的 folded / rail-folded，就是第二个事实源
    （v1.0.636 刚为 --sbw 手抄道过歉）。
    注意 `folded` 这个词在本项目里还被别人用着——js/09 的 `.tool-body.folded` 是
    工具参数折叠，跟任务区无关，所以这里按**接收者**（taskSec 那个 sec）来判，不按类名判。"""
    hits = []
    for fn in os.listdir(JS_DIR):
        if not fn.endswith(".js"):
            continue
        src = _code_only_v637(_src_v637("js", fn))
        hits += [(fn, m.group(0)) for m in re.finditer(
            r'\w*[sS]ec\.classList\.(?:toggle|add|remove)\(\s*"folded"|classList\.toggle\(\s*"rail-folded"', src)]
    assert len(hits) == 2, "任务区折叠类被写了 %d 处（应只有 applyRailFold 里的 2 句）：%s" % (len(hits), hits)
    assert all(fn == "10-chat.js" for fn, _ in hits), hits


def test_toggle_task_rail_delegates_and_only_persists():
    body = JS10[JS10.index("function toggleTaskRail("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "applyRailFold(" in body, "toggleTaskRail 没走唯一写入点"
    assert 'lsSet("taskRailFold"' in body


# ---------------------------------------------------------------- 上限只撤折叠态
def test_nav_cap_is_lifted_only_in_the_folded_state():
    css = _code_only_v637(CSS_v637, "css")
    m = re.search(r"\.sidebar\.rail-folded \.nav \{([^}]*)\}", css)
    assert m and "max-height:none" in m.group(1), "折叠态没放宽 .nav 上限——空白只是搬进滚动条"
    base = re.search(r"\.nav \{([^}]*)\}", css).group(1)
    assert "max-height:58vh" in base, "展开态的 58vh 上限不能一起撤（窗口一矮导航会顶穿侧栏）"
    assert "min-height:158px" in base, "v1.0.591 的导航保底高度不能丢"


# ---------------------------------------------------------------- 偏好 vs 联动临时态
def test_linkage_never_writes_the_user_preference():
    """navMoreOpen 是用户偏好；任务区折叠带出来的展开是临时态，落盘就会"下次启动莫名是开的"。"""
    body = JS02[JS02.index("function setNavMoreOpen("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "persist !== false" in body, "setNavMoreOpen 无条件写偏好"
    forced = JS02[JS02.index("function forceNavMoreForRail("):]
    forced = forced[:forced.index("\n(function", 10)]
    for call in re.findall(r"setNavMoreOpen\(([^)]*)\)", forced):
        assert "false" in call, "联动里这一句会写偏好：%s" % call


def test_manual_click_takes_over_from_the_linkage():
    """用户在折叠期间手动点「更多」，联动必须让路——否则刚点就被下一次刷新盖回去。"""
    body = JS02[JS02.index("function toggleNavMore("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "__moreForced = false" in body, "toggleNavMore 没解除联动"
    unfold = JS02[JS02.index("function forceNavMoreForRail("):]
    unfold = unfold[:unfold.index("\n(function", 10)]
    assert "getItem(\"navMoreOpen\")" in unfold, "任务区展开后没回到偏好态"


def test_collapsing_the_sidebar_does_not_write_the_more_preference():
    """收成 54px 图标条会强制隐藏「更多」子菜单，但那是**视图动作**——
    原先它调 toggleNavMore()，等于"收个侧栏"就把 navMoreOpen 落盘成关闭，
    再展开时联动也接不回来（偏好已经是关闭了）。"""
    body = _code_only_v637(JS02)
    body = body[body.index("function toggleSidebar("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "toggleNavMore()" not in body, "toggleSidebar 又用手动切换来隐藏子菜单（会写偏好）"
    assert "setNavMoreOpen(false, false)" in body, "隐藏子菜单没走不写偏好的那条路"
    assert "applyRailFold(true)" in body, "重新展开图标条时没把联动补回来"


# ---------------------------------------------------------------- 快捷入口不再是被联动的一级
def test_quick_group_is_never_auto_expanded_by_the_rail():
    """v1.0.644（用户："快捷入口 放工作台按钮上面 且默认折叠，需要用户使用再去点展开按钮"）：
    v1.0.638 加的第二级填充整条撤掉。当时的判据是分组埋在「更多」里、折叠任务区是唯一把它
    掏出来的机会；现在它是导航第一行、随时可达，再自动摊开就变成"我没点它自己开了"。
    留这条锁是为了防止哪天又"顺手"把联动加回来——那块空白的代价用户已经知情。"""
    assert "forceQuickForRail" not in _code_only_v637(JS02, "js"), "快捷入口的联动展开函数又长回来了"
    assert "__quickForced" not in _code_only_v637(JS02, "js"), "又用「联动态」覆盖用户的手动折叠"
    body = JS10[JS10.index("function applyRailFold("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "forceQuickForRail" not in body, "applyRailFold 又去摊快捷入口了"
    assert "forceNavMoreForRail(fold)" in body, "第一级「更多」的接手不能一起删掉"


def test_quick_fold_always_persists():
    """没有联动之后，setQuickCollapsed 只剩"用户点的"这一种调用来源——
    再留 persist=false 这条支路就是一个永远走不到、却允许静默不写偏好的口子。"""
    body = JS02[JS02.index("function setQuickCollapsed("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "__quickFolded = collapsed" in body, "内存态没同步——重绘会把 DOM 打回原形"
    assert "applyQuickFold()" in body, "没落到 DOM"
    assert "persist" not in body, "还留着不写偏好的支路"
    assert 'lsSet("__quickFold"' in body, "折叠状态没写进偏好"


def test_more_handle_is_locked_while_the_rail_is_folded():
    """用户实报："只要把更多折叠回去以后就会出现空白"。折叠期间「更多」就是这块空间的
    唯一 occupant，收起它 = 空白立刻回来，所以两道一起上：函数里先拦（键盘/脚本也走得通），
    CSS_v637 再把箭头隐掉（不给一个点不动的钮）。"""
    body = JS02[JS02.index("function toggleNavMore("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "if(railFolded())" in body, "toggleNavMore 没有折叠期拦截"
    assert body.index("railFolded()") < body.index("setNavMoreOpen("), "拦截排在改状态之后"
    css = _code_only_v637(CSS_v637, "css")
    assert re.search(r"\.sidebar\.rail-folded \.nm-caret \{ display:none", css), \
        "折叠期间没把「更多」的箭头隐掉"


def test_quick_group_fold_is_never_locked_by_the_rail():
    """v1.0.640（用户澄清）：快捷入口与模板/技能中心/经验库是**同级入口**，只是自带一个列表，
    收起这个列表是它自己的事。v1.0.639 我把它跟「更多」一起锁了，那是锁错对象——
    任务区的状态不该没收一个平级模块的折叠能力。"""
    body = JS02[JS02.index("function toggleQuickFold("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "railFolded()" not in body, "toggleQuickFold 又被任务区锁住了"
    css = _code_only_v637(CSS_v637, "css")
    i = css.index(".sidebar.rail-folded .nm-caret")
    assert "#quickFoldBtn" not in css[i:i + 200], "折叠期间又把快捷入口的钮隐掉了"


def test_quick_group_head_reads_as_a_peer_row():
    """「平级」在界面上的落点：折叠钮在行尾（与「更多」箭头同一个位置同一个尺寸），
    行首不摆 24px 方框——否则它看着像"一个容器"而不是一条入口。
    v1.0.644 之后整行还是把手，所以行内两枚按钮必须挡住冒泡，不然点「✏️ 编辑」
    会连带把分组收起（弹窗开着、列表没了）。"""
    i = HTML_v637.index('id="quickGroup"')
    head = HTML_v637[i:HTML_v637.index("side-group-body", i)]
    btn = head[head.rindex("<button", 0, head.index("quickFoldBtn")):]
    btn = btn[:btn.index(">")]
    assert "quick-caret" in btn and "side-fold" not in btn, btn
    assert head.index("⚡ 快捷入口") < head.index("quickFoldBtn"), "折叠钮还在行首"
    assert "fold-ic" in btn, "字形仍要来自统一原语"
    assert 'class="side-group-head" onclick="toggleQuickFold()"' in head, "整行不再是把手"
    assert head.count("event.stopPropagation()") == 2, "行内按钮没挡住冒泡（编辑会顺带收起分组）"


def test_manual_quick_toggle_takes_over():
    body = JS02[JS02.index("function toggleQuickFold("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "setQuickCollapsed(!__quickFolded)" in body, "手动切换没走唯一的写入函数"
    assert "railFolded()" not in body, "v1.0.640 那条仍然成立：这颗钮不被任务区锁"


def test_fold_memory_is_restored_at_sidebar_init_not_in_list_render():
    """折叠记忆原先挂在 renderTaskRail() 末尾：列表接口没回来/拉失败时记忆整个不生效，
    而每 10 秒的静默轮询又把它重刷一遍。侧栏状态不该依赖列表数据。"""
    assert 'applyRailFold(localStorage.getItem("taskRailFold")' in _code_only_v637(JS02), \
        "initSidebarLayout 里没有折叠记忆恢复"
    js10 = _code_only_v637(JS10)
    assert 'getItem("taskRailFold")' not in js10, "renderTaskRail 还在自己读折叠记忆"
    assert 'lsSet("taskRailFold"' in js10, "toggleTaskRail 该继续写记忆"

# ============================================================ 来自 test_v645_layout_replay.py（v1.0.645）
import json
import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, ROOT)

import core                                                    # noqa: E402
from services import conversations                             # noqa: E402


def _src_v645(*rel):
    return io.open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _code(text, kind="js"):
    s = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if kind == "js":
        s = re.sub(r"(?m)^[ \t]*//.*$", " ", s)
    elif kind == "html":
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    return s


JS09, JS10 = _src_v645("js", "09-chat-render.js"), _src_v645("js", "10-chat.js")


@pytest.fixture
def tmp_ws(monkeypatch):
    # 目录**必须真叫「产物库」**、并且住在 `<base>/dist/产物库` 这层结构里：
    # resolve_workspace_ref 认「产物库/产出/x.md」是拿"运行目录的父目录 + 原样路径"去试的，
    # 而 _locatable_outside 第一条就把"软件自己那棵树"（= 产物库的父目录）整片排掉——
    # 两个条件任一不满足，测试要么测不到那条分支，要么把本该放行的桌面判成拒绝。
    base = tempfile.mkdtemp(prefix="wec_v645_")
    d = os.path.join(base, "dist", "产物库")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(core.paths, "workspace_root", lambda: d)
    monkeypatch.setattr(conversations, "workspace_root", lambda: d)
    os.makedirs(os.path.join(d, "会话"), exist_ok=True)
    return d


def _desk(tmp_ws):
    """产物库、软件目录之外的假桌面。"""
    base = os.path.dirname(os.path.dirname(tmp_ws))
    home = os.path.join(base, "Desktop")
    os.makedirs(home, exist_ok=True)
    return home


# ---------------------------------------------------------------- 存储层
def test_blocks_round_trip_and_old_shape_unchanged(tmp_ws):
    cid = conversations.create("版式复原")
    conversations.append_message(cid, "user", "帮我出一份月报")
    conversations.append_message(cid, "agent", "已生成 产物库/产出/月报.md",
                                 blocks=[{"k": "tool", "name": "write_file", "step": 1,
                                          "args": '{"path":"a.md"}', "res": '{"ok":true}',
                                          "ok": True, "path": "产物库/产出/月报.md"}])
    msgs = conversations.load(cid)["messages"]
    assert "blocks" not in msgs[0], "user 消息没传 blocks 就不该多出这个键（老会话形状不许变）"
    b = msgs[1].get("blocks")
    assert b and b[0]["k"] == "tool" and b[0]["path"].endswith("月报.md"), b


def test_clamp_blocks_caps_whitelist_and_marks_the_cut(tmp_ws):
    long_args = "x" * 9000
    blocks = [{"k": "tool", "name": "t%d" % i, "args": long_args, "step": i,
               "injected": "不该存的键"} for i in range(200)]
    blocks.insert(3, "不是 dict 的一条")
    out = conversations._clamp_blocks(blocks)
    assert len(out) == conversations._BLOCKS_MAX + 1, len(out)
    assert out[-1]["k"] == "more" and out[-1]["n"] == len(blocks) - conversations._BLOCKS_MAX, out[-1]
    assert all(len(b.get("args", "")) <= conversations._BLOCK_FIELD_MAX["args"] for b in out)
    assert all("injected" not in b for b in out), "字段白名单没生效：任意键都能跟着落盘"
    assert all(b.get("k") for b in out)


def test_render_log_never_enters_llm_context(tmp_ws):
    """blocks 是给界面重放用的，不是给模型看的。混进上下文等于把 60 步工具入参
    每轮再喂一遍——本地模型直接被顶爆（v1.0.628 就是被 20×20000 字历史撑爆那一类）。"""
    cid = conversations.create("上下文纯净")
    conversations.append_message(cid, "user", "跑一个长任务")
    conversations.append_message(cid, "agent", "完成", blocks=[
        {"k": "tool", "name": "run_cmd", "args": "y" * 3000, "res": "z" * 3000, "ok": True}])
    hist = conversations.history_messages(cid)
    assert hist and all(set(m.keys()) == {"role", "content"} for m in hist), hist
    assert "run_cmd" not in json.dumps(hist, ensure_ascii=False)


# ---------------------------------------------------------------- 采集点
def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _drain(gen):
    for _ in gen:
        pass


@pytest.fixture
def no_side_effects(monkeypatch):
    """_memory_watch 收尾会挂记忆沉淀与技能自进化——测试里一律掐掉，
    不然单测往真实产物库里写东西（这类"测试顺手改了用户数据"必须零容忍）。"""
    import services.memory_ops as mo
    import services.skill_evolve as se
    monkeypatch.setattr(mo, "remember_async", lambda *a, **k: None)
    monkeypatch.setattr(se, "maybe_distill_async", lambda *a, **k: None)


def test_memory_watch_records_the_stream(tmp_ws, no_side_effects):
    from routes.chat import _memory_watch
    cid = conversations.create("采集")
    events = [
        _sse({"type": "meta", "model": "T"}),
        _sse({"type": "reasoning", "text": "先想清楚要读哪张表" * 30}),
        _sse({"type": "tool", "name": "read_wecom_page", "args": {"pick": 1}}),
        _sse({"type": "tool_result", "name": "read_wecom_page",
              "summary": '{"ok":true,"form_progress":{"required":5,"filled":3}}'}),
        _sse({"type": "tool", "name": "save_workspace_file", "args": {"path": "产出/月报.md"}}),
        _sse({"type": "tool_result", "name": "save_workspace_file",
              "summary": '{"ok":true}', "path": "产物库/产出/月报.md"}),
        _sse({"type": "token", "text": "【完成】月报已生成"}),
        # v1.0.650（F2）：done 收据带全量任务统计——此前只有 rounds 一项被前端读过，
        # 且整条统计条从不落库，重开历史任务就凭空消失。
        _sse({"type": "done", "summary": {"rounds": 2, "tool_calls": 2, "tool_errors": 0,
                                          "batch_done": 5, "elapsed_s": 12.34,
                                          "prompt_tokens": 123456, "completion_tokens": 789}}),
    ]
    _drain(_memory_watch(iter(events), "跑月报", {}, conv_id=cid))
    agent = [m for m in conversations.load(cid)["messages"] if m["role"] == "agent"][-1]
    assert agent["content"] == "月报已生成", agent
    kinds = [b["k"] for b in agent["blocks"]]
    assert kinds == ["tool", "tool", "stat", "reason"], kinds    # 两步卡 + 统计条 + 一条推理框
    assert agent["blocks"][0]["step"] == 1 and agent["blocks"][1]["step"] == 2
    assert "form_progress" in agent["blocks"][0]["res"]        # 必填徽章有数据可算
    assert agent["blocks"][1]["path"].endswith("月报.md")      # 产物文件行有路径
    assert agent["blocks"][1]["ok"] is True
    _stat = agent["blocks"][2]
    assert (_stat["rounds"], _stat["tool_calls"], _stat["batch_done"]) == (2, 2, 5)
    assert _stat["elapsed_s"] == 12.3 and _stat["prompt_tokens"] == 123456, _stat
    assert agent["blocks"][-1]["n"] > 0, "推理字数没记下来"


def test_stat_block_ignores_bools_and_junk(tmp_ws):
    """v1.0.650 F2：统计块只收数字。bool 混进来会被前端渲染成「1 次操作」，
    字符串/None 混进来则会在 chip 上原样吐出来——都不许进 blocks。"""
    from routes.chat import _stat_block
    assert _stat_block({"summary": {"rounds": 3, "tool_calls": True, "elapsed_s": "12s",
                                    "prompt_tokens": None}}) == {"k": "stat", "rounds": 3}
    assert _stat_block({"summary": "不是字典"}) is None
    assert _stat_block({}) is None
    assert _stat_block({"summary": {"tool_errors": 0}}) == {"k": "stat", "tool_errors": 0}, \
        "0 是合法值（零失败），不能被当成缺省丢掉"


def test_memory_watch_records_the_dead_ends(tmp_ws, no_side_effects):
    """错误与「已停止」也是版式的一部分——当时气泡下面有一行红字，重开不能什么都没有。"""
    from routes.chat import _memory_watch
    cid = conversations.create("异常")
    _drain(_memory_watch(iter([
        _sse({"type": "tool", "name": "click_wecom_element", "args": {}}),
        _sse({"type": "tool_result", "name": "click_wecom_element", "summary": "失败：元素不可点"}),
        _sse({"type": "error", "text": "模型超时"}),
    ]), "点一下", {}, conv_id=cid))
    agent = [m for m in conversations.load(cid)["messages"] if m["role"] == "agent"][-1]
    assert [b["k"] for b in agent["blocks"]] == ["tool", "err"], agent["blocks"]
    assert agent["blocks"][0]["ok"] is False, "失败态没记下来 → 重开后 ❌ 变 ✅"
    assert agent["blocks"][1]["text"] == "模型超时"

    cid2 = conversations.create("停止")
    _drain(_memory_watch(iter([
        _sse({"type": "token", "text": "已经写了一半"}),
        _sse({"type": "aborted"}),
    ]), "接着跑", {}, conv_id=cid2))
    agent2 = [m for m in conversations.load(cid2)["messages"] if m["role"] == "agent"][-1]
    assert agent2["blocks"][-1]["k"] == "stopped", agent2["blocks"]


def test_capture_ignores_event_shape_but_not_content(tmp_ws, no_side_effects):
    """v1.0.628 的教训在采集层重演一次的代价：真流 yield 的是 SSE **字符串**，
    而历史单测喂的是 dict 假桩——判据只认其中一种，另一种就整轮静默什么都不记。
    `_parse_sse_event` 两种都吃，所以这里两头都钉：字符串要记，dict 也要记，
    而**非事件**（既没 type 又不是 data: 前缀）一律不记。"""
    from routes.chat import _memory_watch
    cid = conversations.create("形态")
    _drain(_memory_watch(iter([{"type": "tool", "name": "x", "args": {}}]), "u", {}, conv_id=cid))
    agent = [m for m in conversations.load(cid)["messages"] if m["role"] == "agent"][-1]
    assert [b["k"] for b in agent["blocks"]] == ["tool"], "dict 假桩形态不再被采——单测会先绿后瞎"

    cid2 = conversations.create("噪声")
    _drain(_memory_watch(iter(["heartbeat\n\n", "data: not-json\n\n",
                                _sse({"type": "meta", "model": "T"})]), "u", {}, conv_id=cid2))
    agent2 = [m for m in conversations.load(cid2)["messages"] if m["role"] == "agent"][-1]
    assert "blocks" not in agent2, "非事件（心跳/坏 JSON/meta）不该产出任何渲染步骤，" \
                                   "更不该在会话里多写一个空键"


# ---------------------------------------------------------------- 预览接口
@pytest.fixture
def client(tmp_ws):
    import backend
    return backend.app.test_client(use_cookies=False)


def _mk(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8").write(text)
    return path


def test_preview_reads_text_files_in_and_out_of_the_workspace(client, tmp_ws, monkeypatch):
    from routes import workspace as RW
    home = _desk(tmp_ws)
    monkeypatch.setattr(RW, "_outside_allow_dirs", lambda: [home])
    _mk(os.path.join(tmp_ws, "产出", "月报.md"), "# 一月\n\n- 绿 3 单")
    _mk(os.path.join(home, "付款凭证汇总.md"), "桌面那份")
    for ref in ("产物库/产出/月报.md", "产出/月报.md", os.path.join(home, "付款凭证汇总.md")):
        r = client.get("/api/workspace/file?ref=" + ref)
        d = r.get_json()
        assert r.status_code == 200 and d["ok"], (ref, d)
        assert d["content"].strip(), ref
    # 桌面之外的一律拒绝
    elsewhere = _mk(os.path.join(os.path.dirname(home), "别处", "z.md"), "不该读到")
    assert client.get("/api/workspace/file?ref=" + elsewhere).status_code == 404


def test_preview_refuses_binaries_credentials_and_strays(client, tmp_ws, monkeypatch):
    from routes import workspace as RW
    monkeypatch.setattr(RW, "_outside_allow_dirs", lambda: [_desk(tmp_ws)])
    _mk(os.path.join(tmp_ws, "产出", "x.pdf"), "%PDF-1.7")
    r = client.get("/api/workspace/file?ref=产出/x.pdf")
    assert r.status_code == 415 and r.get_json()["binary"], "PDF 该让位给「📄 打开」，不是给一段乱码"
    # 凭据与"没打算开放"的段：产物库/系统/ 是黑名单挡的，产物库/会话/ 是**预览白名单**挡的
    # （定位只开资源管理器，预览把字节读进界面——会话原文全在 会话/*.json 里）
    for rel in ("系统/api_token.txt", "会话/conv_x.json", "日志/companion.log", "别处/y.md"):
        _mk(os.path.join(tmp_ws, *rel.split("/")), "SECRET-VALUE")
        r = client.get("/api/workspace/file?ref=" + rel)
        assert r.status_code == 404, "%s 不该被预览读到：%s" % (rel, r.get_json())
    r = client.get("/api/workspace/file?ref=产出/不存在.md")
    assert r.status_code == 404


def test_legacy_sub_name_contract_still_works(client, tmp_ws):
    """产物库视图那套调用没改契约——加 ref 分支不能把它挤掉。"""
    _mk(os.path.join(tmp_ws, "产出", "旧调用.md"), "内容")
    r = client.get("/api/workspace/file?sub=产出&name=旧调用.md")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["content"] == "内容", d
    assert client.get("/api/workspace/file?sub=../../etc&name=passwd").status_code == 400


def test_user_bubble_keeps_what_was_on_screen(tmp_ws):
    """带附件那轮：气泡显示「输入 + 附件引用行」，喂模型的是另一套拼装。
    两个口径都要留——只存喂模型那份，重开就少几行；只存界面那份，上下文里就混进界面文案。"""
    cid = conversations.create("附件轮")
    conversations.append_message(cid, "user", "分析这个",
                                 display="分析这个\n\n📎 附件：产物库/上传/a.pdf")
    m = conversations.load(cid)["messages"][0]
    assert m["display"].startswith("分析这个\n\n📎"), m
    assert m["content"] == "分析这个", m
    h = conversations.history_messages(cid)
    assert h[0]["content"] == "分析这个", "display 漏进了模型上下文"
    conversations.append_message(cid, "user", "无附件的一轮")
    assert "display" not in conversations.load(cid)["messages"][-1], "没差别就别多写一个键"


def test_memory_watch_threads_the_display_text(tmp_ws, no_side_effects):
    from routes.chat import _memory_watch
    cid = conversations.create("透传")
    _drain(_memory_watch(iter([_sse({"type": "token", "text": "【完成】好"})]),
                         "分析这个", {}, conv_id=cid, user_display="分析这个\n\n📎 附件：a.pdf"))
    user = conversations.load(cid)["messages"][0]
    assert user["display"].endswith("📎 附件：a.pdf"), user
    assert user["content"] == "分析这个", user


def test_frontend_sends_and_uses_the_display_text():
    body = _code(_fn(JS10, "_renderStoredMsgs"))
    assert "m.display || m.content" in body, "重开时用户气泡没优先取界面那份文案"
    assert "body.ui_text = uiText" in _code(JS10), "发送时没把界面文案送出去"


# ---------------------------------------------------------------- 前端只有一套渲染器
def _fn(text, name):
    i = text.index("function %s(" % name)
    j = text.find("\nfunction ", i + 10)
    return text[i:j if j > 0 else len(text)]


def test_tool_result_has_a_single_owner():
    """卡片结果怎么画，只许 js/09 一处。js/10 再留一份就是这次 bug 的成因。"""
    js10 = _code(JS10)
    assert "function applyToolResult(" in _code(JS09)
    assert js10.count("applyToolResult(") >= 1, "实时流没走共用函数"
    assert js10.count('querySelector(".tool-st")') == 0, "js/10 还在自己画步骤卡状态图标"
    assert "TOOL_SUMMARY_MAX" not in js10, "摘要上限又出现第二份事实源"


def test_replay_reuses_the_live_builders():
    body = _code(_fn(JS10, "_replayAgentMsg"))
    for helper in ("addToolMsg(", "applyToolResult(", "createReasoningBox(", "addMsg("):
        assert helper in body, "%s 没被重放路径复用——那就是另写了一套渲染器" % helper
    assert "继续" not in body, "重放里不该挂「继续」：断点不随历史会话存档，点了只会报错"


def test_stored_messages_dispatch_on_blocks_and_keep_the_fallback():
    body = _code(_fn(JS10, "_renderStoredMsgs"))
    assert "_replayAgentMsg(" in body
    assert 'addMsg(key, "agent"' in body, "没有 blocks 的老会话必须有兜底渲染，不能变空白"
    assert "linkifyWorkspacePaths(" in body
    assert "m.ts" in body, "恢复历史的时间戳必须取存档值——否则重开一次所有时间都变成" \
                          "「此刻」（v1.0.615 那条口径在这里同样成立）"
    assert "toLocaleTimeString" not in body, "重放里不许现取当前时间"


def test_step_numbers_are_replayed_not_recounted():
    """步骤号是"当时第几步"，重开不能从 1 重新数——用户就是靠它对上当时那条卡。"""
    assert "o.step ||" in _code(_fn(JS09, "addToolMsg")), "opts.step 没被采纳"
    assert "step:b.step" in _code(_fn(JS10, "_replayAgentMsg"))


def test_preview_button_is_offered_only_for_text_files():
    body = _code(_fn(JS09, "_fileRefRow"))
    assert "_PREVIEW_EXT.test(" in body and '"👁 预览"' in body
    assert "data-ref" in body, "预览要拿整句路径回查后端，只留文件名就找不回文件"
    js09 = _code(JS09)
    assert "toggleFilePreview" in js09
    assert "/api/workspace/file?ref=" in js09
    # 内容按不可信处理：md 走 renderRich（内部 escapeHtml），其余走 textContent
    assert "pre.textContent = txt" in js09 or "pre.textContent=txt" in js09


def test_preview_panel_is_scrollable_and_removed_on_collapse():
    """一份几万字的操作手册不能把整条对话顶走：内容区限高内滚；再点一次整块 remove。"""
    css = _code(_src_v645("css", "style.css"), "css")
    blk = css[css.find(".file-preview .fp-body{"):]
    blk = blk[:blk.find("}")]
    assert "max-height" in blk and "overflow:auto" in blk, blk
    body = _code(_fn(JS09, "toggleFilePreview"))
    assert "opened.remove()" in body, "再点一次要整块收起，不能留占位"


# ============ 来自 v1.0.650（用户："全部优化"）——U1-U6 界面债 + 统计条落库 ============

def _v650(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


HTML650 = _v650("index.html")
CSS650 = _v650("css", "style.css")
CORE650 = _v650("js", "01-core.js")
WIN650 = _v650("js", "06-windows.js")
CHAT650 = _v650("js", "10-chat.js")
REND650 = _v650("js", "09-chat-render.js")
WS650 = _v650("js", "11-workspace-kb-study.js")
ASR650 = _v650("js", "15-asr-component.js")


def test_cardhead_is_one_class_not_inline_flex():
    """U1：卡片头逐字换行是 623 .shothead / 627 .file-ref 同一类病的第三个实例。
    修完必须收进一个类——内联 flex 抄一次漏一次，第三处就是这么冒出来的。"""
    assert ".cardhead { display:flex" in CSS650 and "flex-wrap:wrap" in CSS650
    seg = CSS650[CSS650.index(".cardhead {"):]
    seg = seg[:seg.find("}")]
    assert "flex-wrap:wrap" in seg, seg
    assert "> * { flex:0 0 auto; white-space:nowrap" in CSS650, "子项必须整项不压"
    assert "> .grow" in CSS650, "长说明文字要能显式豁免（benchCard 那句 7 项说明）"
    # 两张卡片头都改走类，不许再留内联 flex 头
    assert 'style="display:flex; align-items:center; gap:10px; margin-bottom:12px"' not in HTML650
    assert HTML650.count('<div class="cardhead">') >= 3, HTML650.count('<div class="cardhead">')
    assert 'id="usageRangeLabel">近 14 天' in HTML650   # 被压成 3 行的那颗标签还在原位


def test_window_badge_is_outside_the_ellipsis():
    """U2：徽章以前拼在 .t 里，长标题时整枚「当前/新」被省略号吃掉。"""
    assert '<span class="tt">' in WIN650, "标题段与徽章段没拆开"
    assert '${badge}</div><div class="u">' in WIN650
    seg = CSS650[CSS650.index(".win .t .tt"):]
    seg = seg[:seg.find("}")]
    assert "text-overflow:ellipsis" in seg
    bseg = CSS650[CSS650.index(".win .t .badge"):]
    bseg = bseg[:bseg.find("}")]
    assert "flex:0 0 auto" in bseg, "徽章段必须不参与收缩"
    assert "padding-right:26px" in CSS650[CSS650.index(".win .t"):][:200], "要给右上角 × 让位"


def test_dropdowns_are_keyboard_reachable():
    """U3：三个下拉的选项原来是纯 div（tabIndex=-1/role=null），Tab 进不去。"""
    assert CHAT650.count('role="menuitem" tabindex="0"') >= 3, "三类菜单项都要可聚焦"
    assert CHAT650.count('onkeydown="ddItemKey(event)"') >= 3
    assert 'function ddItemKey(ev)' in CHAT650 and 'ev.currentTarget.click()' in CHAT650
    assert HTML650.count('aria-haspopup="true"') == 6, "6 个触发钮（两套面板 × 三个下拉）"
    assert HTML650.count('role="menu"') == 6
    assert 'btn.setAttribute("aria-expanded"' in CHAT650, "开合态要同步给读屏"
    assert CHAT650.count('dd.classList.toggle("open", opening);\n  _syncDDAria();') == 3


def test_dropdown_close_is_one_function():
    """关菜单原来手抄 7 处；aria-expanded 同步要跟着一起改——收敛成 closeDDs()。"""
    assert CHAT650.count("closeDDs();") >= 7, "调用点没接上单点"
    assert CHAT650.count('querySelectorAll(".model-dd.open")') == 1, "又出现第二处各写各的关闭逻辑"


def test_row_actions_are_not_hover_only():
    """U4：任务行「⋯」与气泡复制钮原来 opacity:0——键盘焦点落在隐形按钮上。"""
    seg = CSS650[CSS650.index(".tr-more {"):]
    seg = seg[:seg.find("}")]
    assert "opacity:0" not in seg, "又改回只在 hover 显形"
    assert ".tr-more:focus-visible" in CSS650 and ".tr-item:focus-within .tr-more" in CSS650
    assert 'document.createElement("button"); cp.type="button"' in REND650, "复制钮还是 span？"
    assert 'aria-label","复制本步骤卡片文本"' in REND650
    cseg = CSS650[CSS650.index(".msg .copy {"):]
    cseg = cseg[:cseg.find("}")]
    assert "min-width:26px" in cseg and "opacity:.45" in cseg


def test_small_targets_and_focus_rings():
    """U5：实测小目标——窗口 × 20px、🌙 只有 title 没有 aria-label、搜索框 focus 只改边框。"""
    seg = CSS650[CSS650.index(".win .winx {"):]
    seg = seg[:seg.find("}")]
    assert "width:26px" in seg and "height:26px" in seg, seg
    seg = CSS650[CSS650.index(".msg-ops button {"):]
    seg = seg[:seg.find("}")]
    assert "min-height:26px" in seg, seg
    assert 'aria-label="切换深色或浅色模式"' in HTML650
    assert ".tr-search input:focus-visible" in CSS650
    assert CSS650.index(".tr-search input:focus-visible") > CSS650.index(".tr-search input:focus {")


def test_status_vocabulary_has_one_source():
    """U6：同一件"装好了"三处三种叫法两种图标——规范写在 css 注释里没法被 JS 引用。"""
    assert "const STATUS_ICON =" in CORE650 and "const STATUS_WORD =" in CORE650
    for k in ("ok", "err", "degraded", "missing", "installing", "unknown", "outdated", "na", "off"):
        assert k + ":" in CORE650[CORE650.index("const STATUS_ICON ="):CORE650.index("const STATUS_WORD =")]
    assert "STATUS_ICON.err + \" \"" in CORE650, "toast 的 err 又回到 ⚠️（规范：错误=❌、⚠️=警告）"
    assert "🟢" not in CHAT650, "跟随中又用回了不在集内的 🟢"
    # 「全部就绪」只许出现在解释性注释里，不许出现在实际拼串的代码行上
    # （按整行 grep 判会把注释自己判红——本仓已踩过两次，这次直接把口径写进锁）。
    _code_lines = [l for l in WS650.splitlines() if not l.strip().startswith(("//", "*", "/*"))]
    assert not [l for l in _code_lines if "全部就绪" in l], "同页汇总行又叫回「全部就绪」"
    assert "STATUS_WORD.ok" in WS650 and "STATUS_ICON.installing" in WS650
    assert "已安装——" not in ASR650 and "STATUS_WORD.ok" in ASR650


def test_run_stat_chip_single_builder():
    """F2 + v645 遗留：统计条以前只活在流式那一刻（前端自己累加 token），
    现在整条由 done 收据生成，且实时与重放共用同一个函数。"""
    assert "function runStatHTML(s, textLen)" in REND650
    assert "function applyRunStat(bubble, s, textLen)" in REND650
    assert "csS.summary" in CHAT650 and "applyRunStat(lastAgentS" in CHAT650
    assert 'else if(b.k==="stat") stat=b;' in CHAT650, "重放没接统计块"
    assert "applyRunStat(el, stat," in CHAT650
    # 前端自计的那份已经删干净（只剩注释里提到过）
    code_lines = [l for l in CHAT650.splitlines()
                  if "__msgStats" in l and not l.strip().startswith("//")]
    assert not code_lines, code_lines
    for f in ("elapsed_s", "tool_calls", "rounds", "prompt_tokens", "completion_tokens",
              "tool_errors", "batch_done"):
        assert f in REND650[REND650.index("function runStatHTML"):], f


def test_run_stat_shows_nothing_it_cannot_replay():
    """服务端没回 usage 时不假装有 token——改显示答案字数（两边都能从同一文本算出来）。"""
    body = REND650[REND650.index("function runStatHTML"):]
    body = body[:body.find("\nfunction applyRunStat")]
    assert "!out && textLen" in body, "没有 usage 时的字数退路被删了"
    assert "__msgStats" not in body and "Date.now()" not in body, "chip 不许再依赖墙上时间"


def test_folded_space_goes_to_the_task_list_not_into_a_dead_band():
    """v1.0.644 遗留的 206px 空白：按用户真实数据规模复测后**不是缺陷**。

    当初那 206px 是在只有几条会话的种子里量出来的。本机 dist 实测 99 个会话文件，
    任务列表本来就超出可视高度在滚——快捷入口折叠让出来的高度会被 .tasksec 整块吃掉
    （多显示几行任务），不会出现空白带。所以这一版不改布局，只把"空间必须归任务列表"
    这条判据钉住：.nav 不许 grow（它自己会滚），.tasksec 必须 grow 且没有高度上限。
    """
    seg = CSS650[CSS650.index(".nav {"):]
    seg = seg[:seg.find("}")]
    assert "flex:0 1 auto" in seg, "导航一 grow 就会把任务列表挤扁"
    t = CSS650[CSS650.index(".tasksec {"):]
    t = t[:t.find("}")]
    assert "flex:1 1 auto" in t, "任务区不 grow = 折叠让出来的空间变成空白带"
    assert "max-height:none" in t, "又给任务区加高度上限 = 空间挪进滚动条而不是列表"
    assert "min-height:140px" in t
