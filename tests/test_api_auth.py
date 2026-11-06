# -*- coding: utf-8 -*-
"""tests/test_api_auth.py — v1.0.405（H1）API 鉴权专项测试。
验证：①无 cookie 调 /api/* → 401；②带正确 cookie → 200；③错误 Host → 403；
④/ 路由响应 Set-Cookie；⑤conftest 豁免开关恢复后仍生效。
注意：与其它测试同进程共享 backend.app，临时翻转 _AUTH_DISABLED 并在
finally 恢复（pytest 默认顺序执行，无并发冲突）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _client():
    import backend
    # use_cookies=False：Werkzeug 3.1 的 test client 默认 cookie jar 会在每个请求前
    # pop 掉手动设置的 HTTP_COOKIE（tests/test_api_auth.py 里显式带 Cookie 头会被吃掉）
    return backend, backend.app.test_client(use_cookies=False)


def test_api_requires_cookie():
    backend, c = _client()
    old = backend._AUTH_DISABLED
    backend._AUTH_DISABLED = False
    try:
        # ① 无 cookie → 401
        r = c.get("/api/status")
        assert r.status_code == 401, r.status_code
        # ② 错误 cookie → 401
        r = c.get("/api/status", headers={"Cookie": "__wc_token=bad"})
        assert r.status_code == 401, r.status_code
        # ③ 正确 cookie → 放行（200；/api/status 本身不依赖外部服务）
        tok = backend._api_token()
        assert tok and len(tok) >= 32, "token 应已生成且 ≥32 字符"
        r = c.get("/api/status", headers={"Cookie": f"__wc_token={tok}"})
        assert r.status_code == 200, (r.status_code, r.data[:200])
        # ④ / 路由应种下 HttpOnly cookie
        r = c.get("/")
        sc = r.headers.get("Set-Cookie", "")
        assert "__wc_token=" in sc and "HttpOnly" in sc and "Strict" in sc, sc
        # ⑤ Werkzeug 3.x test_client 无 cookie jar——模拟浏览器：从 / 响应提取 cookie
        # 再显式回传（真实 webview/浏览器由 cookie jar 自动完成同一行为）
        token_from_cookie = sc.split("__wc_token=", 1)[1].split(";", 1)[0]
        r = c.get("/api/status", headers={"Cookie": f"__wc_token={token_from_cookie}"})
        assert r.status_code == 200, r.status_code
    finally:
        backend._AUTH_DISABLED = old


def test_api_host_whitelist():
    backend, c = _client()
    old = backend._AUTH_DISABLED
    backend._AUTH_DISABLED = False
    try:
        tok = backend._api_token()
        # 恶意域名（DNS rebinding 场景 Host 是攻击者域名）→ 403（即使带对 cookie）
        r = c.get("/api/status", headers={"Host": "evil.example.com:8787",
                                          "Cookie": f"__wc_token={tok}"})
        assert r.status_code == 403, r.status_code
        # 静态路径也校验 Host（index/css/js 无敏感但统一入口更干净）
        r = c.get("/", headers={"Host": "evil.example.com:8787"})
        assert r.status_code == 403, r.status_code
    finally:
        backend._AUTH_DISABLED = old


def test_auth_disabled_escape_hatch():
    """conftest 豁免后其余测试不受影响（豁免态直接 200，不做 token 校验）。"""
    backend, c = _client()
    assert backend._AUTH_DISABLED is True, "conftest 应已置 True"
    r = c.get("/api/status")
    assert r.status_code == 200, r.status_code


def test_token_file_is_hardened_before_content_is_written(monkeypatch, tmp_path):
    """v1.0.646：token 明文绝不能先落到默认继承权限下再收紧。

    旧实现顺序是 写明文 → 收紧 ACL，中间有一个「任意本机账户可读 token」的窗口
    （毫秒级，但 token 是本机 API 的唯一闸门）。锁的是**顺序**：内容写入那一刻，
    收紧动作必须已经发生过。
    """
    import backend as B

    events = []
    tok_path = str(tmp_path / "api_token.txt")

    def fake_harden(p):
        events.append(("harden", p))
        return None

    real_open = open

    def fake_open(p, mode="r", *a, **k):
        if "w" in str(mode) and str(p).endswith("api_token.txt"):
            events.append(("open-write", p))
        return real_open(p, mode, *a, **k)

    monkeypatch.setattr(B, "_harden_token_acl", fake_harden)
    monkeypatch.setattr(B, "open", fake_open, raising=False)
    # 捕获真正写内容的那次：直接盯 write 调用
    wrote = {"n": 0}
    real_write = None

    class _SpyFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, s):
            wrote["n"] += 1
            if s:
                events.append(("content-written", len(s)))
            return self._fh.write(s)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

    def spy_open(p, mode="r", *a, **k):
        fh = real_open(p, mode, *a, **k)
        if "w" in str(mode):
            return _SpyFile(fh)
        return fh

    monkeypatch.setattr(B, "open", spy_open, raising=False)

    assert B._persist_token(tok_path, "t" * 64) is True

    kinds = [e[0] for e in events]
    assert "content-written" in kinds, "没写到 token 内容（被测行为变了？）"
    first_content = kinds.index("content-written")
    assert "harden" in kinds[:first_content], \
        f"明文写入前没有先收紧 ACL：{kinds} —— token 明文曾暴露在默认继承权限下"


if __name__ == "__main__":
    test_api_requires_cookie()
    test_api_host_whitelist()
    test_auth_disabled_escape_hatch()
    print("PASS api auth 3 场景")
