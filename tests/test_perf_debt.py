# -*- coding: utf-8 -*-
"""tests/test_perf_debt.py — 按域归并的回归锁合集

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
import ast
import threading
import zipfile


# ============================================================ 来自 test_v614_debt.py（v1.0.614）
import io
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def rd(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ---------- ⑲ 预览截断收敛 ----------

def test_read_preview_truncates_and_formats(tmp_path):
    from core.files import read_preview, PREVIEW_LIMIT
    big = tmp_path / "big.txt"
    big.write_text("x" * (PREVIEW_LIMIT + 1024), encoding="utf-8")
    content, size, truncated = read_preview(str(big))
    assert truncated and size == PREVIEW_LIMIT + 1024
    assert len(content) <= PREVIEW_LIMIT + 80 and "仅预览前" in content
    c2, _, _ = read_preview(str(big), truncated_hint="A{size}B{limit}")
    assert c2.endswith("A%dB%d" % (size, PREVIEW_LIMIT))


def test_small_file_not_truncated(tmp_path):
    from core.files import read_preview
    p = tmp_path / "s.txt"
    p.write_text("hello", encoding="utf-8")
    content, size, truncated = read_preview(str(p))
    assert (content, size, truncated) == ("hello", 5, False)


def test_preview_limit_defined_once():
    bad = [rel for rel in ("routes/knowledge.py", "routes/workspace.py")
           if re.search(r"5\s*\*\s*1024\s*\*\s*1024", rd(rel))]
    assert not bad, "预览阈值又被人各写一份：%s" % bad
    assert "PREVIEW_LIMIT = 5 * 1024 * 1024" in rd("core/files.py")


# ---------- ⑯ 上传体积判定要在读之前 ----------

def test_asr_rejects_oversize_before_reading():
    import backend as B
    B.app.config["TESTING"] = True
    payload = b"R" * (21 * 1024 * 1024)
    boundary = "wc614"
    body = (("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.webm\"\r\n"
             "Content-Type: audio/webm\r\n\r\n" % boundary).encode()
            + payload + ("\r\n--%s--\r\n" % boundary).encode())
    with B.app.test_client() as c:
        r = c.post("/api/asr/transcribe", data=body,
                   content_type="multipart/form-data; boundary=" + boundary)
    assert r.status_code == 413, r.status_code
    assert "20MB" in r.get_json()["error"], r.get_json()


def test_asr_source_checks_size_before_read():
    s = rd("routes/b_admin.py")
    i = s.find("def asr_transcribe")
    seg = s[i:i + 1200]
    assert "f.content_length" in seg, "没在读取前按 content_length 拦"
    # 按"行"比顺序，别用 index()——注释里就写着旧代码 `data = f.read()`，
    # 字符串检索会先撞上注释（本项目栽过第三次的同一件事）。
    lines = [l for l in seg.split("\n")]
    at = next(n for n, l in enumerate(lines) if "content_length" in l and not l.strip().startswith("#"))
    rt = next(n for n, l in enumerate(lines) if l.strip().startswith("data = f.read("))
    assert at < rt, "体积判定仍排在整块 read 之后"


# ---------- ㉗ 看板转义 ----------

def test_dashboard_cells_are_escaped():
    s = rd("js/13-dashboard.js")
    i = s.find('el.innerHTML = `<div class="dc-t">')
    assert i > 0, "看板渲染点找不到了"
    seg = s[i:i + 300]
    assert seg.count("escapeHtml(") == 3, "三格内容必须各转义一次：%s" % seg
    for field in ("c.t", "c.v", "c.s"):
        assert field in seg, "漏了 %s" % field


# ---------- ㉖ 控件一致性：停止按钮 / 关闭符号 ----------

def test_stop_buttons_are_consistent():
    h = rd("index.html")
    stops = re.findall(r"<button[^>]*>([^<]*停止[^<]*)</button>", h)
    assert stops, "找不到停止按钮"
    for label in stops:
        assert label.strip().startswith("⏹"), "停止按钮少了统一的状态图标：%r" % label
    for m in re.finditer(r"<button[^>]*id=\"(?:opStop|setStop|runStopBtn|studyStopBtn)\"[^>]*>", h):
        assert 'class="ghost"' in m.group(0), "停止按钮 class 不统一：%s" % m.group(0)


def test_close_glyph_unified():
    h = rd("index.html")
    bad = [x for x in re.findall(r">([×❌✕])</button>", h) if x != "✕"]
    assert not bad, "关闭符号还有 %s 两套（统一用 ✕）" % set(bad)


def test_clear_button_has_title_on_both_panes():
    h = rd("index.html")
    lines = [l for l in h.split("\n") if "clearChat(" in l and "<button" in l]
    assert len(lines) >= 2, "两个 composer 的「清空」按钮至少要有两个"
    for l in lines:
        assert "title=" in l, "「清空」缺 title（不可恢复操作必须说清后果）：%s" % l.strip()[:90]


# ---------- ㉓ 停止/超时不再冒充错误 ----------

def test_stop_and_timeout_use_notice_not_err():
    s = rd("js/08-templates.js")
    assert 'class="msg err">已手动停止' not in s
    assert 'class="msg notice">已手动停止' in s
    assert 'class="msg notice">模板运行超时' in s
    css = rd("css/style.css")
    m = re.search(r"\.msg\.notice\s*\{([^}]*)\}", css)
    assert m, ".msg.notice 没定义"
    for t in ("--info-bg", "--info-bd", "--info-fg"):
        assert "var(%s)" % t in m.group(1)


# ---------- ㉕ 录音/转写状态可见 ----------

def test_mic_status_is_visible_not_only_tooltip():
    s = rd("js/14-mic.js")
    assert re.search(r'btn\.textContent\s*=\s*"⏺"\s*\+\s*sec', s), "录音秒数没显示在按钮上"
    assert "⏳转写" in s, "转写中状态不可见"


# ---------- ㉔ 超长会话折叠提示 ----------

def test_trim_pane_leaves_a_hint():
    js = rd("js/09-chat-render.js")
    i = js.find("function trimPane")
    seg = js[i:i + 900]
    assert "cut" in seg and "pane-trimmed" in seg, "裁掉气泡后没有任何提示"
    assert "style=" not in seg, "提示行不许写行内样式（走 .pane-trimmed 类）"
    assert ".pane-trimmed" in rd("css/style.css")


# ---------- ⑰ 开机后台任务让位界面空闲 ----------

def test_idle_gate_behaviour():
    from core import idle
    idle._last["ts"] = 0.0
    assert idle.idle_seconds() > 1000, "从未有请求时应视为空闲"
    idle.touch()
    assert idle.idle_seconds() < 2
    waited = idle.wait_until_idle(min_wait=0.0, idle_for=0.2, deadline=3.0, poll=0.05)
    assert 0.15 <= waited <= 3.0, waited
    # 界面一直在动 → 到 deadline 就收手，不能无限等
    idle.touch()
    w2 = idle.wait_until_idle(min_wait=0.0, idle_for=30.0, deadline=0.4, poll=0.05)
    assert w2 < 1.5, "没有 deadline 兜底，自检会永远跑不上"


def test_startup_housekeeping_waits_for_idle():
    s = rd("core/ocr.py")
    for fn in ("def ocr_selfcheck_async", "def ocr_update_check_async"):
        i = s.find(fn)
        seg = s[i:i + 900]
        assert "wait_until_idle" in seg, "%s 没等界面空闲" % fn
    b = rd("backend.py")
    assert "idle.touch()" in b, "before_request 没记界面活动"


# ---------- ㉒ JS 拼的弹窗补无障碍 + Esc 只接管可背景关闭的 ----------

def test_modal_veils_get_role_and_esc():
    js = rd("js/12-init.js")
    assert "armDialogVeils" in js and "MutationObserver" in js
    assert "aria-modal" in js and "'dialog'" in js
    i = js.find('if(e.key==="Escape")')
    seg = js[i:i + 1200]
    assert "modal-veil" in seg, "Esc 仍不接管 .modal-veil 弹窗"
    assert 'typeof v.onclick==="function"' in seg, \
        "Esc 必须只接管允许点背景关闭的弹窗（审批弹窗不能被 Esc 顺手拒绝）"

# ============================================================ v1.0.648 文件解析分派（拆函数前的行为锁）
class TestFileParseDispatch:
    """v1.0.648 要把 core/files.py::_parse_file_content（247 行，按扩展名一长串
    if/elif）拆成分派表 + 每格式一个 helper。拆之前先把**对外可观测的行为**钉住：
    返回三元组的形状、每种格式打出的 kind、失败时的 (None, None, "（解析失败：…）")、
    以及老格式转换的前缀拼接。这些是调用方（routes/workspace.py:103、
    agent_tools/executors.py:270）唯一依赖的东西。

    锁的重点是"拆完必须还长这样"——不是测各解析库本身（那属于第三方），
    而是测分派与返回值契约。
    """

    def _mk(self, tmp_path, name, data=b"hello"):
        f = tmp_path / name
        f.write_bytes(data)
        return str(f)

    def test_text_extension_returns_ext_as_kind(self, tmp_path):
        from core.files import _parse_file_content
        p = self._mk(tmp_path, "a.md", "标题\n正文".encode("utf-8"))
        kind, content, note = _parse_file_content(p, 10)
        assert kind == "md" and "标题" in content

    def test_unknown_binary_is_reported_not_guessed(self, tmp_path):
        """未知类型不能装作读出来了：无文本或含 \\x00 时 content 必须为空 + 明确 note。"""
        from core.files import _parse_file_content
        p = self._mk(tmp_path, "x.bin", b"\x00\x01\x02\x03")
        kind, content, note = _parse_file_content(p, 4)
        assert kind == "unknown" and content == ""
        assert "二进制或无法解析" in note

    def test_failure_returns_triple_none_with_reason(self, tmp_path):
        """解析抛异常时是 (None, None, 原因)——调用方靠 None 判失败，不是靠空串。"""
        from core.files import _parse_file_content
        p = self._mk(tmp_path, "broken.pdf", b"not a pdf at all")
        kind, content, note = _parse_file_content(p, 16)
        assert kind is None and content is None
        assert note.startswith("（解析失败：")

    def test_legacy_office_without_office_installed(self, tmp_path, monkeypatch):
        """老格式：转换失败时不解析、不伪装成功，且提示里要写明目标扩展名。"""
        import core.files as F
        monkeypatch.setattr(F, "convert_legacy_office", lambda src: None)
        p = self._mk(tmp_path, "old.doc", b"\xd0\xcf\x11\xe0")
        kind, content, note = F._parse_file_content(p, 4)
        assert kind == "legacy-office" and content == ""
        assert ".docx" in note and "Office/WPS" in note

    def test_legacy_office_converted_recurses_and_prefixes_note(self, tmp_path, monkeypatch):
        """转换成功 → 递归解析新文件，并在 note 前拼转换说明（顺序不能反）。"""
        import core.files as F
        new = self._mk(tmp_path, "old.docx", b"PK\x03\x04")
        monkeypatch.setattr(F, "convert_legacy_office", lambda src: new)
        # 转换产物的真实解析结果不属于本锁（第三方库行为），这里只钉住
        # 「递归进新文件 + note 前缀拼接」这一层，所以把 docx helper 顶掉。
        monkeypatch.setattr(F, "_parse_docx", lambda p: ("docx", "转换后正文", ""))
        kind, content, note = F._parse_file_content(str(tmp_path / "old.doc"), 4)
        assert kind == "docx"
        assert note.startswith("（检测到老版本 .doc 格式，已自动转换为 .docx 后解析）")

    def test_dispatch_covers_the_same_extension_set(self):
        """分派必须与拆之前覆盖同一批扩展名——拆的时候最容易漏掉冷门那几个
        （.docm/.xlsm/.tif/.webp/.tsx/.log 之类的邻居）。

        口径：扩展名现在分散在三处——_PDF_EXT/_IMAGE_EXT/_TEXT_EXT 三个常量，
        加上分派函数里内联的那几个（.doc/.xls/.rtf/.docx/.xlsx/.pptx）。
        所以从 AST 里把这三处字面量全收出来再比，而不是只扫函数体文本——
        否则"把扩展名搬进常量"会被误判成"漏了扩展名"，这条锁就成了假红。"""
        src = io.open(os.path.join(ROOT, "core", "files.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        covered = set()

        def _strings(node):
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    covered.update(v for v in n.value.split() if v.startswith("."))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id in ("_PDF_EXT", "_IMAGE_EXT", "_TEXT_EXT"):
                _strings(node.value)
            if isinstance(node, ast.FunctionDef) and node.name == "_parse_file_content":
                _strings(node)
        for ext in (".pdf", ".doc", ".xls", ".rtf", ".png", ".jpg", ".jpeg", ".bmp",
                    ".tif", ".tiff", ".webp", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx",
                    ".txt", ".csv", ".md", ".json", ".py", ".js", ".html", ".xml", ".log",
                    ".ini", ".yaml", ".yml", ".sql", ".sh", ".bat", ".ts", ".tsx", ".css"):
            assert ext in covered, f"分派漏了 {ext}（拆分时最容易掉的冷门扩展名）"
        assert "_LEGACY_OFFICE_MAP" in src

    def test_helpers_exist_per_format(self):
        """拆完的形态：每格式一个 helper，分派函数自己不许再超过 100 行
        （否则巨型函数棘轮照样会咬，这次拆分等于白拆）。"""
        import core.files as F
        for name in ("_parse_pdf", "_parse_doc", "_parse_xls", "_parse_rtf",
                     "_parse_image", "_parse_docx", "_parse_xlsx", "_parse_pptx",
                     "_parse_textish", "_parse_unknown"):
            assert hasattr(F, name), f"缺少 helper：{name}"
        import ast
        tree = ast.parse(io.open(os.path.join(ROOT, "core", "files.py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_parse_file_content":
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                assert span <= 100, f"_parse_file_content 拆完仍有 {span} 行"

# ============================================================ 来自 test_v616_debt.py（v1.0.616）
import os
import re
import sys
import threading
import time
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def body(rel, fn):
    """取某个顶层/方法函数的源码块（按 def 行缩进切片），避免整文件字符串检索撞注释。"""
    s = rd(rel)
    m = re.search(r"^(?:    )?def %s\(" % re.escape(fn), s, re.M)
    assert m, "找不到函数 %s（%s）" % (fn, rel)
    lines = s[m.start():].split("\n")[1:]
    start_indent = len(s[m.start():].split("\n")[0]) - len(s[m.start():].split("\n")[0].lstrip())
    out = []
    for l in lines:
        if l.strip() and (len(l) - len(l.lstrip())) <= start_indent and not l.strip().startswith(
                (')', ']', '}', '#')):
            break
        out.append(l)
    return "\n".join(out)


# ---------- ① 收纳/差分开页白名单：唯一事实源 ----------

def test_keep_marks_have_one_home():
    from wecom_bridge import windows as W
    import services.batch_flow as bf
    assert isinstance(W.KEEP_URL_MARKS, tuple) and len(W.KEEP_URL_MARKS) >= 4
    assert "tpp-web" in W.KEEP_URL_MARKS, "批审列表域必须在名单里"
    # 别名必须是同一个对象（不是复制一份），否则改一漏一
    assert bf._KEEP_URL_PAT is W.KEEP_URL_MARKS
    assert W._SILENT_KEEP_URL_MARKS is W.KEEP_URL_MARKS


def test_no_second_keep_list_literal():
    """第二份"看起来像名单"的字面量（同一条里既含批审域又含文档域）不许再出现。"""
    bad = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in
                       ("build", "dist", "tests", "docs", "__pycache__", ".git")]
        if os.path.basename(dirpath) in ("build", "dist"):
            continue
        for fn in filenames:
            if not fn.endswith((".py", ".js", ".html")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = io.open(p, encoding="utf-8").read()
            except Exception:  # noqa: BLE001
                continue
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            for line in txt.split("\n"):
                if "tpp-web" in line and "doc.weixin.qq.com" in line:
                    bad.append(rel)
    assert bad == ["wecom_bridge/tucker.py"], "白名单又被抄了一份：%s" % bad


# ---------- ② Content-Disposition 取名：唯一解码器 ----------

def test_name_from_cd_decodes_rfc5987():
    from wecom_bridge.download import _name_from_cd
    assert _name_from_cd("attachment; filename*=UTF-8''%E6%8A%A5%E9%94%80%E5%8D%95.pdf") == "报销单.pdf"
    assert _name_from_cd('attachment; filename="%E5%8F%B0%E8%B4%A6.xlsx"') == "台账.xlsx"
    assert _name_from_cd("attachment; filename=plain.txt") == "plain.txt"
    assert _name_from_cd("") == ""


def test_name_from_cd_kills_traversal():
    from wecom_bridge.download import _name_from_cd
    assert _name_from_cd('attachment; filename="../../windows/system32.exe"') == "system32.exe"
    assert "/" not in _name_from_cd('attachment; filename="a/../b.pdf"')


def test_download_file_reuses_the_decoder():
    b = body("agent_tools/executors.py", "_run_download_file")
    assert "from wecom_bridge.download import _name_from_cd" in b
    assert "_name_from_cd(" in b
    # 不许再就地抄一份裸正则（旧写法：re.search(r"filename…"）
    assert not re.search(r're\.(?:search|findall)\(\s*[\'"]\\?f?i?l?e?a?n?a?m', b), \
        "_run_download_file 里又出现了自己解析 Content-Disposition 的正则"


# ---------- ③ 解压一律过 zip-slip 校验 ----------

def test_safe_extract_blocks_slip(tmp_path):
    from core.portable import _safe_extract
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(zp), "w") as z:
        z.writestr("../escaped.txt", "boom")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RuntimeError) as e:
        _safe_extract(zipfile.ZipFile(str(zp)), str(dest))
    assert "越界" in str(e.value)
    assert not (tmp_path / "escaped.txt").exists(), "拦截前已经把文件写到目录外了"


def test_safe_extract_allows_normal_members(tmp_path):
    from core.portable import _safe_extract
    zp = tmp_path / "ok.zip"
    with zipfile.ZipFile(str(zp), "w") as z:
        z.writestr("pkg/mod.py", "print(1)")
    dest = tmp_path / "out2"
    dest.mkdir()
    _safe_extract(zipfile.ZipFile(str(zp)), str(dest))
    assert (dest / "pkg" / "mod.py").exists()


def test_asr_bootstrap_uses_safe_extract():
    b = body("services/asr.py", "install_component")
    assert "from core.portable import _safe_extract" in b
    assert "_safe_extract(zf, pip_dir)" in b
    assert "extractall(" not in b, "asr 里又出现裸 extractall"


def test_ocr_inline_check_precedes_extract():
    s = rd("core/ocr.py")
    guard = s.find('raise RuntimeError(f"wheel 含越界路径条目')
    extract = s.find("z.extractall(d)")
    assert 0 < guard < extract, "OCR 的 zip-slip 判定必须排在解压之前"


# ---------- ④ OCR 安装的 %TEMP% 残留 ----------

def test_ocr_sweeps_temp_before_downloading():
    b = body("core/ocr.py", "_ocr_install_worker")
    lines = b.split("\n")

    def first(pred):
        return next((n for n, l in enumerate(lines)
                     if pred(l) and not l.strip().startswith("#")), None)

    made = first(lambda l: 'os.path.join(tempfile.gettempdir(), "WeComCompanion_ocr_dl")' in l)
    swept = first(lambda l: "os.listdir(tmp)" in l)
    got = first(lambda l: "requests.get(url" in l)
    assert made is not None and swept is not None and got is not None, (made, swept, got)
    assert made < swept < got, "残留清理必须排在建目录之后、真正下载之前"
    assert 'os.remove(os.path.join(tmp, _stale))' in b, "清理只删本目录里的文件（不许 rmtree 整个临时根）"


def test_ocr_failure_paths_remove_wheel():
    b = body("core/ocr.py", "_ocr_install_worker")
    i = b.find("if not _pyver_satisfies(req, cur_py):")
    assert i > 0
    seg = b[i:i + 1200]
    assert "os.remove(wp)" in seg.split("return")[0], \
        "Requires-Python 中止这条失败路径仍然不清 wheel"


# ---------- ⑤ 看板缓存：读写同锁 ----------

class _LockSpy:
    """替身锁：记录每次进入的时刻，用来证明"读侧进过锁 / 写侧也进过锁"。"""

    def __init__(self):
        self._lk = threading.Lock()
        self.hits = 0
        self.busy = threading.Event()

    def __enter__(self):
        self._lk.acquire()
        self.hits += 1
        return self

    def __exit__(self, *a):  # noqa: ANN002
        self._lk.release()
        return False


@pytest.fixture()
def dash_client(tmp_path, monkeypatch):
    import backend as backend_mod
    import routes.b_admin as ba
    import services.conversations as conv_mod
    monkeypatch.setattr(ba, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(conv_mod, "workspace_root", lambda: str(tmp_path))
    ba._DASH_CACHE["ts"] = 0.0
    ba._DASH_CACHE["data"] = None
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        yield c, ba


def test_dashboard_cache_hit_still_takes_the_lock(dash_client, monkeypatch):
    c, ba = dash_client
    c.get("/api/dashboard")            # 先填一次缓存
    spy = _LockSpy()
    monkeypatch.setattr(ba, "_DASH_LOCK", spy)
    r = c.get("/api/dashboard")
    assert r.status_code == 200
    assert spy.hits == 1, "缓存命中没走 _DASH_LOCK（裸读能撞上序列化期的写）"


def test_dashboard_write_takes_the_lock(dash_client, monkeypatch):
    c, ba = dash_client
    spy = _LockSpy()
    monkeypatch.setattr(ba, "_DASH_LOCK", spy)
    r = c.get("/api/dashboard")
    assert r.status_code == 200
    assert spy.hits == 2, "读一次 + 写一次：写缓存没进锁，就是半截数据能被别人读走"


def test_dashboard_blocks_while_another_thread_holds_the_lock(dash_client):
    c, ba = dash_client
    c.get("/api/dashboard")            # 保证走缓存命中这条最短路径
    released, held = threading.Event(), threading.Event()

    def hog():
        with ba._DASH_LOCK:
            held.set()
            released.wait(1.2)     # 最多占 1.2s：够判定"请求有没有等锁"，又不拖慢全量回归

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert held.wait(2)
    t0 = time.time()
    r = c.get("/api/dashboard")
    waited = time.time() - t0
    released.set()
    t.join(5)
    assert r.status_code == 200
    assert waited > 0.2, "请求没有等锁——读侧仍是裸的（waited=%.3f）" % waited


# ---------- ⑥ 刷课兜底日志：每处每轮一条 ----------

def test_quiet_logs_once_per_site(monkeypatch):
    import services.study_engine as se
    lines = []
    monkeypatch.setattr(se, "log_event", lambda s: lines.append(s))
    se._QUIET_LOGGED.clear()
    se._quiet("probe_a", ValueError("boom"))
    se._quiet("probe_a", ValueError("boom"))
    se._quiet("probe_a", ValueError("boom"))
    assert len(lines) == 1, "同一处兜底记了 %d 条（一次刷课会灌进成千上万行）" % len(lines)
    se._quiet("probe_b", RuntimeError("nope"))
    assert len(lines) == 2
    assert "probe_b" in lines[1] and "RuntimeError" in lines[1], "日志里要能认出是哪一处、什么异常"


def test_quiet_is_reset_each_run():
    src = body("services/study_engine.py", "start")
    assert "_QUIET_LOGGED.clear()" in src, "引擎重跑时不清计数，第二轮就一条兜底日志都没有"


def test_quiet_covers_the_probe_helpers():
    s = rd("services/study_engine.py")
    sites = set(re.findall(r'_quiet\("([^"]+)"', s))
    assert len(sites) >= 8, "CDP 探测兜底只instrument了 %d 处：%s" % (len(sites), sorted(sites))
    assert all("def " not in x for x in sites)


def test_quiet_never_raises_when_logging_fails(monkeypatch):
    import services.study_engine as se

    def boom(_s):
        raise OSError("日志盘满了")

    monkeypatch.setattr(se, "log_event", boom)
    se._QUIET_LOGGED.clear()
    se._quiet("probe_x", ValueError("x"))   # 不许把兜底路径本身炸了


# ============================================================ C 档：前端 fetch 样板收敛（js/ API 层）
# 背景：js/ 曾有 165 处裸 fetch 手抄（含 77 处同款 catch+toast 样板），错误口径随抄随漂。
# 收敛后 01-core.js 只有一份契约：api()（非 JSON 响应按状态码判；d.error / d.ok===false 抛错），
# getJson/postJson/apiToast 都是它的薄封装。保留的裸 fetch 是有意的——FormData 上传、
# SSE/流式、带超时的 AbortController、r.status/自定义字段分支等 api() 契约盖不住的站点。

def test_json_wrappers_sit_on_top_of_api():
    """getJson/postJson 必须复用 api() 的同一契约——另写一份 ok/error 判定就是第二次漂移。"""
    core = rd("js/01-core.js")
    assert "async function getJson(" in core and "async function postJson(" in core
    assert "return api(url);" in core, "getJson 又自己写了一套 fetch+判定"
    assert "async function apiToast(" in core and "toast(fetchErr(e, action" in core
    assert core.count("fetch(") <= 2, "01-core 的 fetch 只许留在 api() 与流式请求内部：%d" % core.count("fetch(")


def test_bare_fetch_is_ratcheted():
    """裸 fetch 只许减不许增——新代码一律走 getJson/postJson/api/apiToast，
    否则 77 处手抄样板的错误口径漂移会原地长回来。"""
    total = 0
    for fn in sorted(os.listdir(os.path.join(ROOT, "js"))):
        if fn.endswith(".js"):
            total += rd("js/" + fn).count("fetch(")
    assert total <= 56, "裸 fetch 从 64 收敛到 56（A/B 类再转 8 处后）又涨回 %d 处" % total


def test_api_layer_actually_absorbed_the_boilerplate():
    """收敛是真用上了，不是只加了封装没人调——三个入口的调用点都要在场。"""
    calls = {"apiToast(": 0, "getJson(": 0, "postJson(": 0}
    for fn in sorted(os.listdir(os.path.join(ROOT, "js"))):
        if fn.endswith(".js"):
            s = rd("js/" + fn)
            for k in calls:
                calls[k] += s.count(k)
    # 每个计数含 01-core 里的 1 处定义，减掉后是纯调用点
    assert calls["apiToast("] - 1 >= 20, calls
    assert calls["getJson("] - 1 >= 20, calls
    assert calls["postJson("] - 1 >= 5, calls
