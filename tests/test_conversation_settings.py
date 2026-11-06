# -*- coding: utf-8 -*-
"""tests/test_conversation_settings.py — 按域归并的回归锁合集

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


# ============================================================ 来自 test_v614_personal.py（v1.0.614）


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile

import core.config_store as cs   # noqa: E402


def _disk(cfg):
    return json.loads(cfg.read_text(encoding="utf-8"))


def test_url_mounted_under_api_once(app_client):
    """蓝图挂在 routes/__init__ 的顶层（统一 /api），嵌套时不重复前缀。"""
    c, _ = app_client
    rules = {r.rule for r in c.application.url_map.iter_rules()}
    assert "/api/personal/config" in rules, sorted(x for x in rules if "personal" in x)
    assert "/api/api/personal/config" not in rules


def test_get_shape_and_no_secret_echo(app_client):
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("dsh", {}).__setitem__("api_key", "sk-realsecret1234"), True)[1])
    d = c.get("/api/personal/config").get_json()
    assert d["ok"] is True
    assert set(d) >= {"reimburse", "dsh", "background"}
    assert d["dsh"]["api_key_set"] is True and d["dsh"]["api_key_tail"] == "1234"
    # 凭据原文绝不进响应体
    body = c.get("/api/personal/config").get_data(as_text=True)
    assert "sk-realsecret" not in body
    assert set(d["background"]) == {"digests_daily", "digests_time", "kb_organize",
                                    "kb_digest", "todo_watch", "todo_interval"}, d["background"]
    assert set(d["reimburse"]) == {"name", "bank_account", "bank_name",
                                   "mask_for_llm", "encrypted_fields"}, d["reimburse"]
    assert set(d["dsh"]) == {"base_url", "api_key_set", "api_key_tail", "api_key_unreadable",
                             "cwd", "extra_args"}, d["dsh"]


def _blob(k, val):
    """把一个"本机解不开的密文"落到配置里：_walk_secrets 对已带 enc: 的值不再加密，
    而 _unprotect_secret 解不开时原样保留——正是跨机/跨账户搬配置的真实形态。"""
    def _m(cfg):
        cfg.setdefault("reimburse", {})[k] = val
        return True
    cs.update_config(_m)


def test_unreadable_cipher_never_reaches_the_dom(app_client):
    """DPAPI 绑定用户+机器，搬过来的配置里那几项必然解不开（实测 0x8009002B）。
    把 359 字符的密文塞进输入框，用户看不见、保存还会被限长截断——必须下发空值 + 标记。"""
    c, cfg = app_client
    _blob("bank_account", "enc:v1:" + "Q" * 350)
    d = c.get("/api/personal/config").get_json()
    assert d["reimburse"]["bank_account"] == ""
    assert d["reimburse"]["encrypted_fields"] == ["bank_account"], d["reimburse"]
    assert "enc:v1" not in c.get("/api/personal/config").get_data(as_text=True)


def test_blank_save_does_not_destroy_unreadable_cipher(app_client):
    c, cfg = app_client
    _blob("bank_account", "enc:v1:" + "W" * 300)
    before = _disk(cfg)["reimburse"]["bank_account"]
    c.post("/api/personal/config", json={"reimburse": {"name": "王五", "bank_account": ""}})
    after = _disk(cfg)["reimburse"]["bank_account"]
    assert after == before, "空值提交把唯一一份密文写没了（v1.0.590 同型数据丢失）"
    assert cs.load_config()["reimburse"]["name"] == "王五"     # 同一次提交里的其它字段照常生效


def test_new_value_replaces_unreadable_cipher(app_client):
    """恢复路径就是"填新值覆盖"——不能因为保护密文而拒绝用户重填。"""
    c, cfg = app_client
    _blob("bank_name", "enc:v1:" + "E" * 300)
    c.post("/api/personal/config", json={"reimburse": {"bank_name": "招行北京分行"}})
    assert cs.load_config()["reimburse"]["bank_name"] == "招行北京分行"
    assert c.get("/api/personal/config").get_json()["reimburse"]["encrypted_fields"] == []


def test_dsh_cipher_reports_unreadable_not_tail(app_client):
    """尾号取密文末 4 位既没辨识意义又泄露密文片段。"""
    c, cfg = app_client
    def _m(cfg2):
        cfg2.setdefault("dsh", {})["api_key"] = "enc:v1:" + "R" * 200
        return True
    cs.update_config(_m)
    d = c.get("/api/personal/config").get_json()["dsh"]
    assert d["api_key_set"] is True and d["api_key_tail"] == ""
    assert d["api_key_unreadable"] is True
    assert "enc:v1" not in json.dumps(d)


def test_reimburse_written_and_encrypted_on_disk(app_client):
    c, cfg = app_client
    r = c.post("/api/personal/config", json={"reimburse": {
        "name": "张三", "bank_account": "6222020000012345678", "bank_name": "招行北京分行"}})
    assert r.get_json()["ok"] is True
    disk = _disk(cfg)["reimburse"]
    assert disk["name"].startswith("enc:"), "reimburse.name 应命中路径级 DPAPI"
    assert disk["bank_account"].startswith("enc:"), "银行账号应命中键名级 DPAPI"
    assert "6222020000012345678" not in cfg.read_text(encoding="utf-8")
    # 运行期读回明文：消费方（rule_check）拿到的必须是可用值
    live = cs.load_config()["reimburse"]
    assert live["name"] == "张三" and live["bank_account"] == "6222020000012345678"


def test_kb_auto_writes_the_keys_the_worker_reads(app_client):
    """kb_auto 的键名必须是 organize/digest（knowledge_ops 只认这两个）。"""
    c, cfg = app_client
    c.post("/api/personal/config", json={"background": {"kb_organize": False, "kb_digest": True}})
    ka = _disk(cfg)["kb_auto"]
    assert ka["organize"] is False and ka["digest"] is True
    assert "enabled" not in ka, "写成 enabled 等于写了个没人读的键"
    ka2 = cs.load_config().get("kb_auto")
    assert bool(ka2.get("organize", True)) is False


def test_digests_time_and_todo_watch(app_client):
    c, cfg = app_client
    c.post("/api/personal/config", json={"background": {"digests_daily": False,
                                                        "digests_time": "07:05", "todo_watch": True}})
    dg = _disk(cfg)["digests"]["daily"]
    assert dg["enabled"] is False and dg["time"] == "07:05"
    from services import daily_digest, todo_watch
    assert daily_digest._cfg(cs.load_config())["time"] == "07:05"
    assert todo_watch._cfg(cs.load_config())["enabled"] is True


def test_bad_time_rejected_without_writing(app_client):
    c, cfg = app_client
    before = cfg.read_text(encoding="utf-8")
    r = c.post("/api/personal/config", json={"background": {"digests_time": "25:99"}})
    assert r.status_code == 400 and "HH:MM" in r.get_json()["error"]
    assert _disk(cfg).get("digests", {"daily": {}})["daily"].get("time", "18:30") == "18:30"
    assert before is not None


def test_api_key_not_overwritten_when_absent(app_client):
    """只改名字时不能把 dsh 凭据抹掉（与连接器密码同口径）。"""
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("dsh", {}).__setitem__("api_key", "sk-keepme"), True)[1])
    c.post("/api/personal/config", json={"reimburse": {"name": "李四"}})
    assert cs.load_config()["dsh"]["api_key"] == "sk-keepme"
    # 给了新值才覆盖
    c.post("/api/personal/config", json={"dsh": {"api_key": "sk-newvalue"}})
    assert cs.load_config()["dsh"]["api_key"] == "sk-newvalue"
    # 空串不算新值（否则输入框未聚焦就保存会清空凭据）
    c.post("/api/personal/config", json={"dsh": {"api_key": "  ", "base_url": "http://x"}})
    assert cs.load_config()["dsh"]["api_key"] == "sk-newvalue"
    assert cs.load_config()["dsh"]["base_url"] == "http://x"


def test_clear_key_explicit(app_client):
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("dsh", {}).__setitem__("api_key", "sk-gone"), True)[1])
    c.post("/api/personal/config", json={"dsh": {"clear_key": True}})
    # api_key 是 DEFAULT_CONFIG.dsh 声明过的键，pop 后合并会回填 ""——判据是"取不到值"
    assert not cs.load_config()["dsh"]["api_key"]
    assert c.get("/api/personal/config").get_json()["dsh"]["api_key_set"] is False


def test_empty_payload_rejected(app_client):
    c, _ = app_client
    assert c.post("/api/personal/config", json={}).status_code == 400


def test_long_values_capped(app_client):
    """这些值会拼进发给大模型的提示词，必须限长。"""
    c, cfg = app_client
    c.post("/api/personal/config", json={"reimburse": {"name": "甲" * 500}})
    assert len(cs.load_config()["reimburse"]["name"]) == 40


def _js():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "js", "11-workspace-kb-study.js"), encoding="utf-8") as f:
        return f.read()


def test_frontend_refuses_save_before_load_succeeded():
    """GET 失败而 POST 成功是可能的（读的时候后端刚重启）——那时输入框是空的，
    不加闸点保存就等于把真实姓名/银行账号存成空值。"""
    js = _js()
    assert "let _persLoaded=false;" in js
    i0 = js.index("async function loadPersonal()")
    i1 = js.index("async function savePersonal(")
    load = js[i0:i1]
    fetch_at = load.index('getJson("/api/personal/config")')   # v1.0.655 C档：fetch 收敛为 getJson，锁随新形态（语义不变）
    assert load.index("_persLoaded=false") < fetch_at, "进函数没先清零：成功后再失败仍被当成已加载"
    assert load.index("_persLoaded=true") > load.index("bg_todo_watch"), "在读完最后一项之前就放行保存"
    for fn in ("savePersonal", "saveBackground"):
        body = js[js.index("async function %s(" % fn):][:200]
        assert "if(!_persLoaded)" in body, "%s 缺『未加载成功就别保存』闸" % fn


# ============================================================ 来自 test_v616_macro_webhook.py（v1.0.616）


@pytest.fixture()
def macro(tmp_path, monkeypatch):
    """把宏目录指到 tmp，并记录 dispatch_tool 的真实调用序列（判据：它被没被调用）。"""
    import routes.b_macro as BM
    d = tmp_path / "宏"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(BM, "_macro_dir", lambda: str(d))
    calls = []

    def _write(calls_):
        (d / "macro_t1.json").write_text(json.dumps(
            {"name": "测试宏", "calls": calls_}, ensure_ascii=False), encoding="utf-8")
        return "macro_t1"
    return _write, calls


def _patch_dispatch(monkeypatch, calls):
    import agent_tools
    def _fake(name, args, cfg, **kw):
        calls.append(name)
        return {"ok": True}
    monkeypatch.setattr(agent_tools, "dispatch_tool", _fake)
    return calls


# ---------------- 宏重放必须过闸门 ----------------

def test_macro_write_step_is_gated_and_does_not_execute(app_client, macro, monkeypatch):
    write, calls = macro
    mid = write([{"name": "click_wecom_element", "args": {"selector": "#approve"}}])
    _patch_dispatch(monkeypatch, calls)
    import routes.b_macro as BM
    monkeypatch.setattr(BM, "load_config",
                        lambda: {"security": {"permission_level": "ask_every"}})
    c, _ = app_client
    body = c.post("/api/macro/run", json={"id": mid}).get_data(as_text=True)
    assert '"approval_request"' in body, body[:400]
    assert '"macro_paused"' in body, body[:400]
    assert calls == [], "「每次询问」档下宏仍把写操作真实执行了"


def test_macro_always_ask_hits_even_in_auto_level(app_client, macro, monkeypatch):
    """run_audit_flow(auto_approve=true) 是参数级不可撤销写：auto 档也要问。"""
    write, calls = macro
    mid = write([{"name": "run_audit_flow", "args": {"flow": "monthly", "auto_approve": True}}])
    _patch_dispatch(monkeypatch, calls)
    import routes.b_macro as BM
    monkeypatch.setattr(BM, "load_config", lambda: {"security": {"permission_level": "auto"}})
    c, _ = app_client
    body = c.post("/api/macro/run", json={"id": mid}).get_data(as_text=True)
    assert '"approval_request"' in body, body[:400]
    assert calls == []


def test_macro_read_steps_run_free(app_client, macro, monkeypatch):
    write, calls = macro
    mid = write([{"name": "read_wecom_page", "args": {}}, {"name": "list_wecom_windows", "args": {}}])
    _patch_dispatch(monkeypatch, calls)
    import routes.b_macro as BM
    monkeypatch.setattr(BM, "load_config",
                        lambda: {"security": {"permission_level": "ask_every"}})
    c, _ = app_client
    body = c.post("/api/macro/run", json={"id": mid}).get_data(as_text=True)
    assert calls == ["read_wecom_page", "list_wecom_windows"], calls
    assert '"macro_done"' in body and '"approval_request"' not in body


def _run(c, mid, extra=None):
    """SSE 路由的生成器是**惰性**的：不读 body 就一步都不执行（第一版测试就栽在这——
    断言"续跑执行了 1 步"却量到 0 步，不是代码错，是测试没消费流）。"""
    r = c.post("/api/macro/run", json=dict({"id": mid}, **(extra or {})))
    body = r.get_data(as_text=True)
    r.close()
    return body


def test_macro_resume_skips_executed_prefix(app_client, macro, monkeypatch):
    """批准后必须从**未执行那一步**续跑：从头重放会把已点过的「通过」再点一次。"""
    write, calls = macro
    mid = write([{"name": "click_wecom_element", "args": {"s": 1}},
                 {"name": "click_wecom_element", "args": {"s": 2}}])
    _patch_dispatch(monkeypatch, calls)
    import routes.b_macro as BM
    monkeypatch.setattr(BM, "load_config",
                        lambda: {"security": {"permission_level": "auto"}})
    c, _ = app_client
    body = _run(c, mid, {"start": 1})
    assert calls == ["click_wecom_element"], calls
    assert '"from": 1' in body, body[:200]
    assert '"macro_done"' in body


def test_macro_illegal_start_clamped(app_client, macro, monkeypatch):
    write, calls = macro
    mid = write([{"name": "read_wecom_page", "args": {}}])
    _patch_dispatch(monkeypatch, calls)
    c, _ = app_client
    for bad in ("abc", -5, 999999999):
        body = _run(c, mid, {"start": bad})
        assert '"macro_start"' in body, (bad, body[:160])
    assert 1 <= len(calls) <= 3, calls      # 越界只可能"少跑"，不能 500、也不能跑飞


def test_macro_route_no_longer_dispatches_ungated():
    """源码锁：重放循环里 dispatch 之前必须先算闸门，且不再有无 gate 的第二条路。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "routes", "b_macro.py"), encoding="utf-8").read()
    assert "gate_write_tool" in src
    seg = src[src.index("def _gen()"):src.index("def _gen()") + 2600]
    assert seg.index("gate_write_tool") < seg.index("dispatch_tool"), "又回到先执行后判断"
    assert "__approval_needed__" in seg and "macro_paused" in seg


