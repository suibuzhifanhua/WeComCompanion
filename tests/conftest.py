# tests/conftest.py
import io
import os
import sys

_sys_path_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_path_root not in sys.path:
    sys.path.insert(0, _sys_path_root)
# v1.0.646：测试之间不得互相 import（test_v623_mute_sync 曾 `from test_v555_batch3
# import _js_verdict`，把两个不相关版本的测试文件焊死在一起——归并按域做时必踩）。
# 跨文件复用的判定函数一律放这里，测试文件只 `from conftest import js_verdict`。
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def js_verdict(expr):
    """跑 _run_wecom_js_safe 的安全判定（不执行 JS——wb.run_js 打桩）。
    返回 "approval"（转人工审批）或 "pass"（直接放行）。

    共享原因：wecom_run_js 防火墙的判定被「JS 通道安全」与「跑课静音表达式过审」
    两类用例同时依赖；放在生产者文件里会让测试文件之间产生 import 依赖。"""
    from agent_tools import executors as EX
    import wecom_bridge as wb

    orig = wb.run_js
    wb.run_js = lambda *a, **k: {"ok": True}   # 放行分支的打桩
    try:
        out = EX._run_wecom_js_safe({"expression": expr}, {"wecom": {"cdp_port": 9222}})
    finally:
        wb.run_js = orig
    if isinstance(out, dict) and out.get("__approval_needed__"):
        return "approval"
    return "pass"

# v1.0.526（QA 改进项1）：sys.modules 别名机制已删除——它把生产环境的裸名 import
# 断裂掩盖了多版本（v1.0.526 P1：memory_ops 裸名导致打包版记忆面板全 500，测试却全绿）。
# 现全部测试与生产统一使用包路径（services.* / desktop.* / core.*），已 grep 验证
# tests/ 与生产代码均无裸名残留。若未来引入别名需求，须同时保证生产可解析。

# v1.0.405（H1）：API 鉴权豁免——test_client 不走 / 路由拿不到 cookie，
# 测试环境统一关闭鉴权闸门（backend._AUTH_DISABLED）。鉴权本身有专项测试
# tests/test_api_auth.py（临时重新开启验证 401/403/放行三态）。
try:
    import backend as _backend
    _backend._AUTH_DISABLED = True
except Exception:
    pass

# v1.0.401：测试环境屏蔽管理员自启动的任务查询（schtasks 在沙箱黑名单会
# 终止整个 pytest 进程树；生产环境不受影响）。_admin_task_exists 无专测。
import pytest  # noqa: E402

# ---------- v1.0.619（测试卫生）：跑批临时目录不再留垃圾 ----------
# 全仓 50 处 tempfile.mkdtemp 里只有 6 处配套 rmtree——实测在 %TEMP% 面积累过
# 3390 个 wec_* 目录（几百 MB），而 tests/ 源码树里也长期躺着 _comp_tmp/_dl_test*
# 这类固定名残留（被误当源码读、还污染打包扫描）。这里在会话级统一回收：
# 包一层 mkdtemp 记下本次跑批创建的目录，会话结束整批删掉（跨用例仍然可见，
# 所以不影响"先写后读"那类同会话依赖）。
import shutil  # noqa: E402
import tempfile  # noqa: E402

_REAL_MKDTEMP = tempfile.mkdtemp
_SESSION_DIRS = []


def _tracked_mkdtemp(*args, **kwargs):
    d = _REAL_MKDTEMP(*args, **kwargs)
    _SESSION_DIRS.append(d)
    return d


_TESTS_SCRATCH = ("_comp_tmp", "_dl_test", "_dl_test2", "_dl_test3", "_dl_out")


@pytest.fixture(scope="session", autouse=True)
def _reclaim_temp_dirs():
    tempfile.mkdtemp = _tracked_mkdtemp
    try:
        yield
    finally:
        tempfile.mkdtemp = _REAL_MKDTEMP
        for d in _SESSION_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        _SESSION_DIRS.clear()
        for name in _TESTS_SCRATCH:
            shutil.rmtree(os.path.join(_sys_path_root, "tests", name), ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_schtasks_query(monkeypatch):
    # v1.0.402 拆分后定义处是 routes.b_admin——patch 定义处（壳与调用方同时覆盖）
    try:
        import routes.b_admin as _ba
        monkeypatch.setattr(_ba, "_admin_task_exists", lambda: False)
    except Exception:
        pass
    try:
        import routes.management as _mg
        monkeypatch.setattr(_mg, "_admin_task_exists", lambda: False)
    except Exception:
        pass


# ---------- v1.0.648 归并残留收敛（A12）：跨文件逐字重复的 helper ----------
# 按 v1.0.646 约定收进来：生产模块一律函数内延迟 import（本模块不依赖生产代码），
# 测试文件以 `from conftest import _src`（或 as 别名）取用。行为与被删的本地拷贝
# 逐字一致；行为不同的本地 helper（如各文件自带的 _mk/_patch/_cleanup）未动。

def _src(*rel):
    """读仓库根下相对路径的文本文件。
    收敛自 6 份同型拷贝（_src/_read/rd 各文件名不变，见下方别名）。"""
    return io.open(os.path.join(_sys_path_root, *rel), encoding="utf-8").read()


_read = _src   # test_agent_runtime / test_silent_fail_gate / test_build_guard 的原有调用名
rd = _src      # test_misc_fixes / test_wecom_windows 的原有调用名


def _done(text="", calls=None):
    """agent 事件流假件：done 事件（原 test_agent_scenarios / test_audit_batch 两份逐字相同）。"""
    return ("done", {"text": text, "tool_calls": calls or [], "usage": None})


def _call(name, args, cid="1"):
    """agent 事件流假件：tool_call 项（同上两份逐字相同）。"""
    return {"id": cid, "name": name, "args": args}


def _patch_config_path(tmp_path):
    """config_store.config_path 改道到临时目录，返回恢复函数。
    （原 test_config_dsh / test_custom_skills 两份逐字相同。）"""
    import core.config_store as config_store
    orig = config_store.config_path
    config_store.config_path = lambda: tmp_path
    return orig


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """隔离 config 的 Flask test_client（yield (client, cfg_path)）。
    原 test_build_guard / test_conversation_settings / test_silent_fail_gate 三份逐字相同。"""
    import core.config_store as cs
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "config_path", lambda: str(cfg))
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None
    import backend as backend_mod
    backend_mod.app.config["TESTING"] = True
    with backend_mod.app.test_client() as c:
        yield c, cfg
    cs._CFG_CACHE["cfg"] = None
    cs._CFG_CACHE["mtime"] = None
