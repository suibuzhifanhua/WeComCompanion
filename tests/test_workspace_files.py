# -*- coding: utf-8 -*-
"""tests/test_workspace_files.py — 按域归并的回归锁合集

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


# ============================================================ 来自 test_v563_save_file_chain.py（v1.0.563）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.paths  # noqa: E402
from core import workspace_io  # noqa: E402
import agent_tools.executors as EX  # noqa: E402


def test_executors_has_save_workspace_file_binding():
    """① 调用点绑定护栏：executors 模块必须能直接引用 save_workspace_file
    （属性查找断链在 import 期不炸、调用期才炸，必须显式锁）。"""
    assert hasattr(EX, "save_workspace_file"), \
        "executors 的 save_workspace_file 绑定丢失（v1.0.555 拆分断链复发）"
    assert callable(EX.save_workspace_file)


def test_run_save_file_real_write_and_ledger():
    """② save_file 端到端真实落盘：产物库/产出 写入成功 + 台账登记。"""
    fname = f"__save_chain_test_{os.getpid()}.md"
    try:
        out = EX._run_save_file(
            {"subdir": "产出", "filename": fname, "content": "# 断链回归\n内容", "note": "测试"},
            {})
        assert isinstance(out, dict) and out.get("ok"), f"save_file 应成功：{out}"
        path = out["path"]
        assert os.path.isfile(path)
        with io.open(path, encoding="utf-8") as f:
            assert "断链回归" in f.read()
        # 台账登记（append_ledger 追加到 产物台账.md）
        ledger = os.path.join(workspace_io.paths.ws_sub("产出"), "产物台账.md")
        assert os.path.isfile(ledger) and "save_file" in io.open(ledger, encoding="utf-8").read()
    finally:
        for p in (out.get("path") if isinstance(out, dict) else None,):
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass


def test_run_save_workspace_file_append_mode():
    """③ save_workspace_file 追加模式（答题记录/进度流水的核心路径）。"""
    fname = f"__append_chain_test_{os.getpid()}.md"
    try:
        r1 = EX._run_save_workspace_file(
            {"subdir": "产出", "filename": fname, "content": "行1\n", "mode": "append"}, {})
        r2 = EX._run_save_workspace_file(
            {"subdir": "产出", "filename": fname, "content": "行2\n", "mode": "append"}, {})
        assert r1.get("ok") and r2.get("ok"), (r1, r2)
        with io.open(r2["path"], encoding="utf-8") as f:
            content = f.read()
        assert "行1" in content and "行2" in content, f"追加模式应保留两行：{content!r}"
    finally:
        try:
            if r2.get("path") and os.path.isfile(r2["path"]):
                os.remove(r2["path"])
        except OSError:
            pass


def test_wecom_route_download_binding():
    """④ routes/wecom 的下载落盘调用点同样绑定（同源断链）。"""
    import routes.wecom as W
    assert hasattr(W, "save_workspace_file") and callable(W.save_workspace_file)


# ============================================================ 来自 test_v627_file_ref.py（v1.0.627）


import routes.workspace as W     # noqa: E402
from core import paths            # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "dist" / "产物库"
    for sub in ("产出", "下载", "日志", "系统"):
        (root / sub).mkdir(parents=True)
    (root / "产出" / "月报检查_20261026.md").write_text("x", encoding="utf-8")
    (root / "产出" / "a.md").write_text("x", encoding="utf-8")
    (root / "下载" / "a.md").write_text("x", encoding="utf-8")   # 重名：裸文件名不该猜
    (root / "系统" / "api_token.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(paths, "workspace_root", lambda: str(root))
    monkeypatch.setattr(W.paths, "workspace_root", lambda: str(root), raising=False)
    return root


# ---------------- 解析与边界 ----------------

def test_relative_and_prefixed_and_abs_all_resolve(ws):
    root = str(ws)
    for ref in ("产出/月报检查_20261026.md",
                "产物库/产出/月报检查_20261026.md",
                os.path.join(root, "产出", "月报检查_20261026.md"),
                os.path.join(root, "产出", "..", "产出", "月报检查_20261026.md"),
                "月报检查_20261026.md"):
        got = W.resolve_workspace_ref(ref)
        assert got and got.endswith("月报检查_20261026.md"), (ref, got)


def test_traversal_and_outside_are_refused(ws):
    outside = str(ws.parent / "config.json")          # 产物库**同级**的 config.json
    ws.parent.joinpath("config.json").write_text("{}", encoding="utf-8")
    for ref in ("../config.json", "产物库/../config.json", outside,
                "C:/Windows/System32/cmd.exe", "产出/../../dist/config.json",
                "", None, "产出/不存在.md\x00evil", "  "):
        assert W.resolve_workspace_ref(ref) is None, ref
    # 目录本身（产物库内可见子目录）可以定位——「跳到产出目录」也是用户的正当需求
    assert W.resolve_workspace_ref("产出") and W.resolve_workspace_ref("产出").endswith("产出")


def test_hidden_system_dir_is_not_locatable(ws):
    """`系统/`（api_token 所在地）不在 tree_subs 可见清单里，因此定位不到——
    聊天里就算被诱导写出 `系统/api_token.txt` 也不会被打开。"""
    assert "系统" not in paths.tree_subs()
    assert W.resolve_workspace_ref("系统/api_token.txt") is None


def test_ambiguous_bare_name_is_not_guessed(ws):
    assert W.resolve_workspace_ref("a.md") is None, "两个子目录里都有 a.md，猜一个就是开错文件"


# ---------------- 路由 ----------------

def test_route_reveal_and_open_do_not_launch_real_shell(ws, monkeypatch):
    import subprocess
    launched = []
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p))
    import backend as B
    with B.app.test_request_context("/api/workspace/locate", method="POST",
                                    json={"ref": "产出/月报检查_20261026.md"}):
        r = W.api_workspace_locate()
    body = r.get_json()
    assert body["ok"] is True and body["action"] == "reveal", body
    assert launched and launched[0][0][0] == "explorer", launched
    assert launched[0][0][1].startswith("/select,"), "没带 /select, 就只是打开文件夹、选不中文件"
    assert launched[0][0][2].endswith("月报检查_20261026.md")
    started.clear()
    with B.app.test_request_context("/api/workspace/locate", method="POST",
                                    json={"ref": "产出/a.md", "action": "open"}):
        r2 = W.api_workspace_locate()
    assert r2.get_json()["ok"] is True and started, "open 应走 startfile"


def test_route_rejects_outside_path(ws):
    """越界一律 404，且响应体是 Flask 元组（直接调视图时 jsonify,code 的常见形态）。"""
    import backend as B
    with B.app.test_request_context("/api/workspace/locate", method="POST",
                                    json={"ref": "../../Windows/win.ini"}):
        r = W.api_workspace_locate()
    resp, code = r if isinstance(r, tuple) else (r, 200)
    assert code == 404, (code, resp.get_json())
    assert resp.get_json()["ok"] is False


def test_hidden_top_dirs_are_denied_even_inside_workspace(ws):
    """`系统/` 就在产物库里，但它存的是本机凭据——黑名单必须真的挡住它。

    这条是被自家测试逼出来的：第一版只判"在不在产物库内"，
    `系统/api_token.txt` 照样解析成功，等于给了聊天一个能打开 token 文件的口子。
    """
    import backend as B
    assert W.resolve_workspace_ref("系统/api_token.txt") is None
    assert W.resolve_workspace_ref("组件/node/current/node.exe") is None
    assert W.resolve_workspace_ref("月报检查_20261026.md"), "可见目录的正常文件不能一起拒掉"
    with B.app.test_request_context("/api/workspace/locate", method="POST",
                                    json={"ref": "系统/api_token.txt"}):
        r = W.api_workspace_locate()
    _resp, code = r if isinstance(r, tuple) else (r, 200)
    assert code == 404


# ---------------- 前端接线（防"后端有、前端断"） ----------------

def _js(name):
    return io.open(os.path.join(ROOT, "js", name), encoding="utf-8").read()


def test_chat_wires_the_two_entry_points():
    render = _js("09-chat-render.js")
    chat = _js("10-chat.js")
    for fn in ("function locateWorkspaceFile", "function attachFileRefToTool",
               "function linkifyWorkspacePaths", "function _fileRefRow"):
        assert fn in render, "渲染层缺 %s" % fn
    # v1.0.629 契约更新：路径优先取后端事件上的 j.path，从 summary 反解只留作兜底
    # （summary 是结果 JSON 的前 400 字，路径靠后或被截断时 JSON.parse 直接抛，
    #   正式包上 create_pptx 的文件行就是这么静默消失的）。
    # v1.0.645：这套"结果怎么落到卡上"整体搬进 js/09 的 applyToolResult——
    # 实时流与历史重放必须共用一个函数，所以这三条现在查 render 而不是 chat。
    assert "attachFileRefToTool(hostEl, _fp)" in render, "步骤卡没挂文件行"
    assert "ev.path" in render, "步骤卡没优先用事件里的 path"
    assert 'JSON.parse(summary||"{}")' in render, "旧事件形态的兜底反解被删了"
    # v1.0.627 契约是"恰好两处"（流式收尾 + localStorage 恢复）。v1.0.641 补上第三处：
    # 从**会话库**渲染那条路（switchConv / reloadPaneFromStore 共用的 _renderStoredMsgs）
    # 此前从不 linkify，用户实报"跑完当时能点，关掉软件重新点开就不行"。
    # 计数从 ==2 改成 >=3 并逐点指名——只卡数字会把"新增一处、少了一处"互相抵消掉。
    assert chat.count("linkifyWorkspacePaths(") >= 3, "正文/历史恢复/会话库三处链接化少了一处"
    assert "function _renderStoredMsgs(" in chat, "会话库渲染没有共用渲染器"
    assert "function restoreHistory(" in chat
    assert "/api/workspace/locate" in render, "前端没接后端入口"


def test_linkify_handler_cannot_capture_the_loop_variable():
    """v1.0.630（用户实报"点路径没打开文件夹"）：onclick 里绝不能引用 while 条件变量 m。

    `while((m = _WS_PATH_RE.exec(text))){ ... btn.onclick = function(){ use(m[0]) } }`
    ——循环正常退出时 m === null，按钮看着在、点了在 handler 里抛
    "Cannot read properties of null"，后端连日志都没有（本次就是这么被发现的）。
    必须逐次 `const ref = m[0]` 再让闭包捕获 ref。行为层由台子
    `build/_test_fileref_mounts.py` 的 locateRefs 断言兜（改回 m[0] 当场变红），
    这条源码锁只是把"别再写回 m[0]"钉在门禁里。
    """
    blk = _js("09-chat-render.js")
    body = blk[blk.index("function linkifyWorkspacePaths"):]
    body = body[:body.index("\n}\n") + 3]
    assert "const ref = m[0]" in body, "匹配结果没逐次捕获成常量"
    assert "locateWorkspaceFile(m[0]" not in body, "handler 又去引用循环变量 m 了"
    assert "locateWorkspaceFile(ref," in body


def test_file_ref_uses_safe_dom_not_inner_html():
    """路径文本来自模型输出，属不可信内容——只能用 textContent 写，不拼 HTML。

    判据只扫代码行：注释里出现 "innerHTML" 这个词是在**说明**为什么不用它，
    按整块文本查会让这条锁一写注释就假红（第一版正是这样自伤的）。
    """
    blk = _js("09-chat-render.js")
    blk = blk[blk.index("AI 产出文件的可点入口"):]
    code = "\n".join(ln for ln in blk.splitlines()
                     if not ln.strip().startswith(("//", "/*", "*")))
    # 例外只有一个：md 预览走 renderRich——那是每条聊天气泡都在用的 sanitizer
    # （inlineMd/代码块都先 escapeHtml），和 addMsg 同一口径。除此以外的 innerHTML
    # 一律算拼 HTML。对拍台 build/_test_replay_v645.py 里放了真 <script> 探针验它不执行。
    stripped = code.replace("innerHTML = renderRich(", "")
    assert "innerHTML" not in stripped, "新代码里出现了 innerHTML 拼接（XSS 面）"
    assert code.count("textContent") >= 3, "芯片文本没走 textContent"
    assert "aria-label" in code, "图标按钮要有无障碍名称（本项目 a11y 约定）"


def test_styles_avoid_the_char_by_char_wrap_bug():
    """623 的 .shothead 教训：flex 容器可换行 + 子项不收缩，否则窄栏逐字换行。"""
    css = io.open(os.path.join(ROOT, "css", "style.css"), encoding="utf-8").read()
    i = css.index(".file-ref{")
    seg = css[i:i + 600]
    assert "flex-wrap:wrap" in seg, ".file-ref 容器不可整项换行"
    assert ".file-ref>*{flex:0 0 auto;white-space:nowrap}" in seg, \
        "子项允许收缩 → 中文文件名会一个字一行（623 .shothead 同款）"

# ============================================================ 来自 test_v634_file_manifest.py（v1.0.634）


import agent.loop as AL                    # noqa: E402
from agent_tools.registry import is_file_producer   # noqa: E402


# ---------------------------------------------------------------- 展示路径
def test_inside_workspace_becomes_forward_slash_relative(tmp_path, monkeypatch):
    import core.paths as P
    monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    p = os.path.join(str(tmp_path), "产物库", "产出", "AI演变史.pptx")
    assert AL._ws_disp_path(p) == "产物库/产出/AI演变史.pptx"


def test_outside_workspace_keeps_absolute_path(tmp_path, monkeypatch):
    import core.paths as P
    monkeypatch.setattr(P, "workspace_root", lambda: str(tmp_path / "ws"))
    p = os.path.join("C:\\Users\\me\\Desktop", "报表.xlsx")
    got = AL._ws_disp_path(p)
    assert got.endswith("报表.xlsx") and "\\" not in got, got


# ---------------------------------------------------------------- 清单文本
def test_no_files_means_no_block_at_all():
    assert AL._produced_manifest([]) == ""
    assert AL._produced_manifest(None) == ""
    assert AL._produced_manifest([""]) == ""


def test_manifest_lists_each_file_once():
    txt = AL._produced_manifest(["产物库/产出/a.md", "产物库/产出/a.md", "产物库/下载/b.pdf"])
    assert "📎 本次生成的文件（2）" in txt, txt
    assert txt.count("a.md") == 1 and "b.pdf" in txt
    assert txt.startswith("\n\n---\n\n"), "要有分隔线，与回答正文视觉上分开"


def test_paths_already_written_by_the_model_are_not_repeated():
    """正文里模型已经原样写过这个路径 → linkify 早把它变成按钮，清单再列一次就是重复。"""
    ans = "已生成 产物库/产出/a.md，请查收。"
    txt = AL._produced_manifest(["产物库/产出/a.md", "产物库/产出/c.md"], ans)
    assert "a.md" not in txt and "c.md" in txt, txt
    assert "（1）" in txt


def test_manifest_caps_rows_but_reports_total():
    files = ["产物库/产出/f%02d.md" % i for i in range(20)]
    txt = AL._produced_manifest(files)
    assert "（20）" in txt and "另有 8 个未列出" in txt, txt
    assert txt.count("\n- ") == 13          # 12 行 + "另有"那行也是列表项


def test_empty_answer_still_lists_files():
    txt = AL._produced_manifest(["产物库/产出/a.md"], "")
    assert "a.md" in txt


# ---------------------------------------------------------------- 产出口径
@pytest.mark.parametrize("name,args,out,want", [
    ("save_file", {"subdir": "产出", "filename": "a.md"},
     {"ok": True, "path": "/ws/产物库/产出/a.md"}, True),
    ("create_pptx", {}, {"ok": True, "path": "/ws/产物库/产出/x.pptx"}, True),
    ("download_wecom_attachment", {}, {"ok": True, "path": "/ws/产物库/下载/x.pdf"}, True),
    ("learn_capture", {}, {"ok": True, "path": "/ws/产物库/知识库/课件/x.md"}, True),
    ("pc_files", {"op": "write", "path": "/ws/x.txt"}, {"ok": True, "path": "/ws/x.txt"}, True),
    # 只读：结果里同样带 path，但不是"本次产出"
    ("read_workspace_file", {"path": "产出/a.md"},
     {"ok": True, "path": "/ws/产物库/产出/a.md", "content": "..."}, False),
    ("pc_files", {"op": "read", "path": "/ws/x.txt"}, {"ok": True, "path": "/ws/x.txt"}, False),
    # 删除的 path 是"刚进回收站的文件"，列成产物等于骗用户去点开
    ("pc_files", {"op": "delete", "path": "/ws/x.txt"},
     {"ok": True, "path": "/ws/x.txt", "note": "已移入回收站"}, False),
    # 失败/待审批的结果里 path 是"待操作对象"
    ("save_file", {}, {"__error__": "保存失败"}, False),
    ("save_file", {}, {"ok": True}, False),
    ("save_file", {}, {"ok": True, "path": "   "}, False),
    ("click_wecom_element", {"target": "x"}, {"ok": True, "path": "/ws/a.md"}, False),
])
def test_is_file_producer(name, args, out, want):
    assert is_file_producer(name, args, out) is want


def test_producer_list_covers_the_tools_that_actually_write():
    """新增写文件的工具却忘了进名单 → 清单少一项，且不会有任何报错。这条替我们兜着。"""
    import agent_tools.executors as EX
    for n in ("save_file", "save_workspace_file", "create_skill", "download_file",
              "download_wecom_attachment", "learn_capture", "pc_files"):
        assert n in EX._TOOL_HANDLERS, n     # 确实注册了
    from agent_tools import office_gen
    import inspect
    src = inspect.getsource(office_gen)
    assert "create_excel" in src and "create_pptx" in src


# ---------------------------------------------------------------- 端到端（跑真 loop）
def _run(tmp_path, monkeypatch, rounds, results):
    import core
    import agent.loop as L
    orig = (core.paths.workspace_root, L.stream_llm_events, L.dispatch_tool)
    core.paths.workspace_root = lambda: os.path.join(str(tmp_path), "产物库")
    seq = list(rounds)

    def _fake_stream(*a, **k):
        payload = seq.pop(0) if seq else {"text": "收尾", "usage": None, "tool_calls": []}
        return iter([("done", payload)])

    called = []

    def _dispatch(name, args, cfg):
        called.append(name)
        if name == "finish":
            # finish 的参数名就是 final_answer（写错会被 v1.0.344 的质量校验当场拦下，
            # 表现为"清单没出现"——这个坑本次踩过一次，注释留在这儿）
            return {"__finish__": True, "final_answer": args.get("final_answer") or "做完了"}
        return results.get(name, {"ok": True})

    L.stream_llm_events = _fake_stream
    L.dispatch_tool = _dispatch
    cfg = {"models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                       "api_key": "", "model": "t", "enabled": True}],
           "wecom": {}, "connectors": {}, "custom_skills": [], "skill_overrides": {},
           "enabled_skills": [], "feature_flags": {"skills": False},
           "security": {"permission_level": "auto"}}
    evs = []
    try:
        for item in L.agent_loop([{"role": "user", "content": "做个PPT"}], cfg,
                                 pane_id="mfx1", max_rounds=3):
            s = item.strip() if isinstance(item, str) else ""
            if s.startswith("data:"):
                try:
                    evs.append(json.loads(s[5:].strip()))
                except Exception:  # noqa: BLE001  注释行/心跳行不是 JSON，跳过即可
                    continue
    finally:
        core.paths.workspace_root, L.stream_llm_events, L.dispatch_tool = orig
    return evs, called


def test_generated_files_are_listed_at_the_end_of_the_answer(tmp_path, monkeypatch):
    pptx = os.path.join(str(tmp_path), "产物库", "产出", "AI演变史.pptx")
    read = os.path.join(str(tmp_path), "产物库", "下载", "素材.pdf")
    rounds = [
        {"text": "", "usage": None, "tool_calls": [
            {"id": "1", "name": "create_pptx", "args": {"slides": []}},
            {"id": "2", "name": "read_workspace_file", "args": {"path": "下载/素材.pdf"}}]},
        {"text": "", "usage": None, "tool_calls": [
            {"id": "3", "name": "finish", "args": {"final_answer": "PPT 已生成，共 12 页。"}}]},
    ]
    results = {"create_pptx": {"ok": True, "path": pptx, "size": 53000},
               "read_workspace_file": {"ok": True, "path": read, "content": "……"}}
    evs, called = _run(tmp_path, monkeypatch, rounds, results)
    assert "create_pptx" in called and "finish" in called
    tok = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
    assert "📎 本次生成的文件（1）" in tok, tok[-300:]
    assert "产物库/产出/AI演变史.pptx" in tok
    assert "素材.pdf" not in tok, "只读过的文件被当成产物列出来了"
    assert tok.rstrip().endswith("AI演变史.pptx"), "清单必须在回答**末尾**"


def test_no_output_files_means_the_answer_has_no_manifest(tmp_path, monkeypatch):
    rounds = [{"text": "", "usage": None, "tool_calls": [
        {"id": "1", "name": "finish", "args": {"final_answer": "已读完页面，无需生成文件。"}}]}]
    evs, _ = _run(tmp_path, monkeypatch, rounds, {})
    tok = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
    assert "📎" not in tok and "本次生成的文件" not in tok, tok[-200:]


def test_manifest_is_not_marked_as_a_system_notice(tmp_path, monkeypatch):
    """清单必须**进落库**（重开历史还在）。B3 之后带 sys:1 的 token 会被 _memory_watch 跳过，
    所以这里锁住"清单所在的那个 token 事件不是 sys"。"""
    pptx = os.path.join(str(tmp_path), "产物库", "产出", "a.md")
    rounds = [
        {"text": "", "usage": None, "tool_calls": [
            {"id": "1", "name": "save_file", "args": {}}]},
        {"text": "", "usage": None, "tool_calls": [
            {"id": "2", "name": "finish", "args": {"final_answer": "好了"}}]},
    ]
    evs, _ = _run(tmp_path, monkeypatch, rounds, {"save_file": {"ok": True, "path": pptx}})
    hit = [e for e in evs if e.get("type") == "token" and "本次生成的文件" in (e.get("text") or "")]
    assert hit, evs
    assert not hit[0].get("sys"), "被标成系统播报 → 会话落库会把它丢掉"


def test_frontend_still_owns_the_click_mechanism():
    """后端只负责把路径写进正文；"可点击"依赖 09 的 linkify 口径，两边字面量要对得上。"""
    js = open(os.path.join(ROOT, "js", "09-chat-render.js"), encoding="utf-8").read()
    assert "_WS_PATH_RE" in js
    # 盯的是"前端自己拼了一块清单"这个字面量（带 📎 前缀），不是注释里提到这件事——
    # 上一版这里只查裸短语，被我自己写的那行注释判红了。
    assert "📎 本次生成的文件" not in js, "前端不该再自己拼一份清单（两处渲染必然漂）"

# ============================================================ 来自 test_v635_manifest_diff.py（v1.0.635）


import core.workspace_io as WIO   # noqa: E402


def _mk(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------- 快照与差集
def test_new_and_modified_files_are_detected(tmp_path, monkeypatch):
    from core import paths as P
    monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    before = WIO.snapshot_outputs()
    a = _mk(os.path.join(str(tmp_path), "产物库", "产出", "演示.pptx"), "deck")
    b = _mk(os.path.join(str(tmp_path), "产物库", "下载", "报表.xlsx"), "r1")
    assert a in WIO.new_outputs(before) and b in WIO.new_outputs(before)
    before2 = WIO.snapshot_outputs()
    _mk(b, "r2-longer")                       # 改写既有文件也算产出
    got = WIO.new_outputs(before2)
    assert b in got and a not in got, got


def test_deleted_file_is_not_reported_as_output(tmp_path, monkeypatch):
    """差集只加不减：任务中途删掉的文件不该冒出来"供你打开"。"""
    from core import paths as P
    monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    a = _mk(os.path.join(str(tmp_path), "产物库", "产出", "a.md"), "x")
    before = WIO.snapshot_outputs()
    os.remove(a)
    assert WIO.new_outputs(before) == []


def test_ledger_and_component_dirs_are_skipped(tmp_path, monkeypatch):
    """台账每次 save_file 都被追加——列出来纯噪声；组件/ 是运行时（7 万文件）更不该扫。"""
    from core import paths as P
    monkeypatch.setattr(P, "workspace_root", lambda: os.path.join(str(tmp_path), "产物库"))
    _mk(WIO._ledger_path(), "| 时间 | 来源 |\n")
    _mk(os.path.join(str(tmp_path), "产物库", "产出", "组件", "node", "node.exe"), "bin")
    snap = WIO.snapshot_outputs()
    assert WIO._ledger_path() not in snap
    assert not any("组件" in p.replace("\\", "/") for p in snap), list(snap)[:5]


def test_snapshot_failure_degrades_to_tool_reported_only(tmp_path, monkeypatch):
    """before=None（快照没拍成）→ 差集返回空，清单退回只列工具上报的，不抛。"""
    assert WIO.new_outputs(None) == []
    assert WIO.new_outputs("不是字典") == []


# ---------------------------------------------------------------- 跑真 loop
def _run_loop(tmp_path, monkeypatch, tool_name, side_effect):
    import core
    import agent.loop as L
    orig = (core.paths.workspace_root, L.stream_llm_events, L.dispatch_tool)
    core.paths.workspace_root = lambda: os.path.join(str(tmp_path), "产物库")
    rounds = [
        {"text": "", "usage": None, "tool_calls": [{"id": "1", "name": tool_name, "args": {}}]},
        {"text": "", "usage": None, "tool_calls": [
            {"id": "2", "name": "finish", "args": {"final_answer": "PPT 已生成。"}}]},
    ]
    seq = list(rounds)

    def _fake_stream(*a, **k):
        payload = seq.pop(0) if seq else {"text": "", "usage": None, "tool_calls": []}
        return iter([("done", payload)])

    def _dispatch(name, args, cfg):
        if name == "finish":
            return {"__finish__": True, "final_answer": args.get("final_answer") or "好了"}
        return side_effect()

    L.stream_llm_events = _fake_stream
    L.dispatch_tool = _dispatch
    cfg = {"models": [{"id": "m1", "name": "T", "base_url": "http://127.0.0.1:59999/v1",
                       "api_key": "", "model": "t", "enabled": True}],
           "wecom": {}, "connectors": {}, "custom_skills": [], "skill_overrides": {},
           "enabled_skills": [], "feature_flags": {"skills": False},
           "security": {"permission_level": "auto"}}
    evs = []
    try:
        for item in L.agent_loop([{"role": "user", "content": "做个PPT"}], cfg,
                                 pane_id="mfx2", max_rounds=3):
            s = item.strip() if isinstance(item, str) else ""
            if s.startswith("data:"):
                try:
                    evs.append(json.loads(s[5:].strip()))
                except Exception:  # noqa: BLE001
                    continue
    finally:
        core.paths.workspace_root, L.stream_llm_events, L.dispatch_tool = orig
    return "".join(e.get("text", "") for e in evs if e.get("type") == "token")


def test_script_produced_pptx_shows_up_in_the_manifest(tmp_path, monkeypatch):
    """这就是用户那一单：pc_run 跑脚本落盘的 pptx 没有任何工具结果带路径，
    清单仍必须列出它（工具上报的三个 .py 也保留）。"""
    deck = os.path.join(str(tmp_path), "产物库", "产出", "AI演变史.pptx")

    def _side():
        # 演"脚本写文件"：结果里只有 ok 与 stdout，没有 path
        _mk(deck, "PK\x03\x04 fake pptx bytes")
        for n in ("check_pptx.py", "verify_ppt.py"):
            _mk(os.path.join(str(tmp_path), "产物库", "产出", n), "print(1)")
        return {"ok": True, "stdout": "saved"}

    tok = _run_loop(tmp_path, monkeypatch, "pc_run", _side)
    assert "本次生成的文件（3）" in tok, tok[-400:]
    assert "产物库/产出/AI演变史.pptx" in tok, "脚本产出的 pptx 又漏了"
    assert "check_pptx.py" in tok and "verify_ppt.py" in tok


def test_tool_reported_files_are_not_duplicated_by_the_diff(tmp_path, monkeypatch):
    """save_file 既上报路径、磁盘上也确有此文件——差集不能把它列第二遍。"""
    real = os.path.join(str(tmp_path), "产物库", "产出", "报告.md")

    def _side():
        _mk(real, "# 报告\n")
        return {"ok": True, "path": real}

    tok = _run_loop(tmp_path, monkeypatch, "save_file", _side)
    assert "本次生成的文件（1）" in tok, tok[-300:]
    assert tok.count("报告.md") == 1, tok[-300:]


def test_manifest_order_puts_tool_reported_files_first(tmp_path, monkeypatch):
    """工具上报的语义确定（就是这次动作的产物），差集是兜底——顺序要体现这个优先级。"""
    def _side():
        _mk(os.path.join(str(tmp_path), "产物库", "产出", "zz-脚本产物.md"), "a")
        _mk(os.path.join(str(tmp_path), "产物库", "产出", "aa-工具产物.md"), "b")
        return {"ok": True, "path": os.path.join(str(tmp_path), "产物库", "产出", "aa-工具产物.md")}

    tok = _run_loop(tmp_path, monkeypatch, "save_file", _side)
    body = tok.split("本次生成的文件", 1)[1]
    assert body.index("aa-工具产物") < body.index("zz-脚本产物"), body[:300]


def test_loop_wires_the_disk_diff():
    """接线锁：只写函数不接调用点 = 没修（本项目反复踩过的空闸门）。"""
    import inspect
    src = inspect.getsource(sys.modules["agent.loop"])
    assert "snapshot_outputs" in src and "new_outputs" in src
    i = src.find("def agent_loop")
    body = src[i:]
    assert "_diff_outputs(_out_snapshot)" in body, "收尾没做差集比对"

# ============================================================ 来自 test_v641_outside_outputs.py（v1.0.641）


import pytest  # noqa: E402

from agent.loop import _answer_file_paths, _produced_manifest  # noqa: E402
import routes.workspace as RW  # noqa: E402
from core import paths as P  # noqa: E402


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """造一个"假桌面 + 假产物库"：产物库挂在该目录之下，正好复现本机
    `Desktop\\企微伴侣\\产物库` 那种嵌套关系——这是最容易漏判的一种布局。"""
    home = tmp_path / "Desktop"
    (home / "企微伴侣" / "产物库" / "系统").mkdir(parents=True)
    (home / "企微伴侣" / "产物库" / "产出").mkdir(parents=True)
    monkeypatch.setattr(P, "workspace_root",
                        lambda: str(home / "企微伴侣" / "产物库"))
    monkeypatch.setattr(RW, "_outside_allow_dirs", lambda: [str(home)])
    return home


# ---------------------------------------------------------------- 第三路来源
def test_existing_absolute_paths_are_extracted_from_the_answer(desk):
    pdf = _mk(str(desk / "付款凭证汇总.pdf"), "%PDF")
    got = _answer_file_paths("合并结果：%s（10 页）" % pdf)
    assert got == [pdf], got


def test_made_up_and_nonexistent_paths_are_dropped(desk):
    got = _answer_file_paths("见 C:/nowhere/notexist.pdf 以及 产物库/产出/无.md")
    assert got == [], got


def test_executables_mentioned_in_prose_are_not_outputs(desk):
    """模型正文里出现 `C:\\Windows\\system32\\cmd.exe` 不该被当成"本次生成的文件"。"""
    exe = _mk(str(desk / "evil.exe"), "MZ")
    assert _answer_file_paths("别跑 %s" % exe) == []


def test_urls_are_not_mistaken_for_drive_paths(desk):
    """`https://example.com/a.md` 里的 `s:/` 看着就像盘符——两侧（Python 抽取 / JS linkify）
    都必须挡住，否则一句外链就变成一条点不动还带误导的"产出"。"""
    assert _answer_file_paths("参考 https://example.com/a.md 与 http://x/y.pdf") == []


def test_backslash_and_forward_slash_forms_both_work(desk):
    BS = chr(92)
    pdf = _mk(str(desk / "发票汇总.pdf"), "%PDF")
    want = os.path.realpath(pdf)
    # 两种写法都要抽到；抽出来的字符串保持原样（Windows 两种斜杠都认），
    # 所以比对用 realpath，而不是要求它等于某个特定写法。
    for form in (pdf, pdf.replace(BS, "/")):
        got = _answer_file_paths("合并为：%s" % form)
        assert len(got) == 1 and os.path.realpath(got[0]) == want, (form, got)


# ---------------------------------------------------------------- 清单去重的边界
def test_outside_paths_are_listed_even_though_the_answer_wrote_them(desk):
    """v1.0.634 的去重只该管产物库内：那里正文一定会 linkify 成按钮，列两遍是重复。
    产物库外的绝对路径**必须照列**——用户报的就是"没列出来"。"""
    pdf = _mk(str(desk / "付款凭证汇总.pdf"), "%PDF")
    txt = _produced_manifest([pdf], "合并为：%s" % pdf)
    assert "📎 本次生成的文件（1）" in txt, txt
    assert "付款凭证汇总.pdf" in txt


def test_workspace_paths_already_written_are_still_deduped(desk):
    txt = _produced_manifest(["产物库/产出/核查报告.md"], "报告见 产物库/产出/核查报告.md")
    assert txt == "", "产物库内那套去重被改坏了"


# ---------------------------------------------------------------- 定位白/黑名单
def test_outside_output_dirs_are_locatable(desk):
    pdf = _mk(str(desk / "付款凭证汇总.pdf"), "%PDF")
    assert RW.resolve_workspace_ref(pdf) == os.path.realpath(pdf)


def test_executables_are_never_locatable_even_on_the_desktop(desk):
    exe = _mk(str(desk / "工具.exe"), "MZ")
    assert RW.resolve_workspace_ref(exe) is None
    for bad in ("bat", "ps1", "vbs", "lnk", "msi", "hta", "jar", "py"):
        f = _mk(str(desk / ("x." + bad)))
        assert RW.resolve_workspace_ref(f) is None, bad


def test_the_app_tree_does_not_borrow_the_desktop_allowance(desk):
    """核心那道闸：产物库就住在桌面底下，"放开桌面"绝不能把凭据目录一起放开。"""
    tok = _mk(str(desk / "企微伴侣" / "产物库" / "系统" / "api_token.txt"), "secret")
    assert RW.resolve_workspace_ref(tok) is None, "api_token 被定位到了——黑名单失效"
    assert RW.resolve_workspace_ref("产物库/系统/api_token.txt") is None
    # 同一条规则也挡住软件运行目录里的其它文件（dist 同级）
    inside_app = _mk(str(desk / "企微伴侣" / "config.json"), "{}")
    assert RW.resolve_workspace_ref(inside_app) is None


def test_workspace_visible_paths_still_resolve(desk):
    md = _mk(str(desk / "企微伴侣" / "产物库" / "产出" / "报告.md"), "r")
    assert RW.resolve_workspace_ref(md) == os.path.realpath(md)
    assert RW.resolve_workspace_ref("产物库/产出/报告.md") == os.path.realpath(md)


def test_paths_outside_every_allowment_are_refused(desk, tmp_path):
    elsewhere = _mk(str(tmp_path / "other" / "x.pdf"), "%PDF")
    assert RW.resolve_workspace_ref(elsewhere) is None


# ---------------------------------------------------------------- 两边清单不许漂
def test_python_and_js_extension_lists_match():
    """清单（Python 侧）与 linkify（JS 侧）各持一份扩展名清单，漂了就会出现
    "列出来了但点不动"或"能点但没列出来"。这里逐字比一次。"""
    js = open(os.path.join(ROOT, "js", "09-chat-render.js"), encoding="utf-8").read()
    marker = "const _WS_EXT = "
    i = js.index(marker)
    js_ext = js[i + len(marker):js.index(";", i)].strip().strip('"')
    from agent.loop import _PRODUCED_EXT
    assert js_ext == _PRODUCED_EXT, (js_ext, _PRODUCED_EXT)


def test_history_render_path_linkifies():
    """用户第二个报障："跑完当时能点，关掉软件重新点开就不行"。
    根因是 switchConv / reloadPaneFromStore 这条从会话库渲染的路径从来没 linkify 过。"""
    js = open(os.path.join(ROOT, "js", "10-chat.js"), encoding="utf-8").read()
    i = js.index("function _renderStoredMsgs(")
    body = js[i:js.index("\n}", i)]
    assert "linkifyWorkspacePaths(" in body, "两条历史渲染路径又分叉了"
    # v1.0.645 起范围与实时流对齐：只做正文气泡（.msg.agent .mc），不碰步骤卡结果行——
    # "重开比当时多出几个按钮"同样是版式不一致。
    assert '.msg.agent .mc' in body, "历史重放的 linkify 范围不再与实时流同口径"
    assert js.count("_renderStoredMsgs(key, d.messages)") == 2, \
        "switchConv / reloadPaneFromStore 应共用同一个渲染器"
    assert "d.messages||[]).forEach" not in js, "还有地方在自己抄一遍渲染循环"