# ---------------- webhook 不被空串清空 ----------------

def test_webhook_survives_unrelated_save(app_client):
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("wecom", {}).__setitem__("webhook", "https://qyapi/x?key=S"), True)[1])
    c.post("/api/config", json={"auto_launch": True, "silent_mode": False})   # 不带 webhook 字段
    assert cs.load_config()["wecom"]["webhook"] == "https://qyapi/x?key=S"
    # 旧写法的前端一定会带 webhook:""（输入框恒空），这条就是那个场景
    c.post("/api/config", json={"auto_launch": False, "webhook": ""})
    assert cs.load_config()["wecom"]["webhook"] == "https://qyapi/x?key=S", "空串把地址清空了"
    c.post("/api/config", json={"webhook": "   "})
    assert cs.load_config()["wecom"]["webhook"] == "https://qyapi/x?key=S"


def test_webhook_new_value_and_explicit_clear(app_client):
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("wecom", {}).__setitem__("webhook", "https://old"), True)[1])
    c.post("/api/config", json={"webhook": "https://new"})
    assert cs.load_config()["wecom"]["webhook"] == "https://new"
    c.post("/api/config", json={"clear_webhook": True})
    assert not str(cs.load_config()["wecom"].get("webhook") or "").strip()


def test_get_config_reports_presence_not_secret(app_client):
    c, cfg = app_client
    cs.update_config(lambda k: (k.setdefault("wecom", {}).__setitem__("webhook", "https://qyapi/y?key=S"), True)[1])
    body = c.get("/api/config").get_data(as_text=True)
    assert "https://qyapi/y" not in body, "密钥地址又外泄了"
    d = c.get("/api/config").get_json()
    assert d["wecom"]["webhook_set"] is True
    cs.update_config(lambda k: (k.setdefault("wecom", {}).__setitem__("webhook", ""), True)[1])
    assert c.get("/api/config").get_json()["wecom"]["webhook_set"] is False


def test_frontend_does_not_send_blank_webhook():
    """前端同口径：留空就不带这个键（后端也兜，两层都要对）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "js", "05-connectors-status.js"), encoding="utf-8").read()
    i = js.index("async function saveWecomOpt")
    seg = js[i:i + 900]
    assert "if(wh) payload.webhook" in seg, "payload 仍无条件带 webhook 字段"
    assert "clearWecomWebhook" in js and "webhook_set" in js
    html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
    assert 'id="webhookState"' in html and 'id="webhookClearBtn"' in html

# ============================================================ 来自 test_v631_cfg_cache_key.py（v1.0.631）


def _write(path, base_url):
    path.write_text(json.dumps({"dsh": {"base_url": base_url}}), encoding="utf-8")


def _clear_cache():
    cs._invalidate_cfg_cache()


# ---------------------------------------------------------------- 跨文件不冒领
def test_same_mtime_different_path_must_not_cross(tmp_path, monkeypatch):
    """A 存盘后把 B 的 mtime 造成与 A 完全相同：读 B 必须拿到 B 的内容。"""
    a, b = tmp_path / "configA.json", tmp_path / "configB.json"
    _write(a, "FROM-A")
    _clear_cache()
    monkeypatch.setattr(cs, "config_path", lambda: str(a))

    def _set(c):
        c.setdefault("dsh", {})["base_url"] = "FROM-A"
        return True     # update_config 的约定：回调返回真值才落盘
    cs.update_config(_set)
    assert cs.load_config()["dsh"]["base_url"] == "FROM-A"
    st_a = os.stat(a)

    _write(b, "FROM-B")
    # 补齐成与 A 完全相同的字节数：这条测的是"跨路径不冒领"，不能让 size 那一维替它过关
    _pad = st_a.st_size - os.path.getsize(str(b))
    if _pad > 0:
        with open(b, "a", encoding="utf-8") as f:
            f.write(" " * _pad)
    os.utime(b, ns=(st_a.st_atime_ns, st_a.st_mtime_ns))       # 刻意同刻度、同长度
    assert os.stat(b).st_mtime_ns == st_a.st_mtime_ns, "造不出同 mtime，这条测不到东西"
    assert os.stat(b).st_size == st_a.st_size, "两份长度不同，测不到 path 这一维"

    # 关键一步：缓存里现在是 A 的内容 + A 的指纹，切到同指纹的 B 必须**不命中**
    cs._CFG_CACHE["cfg"] = json.loads(json.dumps({"dsh": {"base_url": "FROM-A"}}))
    cs._CFG_CACHE["path"] = str(a)
    cs._CFG_CACHE["stamp"] = (st_a.st_mtime_ns, st_a.st_size)
    monkeypatch.setattr(cs, "config_path", lambda: str(b))
    got = cs.load_config()["dsh"]["base_url"]
    assert got == "FROM-B", f"缓存按 mtime 冒领了另一份配置：读到 {got!r}"


def test_same_path_same_mtime_different_size_is_not_a_hit(tmp_path, monkeypatch):
    """v1.0.634：同一文件被外部改写、mtime 撞在同一刻度上时，靠 size 这一维也要失效。"""
    a = tmp_path / "config.json"
    _write(a, "SHORT")
    _clear_cache()
    monkeypatch.setattr(cs, "config_path", lambda: str(a))
    assert cs.load_config()["dsh"]["base_url"] == "SHORT"
    st = os.stat(a)

    _write(a, "MUCH-LONGER-VALUE")
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))          # mtime 钉回旧值
    assert os.stat(a).st_mtime_ns == st.st_mtime_ns
    assert os.stat(a).st_size != st.st_size
    assert cs.load_config()["dsh"]["base_url"] == "MUCH-LONGER-VALUE", \
        "mtime 没变就认定内容没变——本机 76% 的背靠背改写都会命中这个坑"


def test_save_config_invalidates_even_when_mtime_and_size_unchanged(tmp_path, monkeypatch):
    """本进程写盘后必须立刻读到新值：等长改写 + mtime 钉回旧值，模拟"指纹完全没变"。

    这是 v1.0.634 那一次全量红的真实形状——`save_config` 写完不作废缓存，
    下一次 `load_config` 在同一 mtime 刻度里读回旧配置，路由据此合并后把
    唯一一份密文当成"没有这个字段"写空。"""
    a = tmp_path / "config.json"
    _clear_cache()
    monkeypatch.setattr(cs, "config_path", lambda: str(a))
    cs.save_config({"dsh": {"base_url": "AAAA"}})
    assert cs.load_config()["dsh"]["base_url"] == "AAAA"
    st = os.stat(a)

    cs.save_config({"dsh": {"base_url": "BBBB"}})             # 与上一份等长
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))          # 指纹钉回写之前
    assert (os.stat(a).st_mtime_ns, os.stat(a).st_size) == (st.st_mtime_ns, st.st_size), \
        "构造失败：这份盘能造出同指纹的等长改写才有意义"
    assert cs.load_config()["dsh"]["base_url"] == "BBBB", \
        "写盘后没作废缓存：同指纹改写读回来还是旧配置"


def test_cache_still_hits_for_the_same_file(tmp_path, monkeypatch):
    """反向：同一路径同一指纹仍要走缓存（这条优化是 v1.0.364 H7 来的，不能退化）。"""
    a = tmp_path / "config.json"
    _write(a, "X")
    _clear_cache()
    monkeypatch.setattr(cs, "config_path", lambda: str(a))
    cs.load_config()
    cs._CFG_CACHE["path"] = str(a)
    cs._CFG_CACHE["stamp"] = cs._cfg_stamp(str(a))
    cs._CFG_CACHE["cfg"] = {"dsh": {"base_url": "CACHED"}}
    assert cs.load_config()["dsh"]["base_url"] == "CACHED", "缓存不该失效却失效了（白丢优化）"


# ---------------------------------------------------------------- 源码锁
def test_cache_key_is_path_and_stamp():
    import inspect
    src = inspect.getsource(cs)
    assert '"path": None' in src and '"stamp": None' in src, "缓存字典没带 path/stamp 两维"
    assert '_CFG_CACHE.get("path") == _cp' in src, "命中判定没比路径"
    assert '_CFG_CACHE["stamp"] == _st' in src, "命中判定没比内容指纹"
    assert "st_mtime_ns" in src and "st_size" in src, "指纹退化成只看 mtime"
    assert '_CFG_CACHE["mtime"]' not in src, "旧的 mtime 单键判定还留着（改了一半）"


def test_every_write_path_invalidates_the_cache():
    """两个写盘点都要作废缓存，漏一个就是"save 完 load 回旧值"。"""
    import inspect
    src = inspect.getsource(cs)
    for fn in ("def save_config(", "def update_config("):
        body = src[src.find(fn):]
        body = body[:body.find("\ndef ")]
        assert "_invalidate_cfg_cache()" in body, fn


def test_measured_on_this_disk_mtime_is_too_coarse_to_trust():
    """把"为什么不能只看 mtime"变成一条会随环境变化的实测记录（不判红，只报数）。"""
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    collisions = 0
    for i in range(60):
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"i": i}))
        a = os.stat(p).st_mtime_ns
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"i": i + 1000}))
        if os.stat(p).st_mtime_ns == a:
            collisions += 1
    print("[cfg-cache] 60 次背靠背改写里 mtime 未变的次数：%d" % collisions)
    assert collisions >= 0

# ============================================================ 来自 test_v642_archive_settings.py（v1.0.642）


import core  # noqa: E402
from services import conversations  # noqa: E402


from conftest import _src  # noqa: E402


def _code(text, kind="js"):
    """剥注释：只剥整行注释（`//` 也出现在 "https://…" 字符串里，全局剥会吞掉代码）。"""
    s = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if kind == "js":
        s = re.sub(r"(?m)^[ \t]*//.*$", " ", s)
    elif kind == "html":
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    return s


HTML, CSS = _src("index.html"), _src("css", "style.css")
JS02, JS10 = _src("js", "02-nav-quicklinks.js"), _src("js", "10-chat.js")


@pytest.fixture
def tmp_ws(monkeypatch):
    d = tempfile.mkdtemp(prefix="wec_arch_")
    monkeypatch.setattr(core.paths, "workspace_root", lambda: d)
    monkeypatch.setattr(conversations, "workspace_root", lambda: d)
    return d


def _body(src, fn):
    """取一个函数的函数体（到下一个顶层 `function` 为止），并**先剥注释**——
    本项目注释里会原样引用被删掉的旧写法，不剥就会自己判红自己。"""
    i = src.index(fn)
    j = src.index("\nfunction ", i + len(fn))
    return _code(src[i:j], "js")


# ---------------------------------------------------------------- ① 后端：归档 ↔ 恢复
def test_archive_then_unarchive_round_trip(tmp_ws):
    cid = conversations.create(title="跑一遍学习中心")
    conversations.append_message(cid, "agent", "已完成 3 门课")
    ok, _ = conversations.archive(cid, summarize=False)
    assert ok and conversations.load(cid)["archived"] is True
    assert conversations.unarchive(cid) is True
    assert conversations.load(cid)["archived"] is False
    assert conversations.unarchive("不存在的会话") is False


def test_list_all_reports_the_flag_and_the_message_count(tmp_ws):
    cid = conversations.create(title="合并凭证")
    conversations.append_message(cid, "user", "合并这 4 张")
    conversations.append_message(cid, "agent", "已合并")
    conversations.archive(cid, summarize=False)
    row = [x for x in conversations.list_all() if x["id"] == cid]
    assert len(row) == 1, row
    assert row[0]["archived"] is True and len(row[0]["messages"]) == 2, row[0]


# ---------------------------------------------------------------- ③ 归档不再被自动清掉
def test_cleanup_no_longer_deletes_archived(tmp_ws, monkeypatch):
    """超上限时原先"挑最旧的归档删"——现在只告警不删。
    这是"保留"两个字的硬承诺：静默少一条记录，用户永远发现不了。"""
    monkeypatch.setattr(conversations, "_MAX_KEEP", 2)
    cids = [conversations.create(title="旧任务 %d" % i) for i in range(5)]
    for c in cids:
        conversations.archive(c, summarize=False)
    conversations._cleanup_old()
    assert len(conversations.list_all()) == 5, "归档被自动删除了"


def test_cleanup_leaves_live_conversations_alone(tmp_ws, monkeypatch):
    monkeypatch.setattr(conversations, "_MAX_KEEP", 1)
    c = conversations.create(title="还在跑的任务")
    conversations._cleanup_old()
    assert conversations.load(c), "未归档的会话被清了"


# ---------------------------------------------------------------- ① 侧栏不再渲染归档项
def test_rail_drops_the_archived_section():
    rail = _body(JS10, "function renderTaskRail")
    assert "已归档" not in rail, "侧栏又在渲染归档段"
    assert "m.archived" in rail, "没有把归档项挡在列表之外"
    row = _body(JS10, "function _trRow")
    assert "m.archived" not in row and ".arch" not in row, \
        "_trRow 还在分支归档行（这种行已不可能出现，留着就是死分支）"
    menu = _body(JS10, "function trMenu")
    assert "取消归档" not in menu, "侧栏菜单还留着「取消归档」——归档项已经不在侧栏了"


def test_arch_row_css_rule_is_removed():
    assert ".tr-item.arch" not in _code(CSS, "css"), \
        "归档行灰化规则要随渲染一起删（留着就是永不命中的死选择器）"


# ---------------------------------------------------------------- ② 设置里的落脚地
def test_settings_has_the_archived_card():
    i = HTML.index("📥 已归档任务")
    seg = _code(HTML[i - 200:i + 1000], "html")
    assert 'id="archivedListBox"' in seg and 'id="archivedCount"' in seg
    assert "永久删除" in seg and "恢复" in seg, "卡片要写清楚两个动作各自的后果"
    assert "离开" in seg, "要说明归档后它不在任务列表里"


def test_openview_refreshes_the_archived_list():
    line = JS02[JS02.index('name==="settings"'):]
    line = line[:line.index("\n")]
    assert "loadArchivedList()" in line, "进设置页不刷新归档列表"
    assert "try{ loadArchivedList(); }catch(e){}" in line, \
        "要单独 try/catch：一处死引用不该连坐同一条链上后面的加载"


def test_restore_and_purge_are_wired():
    for fn in ("async function loadArchivedList(", "async function restoreArchived(",
               "function purgeArchived("):
        assert fn in JS10, "缺 %s" % fn
    assert "/api/conversations/unarchive" in JS10, "恢复没打后端"
    # 永久删除必须复用那一条删除路径（确认弹层 / abort / 容器清理 / __curConv 复位都在里面）。
    # 另写一份 fetch+清理 就是第二个真相，迟早和侧栏删除对不上——所以这里判"没有 fetch"。
    p = JS10[JS10.index("function purgeArchived("):]
    p = p[:p.index("\n}")]
    assert "deleteConversation(id, loadArchivedList," in p, "永久删除没复用统一删除路径"
    assert "永久删除归档任务" in p, "确认弹层的标题要和点的那个按钮是同一件事"
    assert "fetch(" not in p, "purgeArchived 自己又发了一份删除请求"


def test_unarchive_route_exists_and_validates():
    src = _src("routes", "conversations.py")
    assert '"/conversations/unarchive"' in src
    i = src.index("def api_conv_unarchive")
    body = src[i:src.index("\n\n\n", i)]
    assert 'data.get("id")' in body and "400" in body, "缺 id 要 400，不能静默成功"
    assert "conversations.unarchive" in body


# ---------------------------------------------------------------- 文案跟着口径走
def test_archive_toast_points_at_the_new_home():
    """归档后东西"消失"了，必须在提示里说清去哪里找回——否则用户第一反应是丢了。"""
    i = JS10.index("async function archiveConversation(")
    body = JS10[i:JS10.index("\n}", i)]
    assert "设置 → 已归档任务" in body, "归档提示没指明去处"
    menu = _body(JS10, "function trMenu")
    assert "设置 → 已归档任务" in menu, "⋯ 菜单里也要在点之前说明归档的后果"


def test_no_stale_copy_promises_the_old_behaviour():
    """全仓扫一遍：不许还有地方（代码，不是注释）写着"归档项会留在任务列表里"。"""
    for f, kind in (("index.html", "html"), ("js/10-chat.js", "js"),
                    ("js/11-workspace-kb-study.js", "js"), ("css/style.css", "css")):
        t = _code(_src(*f.split("/")), kind)
        assert "已归档段" not in t, f
