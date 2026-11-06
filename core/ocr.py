# -*- coding: utf-8 -*-
"""core.ocr — OCR 引擎/在线安装/版本更新/图像识别（自 core_utils 拆分 v1.0.402）
"""
# -*- coding: utf-8 -*-
import os
import sys
import re
import json
import time
import threading
from core.paths import workspace_root, resource_path  # noqa
from core.logging import log_event, _LazyRequests  # noqa
from core.config import config_path  # noqa

_OCR_ENGINE = None
_OCR_ENGINE_LOCK = threading.Lock()   # v1.0.361 审查修复（P7）：双检锁防重复实例化
_OCR_RUNTIME_STATE = None   # None=未初始化；True/False=旁挂目录是否存在（缓存判定结果）


def ensure_ocr_runtime():
    """旁挂 OCR 运行时路径（v1.0.306 启动优化）。

    cv2/onnxruntime/rapidocr_onnxruntime/numpy/shapely/pyclipper 合计约 226MB（本机实测解压后），
    占 exe 包体 66%，但只有课件采集/扫描件 OCR 用得到。它们不再打进 exe，改放 dist/ocr/
    （build.py 的 sync_ocr_runtime 从构建 venv site-packages 复制）。首次 OCR 前把该目录
    插到 sys.path 并注册 DLL 目录——效果等同把这些包装进了运行环境。
    幂等（进程内只判定一次，返回值前后一致）；目录不存在时静默返回 False
    （源码直跑用构建 venv 里的包，行为不变）。
    """
    global _OCR_RUNTIME_STATE
    if _OCR_RUNTIME_STATE is not None:
        return _OCR_RUNTIME_STATE
    try:
        d = os.path.join(os.path.dirname(config_path()), "ocr")
    except Exception:  # noqa: BLE001
        _OCR_RUNTIME_STATE = False
        return False
    if not os.path.isdir(d):
        _OCR_RUNTIME_STATE = False
        return False
    if d not in sys.path:
        sys.path.insert(0, d)
    # .libs 目录（delvewheel 式 DLL 伴生目录）注册进 DLL 搜索路径，cv2/onnxruntime 的
    # .pyd 才能找到伴生动态库；cv2/onnxruntime 自身目录一并注册（双保险）。
    try:
        for sub in sorted(os.listdir(d)):
            if sub.endswith(".libs") or sub in ("cv2", "onnxruntime", "numpy"):
                p = os.path.join(d, sub)
                if os.path.isdir(p):
                    try:
                        os.add_dll_directory(p)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as e:  # noqa: BLE001
        log_event(f"[ocr] 旁挂运行时注册 DLL 目录失败（继续尝试导入）：{e}")
    log_event(f"[ocr] 已旁挂 OCR 运行时：{d}")
    _OCR_RUNTIME_STATE = True
    return True


# ---------- OCR 运行时在线安装（v1.0.307：碰到 OCR 事务才联网下载） ----------
# 背景：1.0.306 把 OCR 栈移出 exe 后，分发机器上若没有 dist/ocr/，课件采集（PDF/视频类）
# 与扫描件识别会静默降级。这里按钉定版本清单从国内 PyPI 镜像拉 wheel、直接解压进 dist/ocr/
# （wheel 即 zip，无需 pip——冻结 exe 里没有 pip），完成后即可用。
# 版本钉定 = 与打包 venv 同版本（本组合已实测：镜像解析/下载/解压/识别全链路通过）。
_OCR_WHEELS = {
    "rapidocr-onnxruntime": "1.2.3",   # py3-none-any（含 onnx 模型）
    "opencv-python": "5.0.0.93",       # cp37-abi3
    "onnxruntime": "1.29.0",           # cp313
    "numpy": "2.5.2",                  # cp313
    "shapely": "2.1.2",                # cp313
    "pyclipper": "1.4.0",              # cp313
}
_OCR_MIRRORS = (
    "https://mirrors.aliyun.com/pypi/simple/{pkg}/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/{pkg}/",
    "https://mirrors.bfsu.edu.cn/pypi/web/simple/{pkg}/",
    "https://pypi.org/simple/{pkg}/",
)
# 体积口径（v1.0.609 本机实测）：此前 UI 里写的是一个把"下载量"和"占用量"混为一谈的
# 旧数字，两头都不对，故统一由 _OCR_DL_MB / _OCR_DISK_MB 两个常量给出，别处不再各写各的。
#   下载量 = 钉定组合 wheel 合计（PyPI JSON 实测：opencv-python 42.0 + onnxruntime 13.7
#            + numpy 12.2 + rapidocr-onnxruntime 11.8 + shapely 1.7 + pyclipper 0.1）
#   占用量 = dist/ocr 解压后实测（含 .libs 伴生 DLL 与 onnx 模型）
_OCR_DL_MB = 81
_OCR_DISK_MB = 226


def ocr_size_hint():
    """给 UI 的一句话体积说明。占用量优先用**本机实测**（目录在就算），
    没装过时回落到钉定组合的实测参考值。"""
    disk = ocr_disk_size_mb()
    if disk:
        return "下载约 %dMB、本机当前占用约 %dMB" % (_OCR_DL_MB, disk)
    return "下载约 %dMB、解压后占用约 %dMB" % (_OCR_DL_MB, _OCR_DISK_MB)
# OCR 组件目录 → wheel 包名（目录存在=已装，目录缺失=需下载）
_OCR_SUB2PKG = {
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "cv2": "opencv-python",
    "onnxruntime": "onnxruntime",
    "numpy": "numpy",
    "shapely": "shapely",
    "pyclipper": "pyclipper",
}
# 反向：wheel 包名 → 导入目录（注意 opencv-python 的目录是 cv2，不能按名字推）
_OCR_PKG2DIR = {pkg: sub for sub, pkg in _OCR_SUB2PKG.items()}
_OCR_DL = {"state": "idle", "progress": 0, "msg": "", "error": ""}   # idle|running|ready|failed
_OCR_DL_LOCK = threading.Lock()
def _ocr_needed_pkgs():
    """返回 ([缺失的 wheel 包名...], ocr 目标目录)。按目录是否存在判定，简单可靠。"""
    d = os.path.join(os.path.dirname(config_path()), "ocr")
    need = [pkg for sub, pkg in _OCR_SUB2PKG.items() if not os.path.isdir(os.path.join(d, sub))]
    return need, d


def _ocr_anchors(html):
    """从 simple 索引 HTML 取 [(wheel href, requires_python)]。
    国内镜像（阿里云/清华/北外）都带 `data-requires-python`（HTML 转义），实测
    rapidocr_onnxruntime 1.4.4 是 `&lt;3.13,&gt;=3.6` —— 拿到它就能在**下载前**判掉不兼容
    版本，不必像 v1.0.473 那样先下完 wheel 再在解压前中止（白跑 15MB）。
    索引未给该属性时返回空串，按"无约束"处理并退回下载后校验。"""
    import html as _h
    out = []
    for tag in re.findall(r"<a\b[^>]*>", html or "", re.I):
        m = re.search(r'href="([^"]+\.whl[^"]*)"', tag, re.I)
        if not m:
            continue
        rm = re.search(r'data-requires-python="([^"]*)"', tag, re.I)
        out.append((m.group(1), _h.unescape(rm.group(1)).strip() if rm else ""))
    return out


def _ocr_pick_wheel(anchors, pkg, ver):
    """从索引锚点里挑钉定版本的匹配 wheel（py3-none-any / cp313 / abi3），返回 (href, requires_python)。
    挑错版本或平台会让 3.13 冻结 exe 直接 ImportError——严格白名单匹配，不猜。"""
    want_norm = pkg.lower().replace("-", "_")
    tags = (
        f"{want_norm}-{ver}-py3-none-any.whl",
        f"{want_norm}-{ver}-cp313-cp313-win_amd64.whl",
        f"{want_norm}-{ver}-cp37-abi3-win_amd64.whl",
        f"{want_norm}-{ver}-cp312-abi3-win_amd64.whl",
    )
    for h, req in anchors:
        clean = h.split("#", 1)[0]
        if clean.rsplit("/", 1)[-1].lower() in tags:
            return clean, req
    return None, ""


def _ocr_resolve_wheel(pkg, ver):
    """镜像 simple 索引解析钉定 wheel 的下载 URL（按 _OCR_MIRRORS 顺序回退）。
    返回 (url, requires_python)——requires 为空表示索引未提供，下载后仍需校验。"""
    last = []
    for mtpl in _OCR_MIRRORS:
        url = mtpl.format(pkg=pkg)
        try:
            import requests
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                last.append(f"{url} -> HTTP {r.status_code}")
                continue
            anchors = _ocr_anchors(r.text)
            hit, req = _ocr_pick_wheel(anchors, pkg, ver)
            if hit:
                full = hit if hit.startswith("http") else url.rsplit("/", 1)[0] + "/" + hit
                return full, req
            last.append(f"{url} -> 索引 {len(anchors)} 条无匹配 wheel")
        except Exception as e:  # noqa: BLE001
            last.append(f"{url} -> {type(e).__name__}: {e}")
    raise RuntimeError(f"{pkg}=={ver} 全部镜像解析失败：\n" + "\n".join(last))


def ocr_install_status():
    """OCR 组件安装状态（设置页 /api/ocr/status 用）。
    v1.0.361（P7）：快照读取走锁，防撕裂中间态。"""
    with _OCR_DL_LOCK:
        st = dict(_OCR_DL)
    st["installed"] = all(
        os.path.isdir(os.path.join(os.path.dirname(config_path()), "ocr", sub))
        for sub in _OCR_SUB2PKG)
    st["selfcheck"] = ocr_selfcheck_status()   # v1.0.608：组件页要看到"真能不能识别"
    return st


def install_ocr_async(versions=None, force=False):
    """触发 OCR 组件后台安装/更新（幂等：运行中不重复触发）。

    versions: {dashed_pkg: version} 期望版本表；None=推荐钉定版本（_OCR_WHEELS）。
    force=False：只装「目录缺失」的组件（首次安装场景）。
    force=True：已装但版本 != 期望 的组件也重装（「更新到最新」/「恢复推荐版本」用）。
    """
    want = dict(_OCR_WHEELS if versions is None else versions)
    with _OCR_DL_LOCK:
        if _OCR_DL["state"] == "running":
            return dict(_OCR_DL)
        need, d = _ocr_needed_pkgs()
        if force:
            installed = ocr_versions()
            need = [pkg for pkg in want
                    if not os.path.isdir(os.path.join(d, _OCR_PKG2DIR.get(pkg, pkg.lower().replace("-", "_"))))
                    or installed.get(pkg) != want[pkg]]
        if not need:
            _OCR_DL.update(state="ready", progress=100, msg="OCR 组件已就绪", error="")
            return dict(_OCR_DL)
        _OCR_DL.update(state="running", progress=0,
                       msg=f"待下载 {len(need)} 个组件（{ocr_size_hint()}，一次性）", error="")
    log_event(f"[ocr] 触发在线安装：{need}（期望版本 { {p: want[p] for p in need} }）")
    threading.Thread(target=_ocr_install_worker, daemon=True,
                     kwargs={"want": want, "need": need}).start()
    return dict(_OCR_DL)


def _ocr_wheel_python_ok(wp):
    """v1.0.473：解压前校验 wheel 的 Requires-Python——py3-none-any 通用包的文件名
    tag 不携带解释器版本约束，1.4.4 声明 Requires-Python <3.13 曾被装进 3.13 冻结
    exe 环境（zip 读 METADATA 零成本，免 packaging 依赖）。
    返回 (ok, requires_python 或 None)。"""
    try:
        import zipfile
        with zipfile.ZipFile(wp) as z:
            for n in z.namelist():
                if n.endswith(".dist-info/METADATA"):
                    txt = z.read(n).decode("utf-8", "replace")
                    for ln in txt.splitlines():
                        if ln.startswith("Requires-Python:"):
                            return True, ln.split(":", 1)[1].strip()
                    return True, None
    except Exception:  # noqa: BLE001
        pass
    return True, None


def _pyver_satisfies(req, pv):
    """极简 Requires-Python 判定（支持 >=/<=/>/</==/!= 逗号分段；免 packaging 依赖）。
    pv: (major, minor, micro)。无法解析的分段忽略（宽松放行）。"""
    if not req:
        return True
    def norm(v):
        n = [int(x) for x in re.findall(r"\d+", str(v))[:3]]
        while len(n) < 3:
            n.append(0)
        return n
    _pv = norm(".".join(map(str, pv)))
    for p in str(req).split(","):
        p = p.strip()
        m = re.match(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)$", p)
        if not m:
            continue
        op, v_raw, v = m.group(1), m.group(2), norm(m.group(2))
        # ==/!= 只比较用户给出的位数（==3.13 匹配 3.13.x 任意 micro）
        nv = len(re.findall(r"\d+", v_raw))
        if op == ">=" and not _pv >= v: return False
        if op == "<":
            if not _pv < v: return False
            continue
        if op == "<=":
            # <=3.10 语义允许 3.10.x（micro 未给时补 999 上界，对齐 pip 行为）
            _vhi = v if nv >= 3 else ([v[0], v[1], 999] if nv == 2 else [v[0], 999, 999])
            if not _pv <= _vhi: return False
            continue
        if op == ">" and not _pv > v: return False
        if op == "==" and _pv[:nv] != v[:nv]: return False
        if op == "!=" and _pv[:nv] == v[:nv]: return False
    return True


def _ocr_install_worker(want=None, need=None):
    """后台安装线程：解析镜像 → 逐个下载（.part 防半包）→ 解压进 dist/ocr → 校验 → 记版本 → 重挂路径。"""
    want = want or dict(_OCR_WHEELS)
    try:
        if need is None:
            need, d = _ocr_needed_pkgs()
        else:
            d = os.path.join(os.path.dirname(config_path()), "ocr")
        if not need:
            with _OCR_DL_LOCK:
                _OCR_DL.update(state="ready", progress=100, msg="OCR 组件已就绪")
            return
        resolved = []
        cur_py = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        for pkg in need:
            ver = want.get(pkg) or _OCR_WHEELS.get(pkg)
            url, req = _ocr_resolve_wheel(pkg, ver)
            resolved.append((pkg, url, req))
            log_event(f"[ocr] 安装源 {pkg}=={ver} -> {url.rsplit('/', 1)[-1]}"
                      + (f"（Requires-Python {req}）" if req else ""))
            # v1.0.608 解析期预检：索引已给出 Requires-Python 就当场拦下，
            # 不再像 v1.0.473 那样先下完 wheel 才在解压前中止（实测白下 ~15MB）
            if req and not _pyver_satisfies(req, cur_py):
                _pin = _OCR_WHEELS.get(pkg, "")
                with _OCR_DL_LOCK:
                    _OCR_DL.update(state="failed",
                                   error=f"{pkg}=={ver} 声明 Requires-Python {req}，与当前 Python "
                                         f"{cur_py[0]}.{cur_py[1]} 不兼容（下载前已拦下，未耗流量）。"
                                         f"要装请点「恢复推荐版本」（该包推荐版本 {_pin}，已实测可跑）")
                log_event(f"[ocr] {pkg}=={ver} Requires-Python {req} 解析期拦下（当前 {cur_py[:2]}）")
                return
        os.makedirs(d, exist_ok=True)
        import tempfile
        import zipfile
        import traceback
        tmp = os.path.join(tempfile.gettempdir(), "WeComCompanion_ocr_dl")
        os.makedirs(tmp, exist_ok=True)
        # v1.0.616（审查 P2）：先把上一轮的残留清掉。原实现只在"解压成功后"删 wheel，
        # 而 Requires-Python 不兼容 / 解压抛错 / 网络中断三条失败路径都不删——
        # 数百 MB 的 .whl/.part 就一直躺在 %TEMP% 里（失败一次留一坨，同 613 的
        # asr_staging 那次）。每次安装本来就要重下，起手清空没有副作用。
        for _stale in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, _stale))
            except OSError:  # 正在被别的安装用（同一 tmp 目录只会有一个安装在跑）
                pass
        n = len(resolved)
        for i, (pkg, url, req_idx) in enumerate(resolved):
            with _OCR_DL_LOCK:   # v1.0.355：状态写入补锁（原 msg/progress 跨线程无锁写）
                _OCR_DL["msg"] = f"下载 {pkg}（{i + 1}/{n}）…"
            import requests
            r = requests.get(url, timeout=(20, 900), stream=True)
            r.raise_for_status()
            cl = int(r.headers.get("Content-Length") or 0)
            got, base = 0, i * 100.0 / n
            wp = os.path.join(tmp, url.rsplit("/", 1)[-1])
            with open(wp + ".part", "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
                    if cl:
                        with _OCR_DL_LOCK:
                            _OCR_DL["progress"] = int(base + (got / cl) * 100.0 / n)
            os.replace(wp + ".part", wp)
            with _OCR_DL_LOCK:
                _OCR_DL["msg"] = f"解压 {pkg}（{i + 1}/{n}）…"
                _OCR_DL["progress"] = int((i + 1) * 100.0 / n)
            # v1.0.473：Requires-Python 校验（兜底）——索引没给 data-requires-python 的镜像，
            # 仍在解压前读 wheel 内 METADATA 判一次（zip 读零成本，免 packaging 依赖）
            req = req_idx or _ocr_wheel_python_ok(wp)[1]
            if not _pyver_satisfies(req, cur_py):
                _pin = _OCR_WHEELS.get(pkg, "")
                with _OCR_DL_LOCK:
                    _OCR_DL.update(state="failed",
                                   error=f"{pkg}=={want.get(pkg)} 声明 Requires-Python {req}，"
                                         f"与当前 Python {cur_py[0]}.{cur_py[1]} 不兼容——请点「恢复推荐版本」安装已验证组合（该包推荐版本 {_pin}）")
                log_event(f"[ocr] {pkg} wheel Requires-Python 不兼容（{req} vs {cur_py[:2]}），中止安装")
                try:
                    os.remove(wp)      # v1.0.616：这条失败路径以前什么都不清
                except OSError:  # noqa: BLE001
                    pass
                return
            # v1.0.473：zip-slip 防护（此前 P2 遗留）——条目解析后必须仍在目标目录内
            import zipfile
            _dn = os.path.normpath(d)
            with zipfile.ZipFile(wp) as z:
                for x in z.namelist():
                    _t = os.path.normpath(os.path.join(_dn, x))
                    if not (_t == _dn or _t.startswith(_dn + os.sep)):
                        raise RuntimeError(f"wheel 含越界路径条目（zip-slip）：{x}")
                z.extractall(d)
            try:
                os.remove(wp)
            except OSError:  # noqa: BLE001
                pass
        missing = _ocr_needed_pkgs()[0]
        if missing:
            with _OCR_DL_LOCK:   # v1.0.361（P7）：收尾路径统一走锁
                _OCR_DL.update(state="failed", error="安装后仍缺组件：" + "、".join(missing))
            log_event(f"[ocr] 组件安装后仍缺：{missing}")
            return
        # 记录组件版本（供「检查更新」对比；build.py 的 sync_ocr_runtime 也写同一文件）
        vers = ocr_versions()
        for pkg, ver in want.items():
            if os.path.isdir(os.path.join(d, _OCR_PKG2DIR.get(pkg, pkg.lower().replace("-", "_")))):
                vers[pkg] = ver
        _ocr_write_versions(vers)
        global _OCR_RUNTIME_STATE
        _OCR_RUNTIME_STATE = None        # 强制 ensure_ocr_runtime 重新挂路径（新目录/新 DLL）
        ensure_ocr_runtime()
        ocr_invalidate_updates()         # 刚换掉的版本不能让「可升级」继续报旧号（v1.0.611 TTL 缓存）
        # v1.0.608：装完立刻跑真实识别自检——"六个目录都在"不等于"能识别"
        # （DLL 挂不上、模型缺文件都会让目录齐全但一跑就废），自检不过就按失败报，
        # 别让用户以为装好了、等到真正用 OCR 时才发现。
        sc = ocr_selfcheck(force=True)
        if not sc.get("ok"):
            with _OCR_DL_LOCK:
                _OCR_DL.update(state="failed",
                               error="组件已安装，但真实识别自检未通过：" + (sc.get("error") or "未知原因"))
            log_event("[ocr] 组件安装完成但自检未通过，按失败处理")
            return
        _scs = round((sc.get("ms") or 0) / 1000.0, 1)
        with _OCR_DL_LOCK:
            _OCR_DL.update(state="ready", progress=100,
                           msg=f"OCR 组件安装完成（自检通过，识别耗时 {_scs} 秒）", error="")
        log_event(f"[ocr] 组件在线安装完成并通过真实识别自检（{_scs}s）")
    except Exception as e:  # noqa: BLE001
        with _OCR_DL_LOCK:
            _OCR_DL.update(state="failed", error=str(e))
        import traceback
        log_event(f"[ocr] 组件在线安装失败（可在设置页重试）：{e}\n{traceback.format_exc().rstrip()}")


# ---------- OCR 组件版本与更新检查（v1.0.307：py 环境组件可持续更新） ----------
def _ocr_versions_path():
    return os.path.join(os.path.dirname(config_path()), "ocr", "versions.json")


def _ocr_versions_recorded():
    """versions.json 里记录的版本表（build.py sync 与安装器写的"意图版本"）。"""
    from core.files import read_json   # v1.0.654 A8：句柄样板收敛
    d = read_json(_ocr_versions_path())
    return d if isinstance(d, dict) else {}


def ocr_versions_on_disk():
    """扫 `dist/ocr/*.dist-info` 目录名得到**实际落盘版本**（dashed_pkg -> version）。

    为什么必须以它为准（v1.0.609，本机实测）：versions.json 写的是"当时打算装什么版本"，
    而在线更新只覆盖目录、不改记录 —— 实测 6/6 全不一致（记录 rapidocr 1.2.3 / onnxruntime
    1.29.0 / numpy 2.5.2，磁盘实际 1.4.4 / 1.30.0 / 2.5.3）。拿记录当"已装版本"去比最新版，
    「检查更新」就永远报"可升级"，还会把用户诱导去点实验性的「更新到最新版」。"""
    out = {}
    try:
        d = os.path.join(os.path.dirname(config_path()), "ocr")
        for name in os.listdir(d):
            if not name.endswith(".dist-info"):
                continue
            m = re.match(r"^(.+?)-(\d[^-]*)\.dist-info$", name)
            if not m:
                continue
            out[m.group(1).replace("_", "-").lower()] = m.group(2)
    except Exception:  # noqa: BLE001
        pass
    return out


def ocr_versions():
    """已装版本表：**磁盘 dist-info 优先**，没有元数据的包（cv2、pyclipper 常只有裸目录）
    回落到 versions.json 的记录值，再没有就留空（由调用方按"未知"处理，不猜）。"""
    out = _ocr_versions_recorded()
    out.update(ocr_versions_on_disk())
    return out


def ocr_disk_size_mb():
    """dist/ocr 实测占用（MB，含 .libs 伴生 DLL）；目录不存在返回 0。"""
    try:
        d = os.path.join(os.path.dirname(config_path()), "ocr")
        if not os.path.isdir(d):
            return 0
        tot = 0
        for dp, _dns, fns in os.walk(d):
            for f in fns:
                try:
                    tot += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return round(tot / 1048576.0)
    except Exception:  # noqa: BLE001
        return 0


def _ocr_write_versions(vers):
    """写组件版本记录（安装器与 build.py sync_ocr_runtime 双方共用同一文件）。"""
    try:
        d = os.path.dirname(_ocr_versions_path())
        os.makedirs(d, exist_ok=True)
        with open(_ocr_versions_path(), "w", encoding="utf-8") as f:
            json.dump(vers, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log_event(f"[ocr] 写入组件版本记录失败：{e}")


# ---------- OCR 真实识别自检（v1.0.608）----------
# 为什么必须有：此前「组件是否就绪」只看 dist/ocr 下六个目录在不在，两类问题判不出来——
# ① 目录齐但 .pyd/DLL 挂不上（导入即炸）；② 版本元数据声明不支持当前解释器却实际能跑
# （rapidocr 1.4.4 声明 <3.13，本机 3.13 实测可识别）。自检走**生产入口** `_ocr_image_file`，
# 判据是"能不能认出已知文本"，而不是"文件在不在"。
# 只用字母数字：PIL 默认位图字体没有中文字形（画出来是方块，识别必然对不上，白判失败）。
_SC_TEXT = "OCR717 SELFTEST 2026"
_OCR_SELFCHECK = {"state": "idle", "ok": None, "ms": None, "saw": "", "error": "", "at": "", "ratio": None}
_OCR_SC_LOCK = threading.Lock()
# 自检判据为什么是"相似度 ≥0.7"而不是"字字相等"：实测默认位图字体下 rapidocr 会把
# OCR717 读成 0XCR717（O↔0、多插一个 X）——引擎明明是好的，严格相等会误判"自检未通过"，
# 反而把可用组件报成坏。故：先尽量用真字体画清晰字，再按归一化（0/O、1/I、5/S、8/B、2/Z）
# 后的序列相似度判，既抓得住"完全读不出"的真故障，也不被字形混淆误伤。
_SC_CONFUSE = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "4": "A", "6": "G"}
_SC_MIN_RATIO = 0.70


def _sc_norm(s):
    return "".join(_SC_CONFUSE.get(ch, ch) for ch in "".join(str(s).split()).upper())


def _sc_font():
    """挑一个系统里能加载到的真字体（默认位图字体太小，rapidocr 容易读错字）。"""
    from PIL import ImageFont
    for name in ("arial.ttf", "segoeui.ttf", "calibri.ttf", "tahoma.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\\Windows"), "Fonts", name), 28)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def ocr_selfcheck_status():
    with _OCR_SC_LOCK:
        return dict(_OCR_SELFCHECK)


def ocr_selfcheck(force=False):
    """跑一次真实识别自检（阻塞约 1-5 秒），结果缓存供组件页/状态接口读。
    force=False 且已有结论时直接回缓存——开机自检与装完自检不会互相重跑。"""
    with _OCR_SC_LOCK:
        if not force and _OCR_SELFCHECK["state"] == "done":
            return dict(_OCR_SELFCHECK)
        _OCR_SELFCHECK.update(state="running")
    import time as _t
    info = {"state": "done", "ok": False, "ms": None, "saw": "", "error": "",
            "at": _t.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if not ensure_ocr_runtime():
            info["error"] = "OCR 旁挂目录不存在（组件未安装）"
        else:
            import tempfile
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (430, 64), "white")
            ImageDraw.Draw(img).text((14, 16), _SC_TEXT, fill="black", font=_sc_font())
            tmp = os.path.join(tempfile.gettempdir(), "WeComCompanion_ocr_selfcheck.png")
            img.save(tmp)
            _t0 = _t.time()
            got = _ocr_image_file(tmp) or ""
            info["ms"] = int((_t.time() - _t0) * 1000)
            try:
                os.remove(tmp)
            except OSError:  # noqa: BLE001
                pass
            info["saw"] = str(got).strip()[:80]
            if not "".join(str(got).split()):
                info["error"] = "识别结果为空（引擎没吐出任何文字）"
            else:
                import difflib
                ratio = difflib.SequenceMatcher(None, _sc_norm(_SC_TEXT), _sc_norm(got)).ratio()
                info["ratio"] = round(ratio, 2)
                if ratio >= _SC_MIN_RATIO:
                    info["ok"] = True
                else:
                    info["error"] = f"识别相似度过低（{ratio:.0%}，读到「{info['saw'][:40]}」）"
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    with _OCR_SC_LOCK:
        _OCR_SELFCHECK.update(**info)
    log_event("[ocr] 真实识别自检 %s（%sms）%s"
              % ("通过" if info["ok"] else "未通过", info["ms"] if info["ms"] is not None else "-",
                 "" if info["ok"] else "：" + info["error"]))
    return dict(info)


def ocr_selfcheck_async(delay=0.0, force=True, wait_idle=True):
    """后台线程跑自检（开机自动自检 / 装完自检用），绝不拖慢主流程。
    v1.0.614：开机那一次改为"等界面空闲再跑"——自检要加载 onnx 模型（3~6 秒 CPU），
    按固定延迟起跑时会把用户第一次进「运行组件」的请求拖慢（实测冷启动窗口最慢 14.48 秒）。"""
    def _go():
        try:
            if wait_idle:
                from core import idle
                idle.wait_until_idle(min_wait=delay, idle_for=3.0, deadline=180.0)
            elif delay:
                time.sleep(delay)
            ocr_selfcheck(force=force)
        except Exception as e:  # noqa: BLE001
            log_event(f"[ocr] 自动自检线程异常：{e}")
    threading.Thread(target=_go, daemon=True, name="ocr-selfcheck").start()
    return {"ok": True, "started": True}


def _verkey(v):
    """版本号排序键：取数字段补齐 4 位（1.2.10 > 1.2.9；rc/post 等非数字段忽略，够用）。"""
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in (nums + ["0", "0", "0", "0"])[:4])


def _ocr_latest_version(pkg, py="cp313"):
    """解析镜像 simple 索引，返回该包**当前解释器可用**的最新兼容 wheel 版本；失败返回 None。
    v1.0.608：带 `data-requires-python` 的候选先过一遍 `_pyver_satisfies` 再参与"取最新"——
    否则「更新到最新版」会把 rapidocr 1.4.4（声明 <3.13）当成最新，一路下载完才中止。"""
    best = None
    cur_py = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    want = pkg.lower().replace("-", "_")
    esc = re.escape(want)
    pats = (
        re.compile(rf"^{esc}-(\d[^/]*?)-py3-none-any\.whl$", re.I),
        re.compile(rf"^{esc}-(\d[^/]*?)-{py}-{py}-win_amd64\.whl$", re.I),
        re.compile(rf"^{esc}-(\d[^/]*?)-cp3\d+-abi3-win_amd64\.whl$", re.I),
    )
    last = []
    skipped = 0
    for mtpl in _OCR_MIRRORS:
        url = mtpl.format(pkg=pkg)
        try:
            import requests
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                last.append(f"{url} -> HTTP {r.status_code}")
                continue
            for h, req in _ocr_anchors(r.text):
                f = h.split("#", 1)[0].rsplit("/", 1)[-1]
                for pat in pats:
                    m = pat.match(f)
                    if not m:
                        continue
                    if not _pyver_satisfies(req, cur_py):
                        skipped += 1
                        break
                    if best is None or _verkey(m.group(1)) > _verkey(best):
                        best = m.group(1)
            if best is not None:
                if skipped:
                    log_event(f"[ocr] {pkg} 最新版解析：跳过 {skipped} 个 Requires-Python 不满足当前 "
                              f"{cur_py[0]}.{cur_py[1]} 的候选，取可用最新 {best}")
                return best
            last.append(f"{url} -> 无兼容 wheel" + (f"（已跳过 {skipped} 个不兼容版本）" if skipped else ""))
        except Exception as e:  # noqa: BLE001
            last.append(f"{url} -> {type(e).__name__}: {e}")
    log_event(f"[ocr] 查询 {pkg} 最新版失败：\n" + "\n".join(last))
    return None


def _ocr_scan_updates():
    """逐包对比已装版本与镜像最新版。每个包都要打一次 simple 索引（6 包 × 多镜像），
    本机实测 34.6 秒 —— 所以它只能进缓存，绝不能挂在界面路径上（见 ocr_check_updates）。"""
    installed = ocr_versions()
    d = os.path.join(os.path.dirname(config_path()), "ocr")
    pkgs = []
    for pkg, pin in _OCR_WHEELS.items():
        cur = installed.get(pkg)
        dir_ok = os.path.isdir(os.path.join(d, _OCR_PKG2DIR.get(pkg, pkg.lower().replace("-", "_"))))
        latest = _ocr_latest_version(pkg)
        if not dir_ok:
            state = "missing"
        elif cur and latest and _verkey(latest) > _verkey(cur):
            state = "outdated"
        elif cur:
            state = "latest"
        else:
            state = "unknown"          # 装过但无版本记录（1.0.306 旧装法）
        pkgs.append({"pkg": pkg, "pin": pin, "installed": cur, "latest": latest,
                     "state": state, "update_available": state in ("outdated", "unknown")})
    return {"packages": pkgs, "updates_available": sum(1 for p in pkgs if p["update_available"])}


_OCR_UPD = {"ts": 0.0, "data": None}
_OCR_UPD_TTL = 900.0


def ocr_invalidate_updates():
    """装完/更新完必须作废缓存，否则「可升级」会继续报刚被换掉的旧版本号。"""
    _OCR_UPD["ts"] = 0.0
    _OCR_UPD["data"] = None


def ocr_check_updates(force=False, cache_only=False):
    """「已装 vs 镜像最新」的对比结果，带 TTL 缓存（v1.0.611）。

    为什么要缓存：全量扫描实测 34.6 秒，而它原先挂在 `GET /api/components/status` 上 ——
    进「运行组件」要白等半分钟才出列表（正式包复测时把台子 15 秒等待直接判红）。
      · cache_only=True → 只读缓存（界面路径用；没有就返回 pending，不联网）
      · force=True      → 无视 TTL 立刻重算（用户点「检查更新」、装/更新收尾时）
    """
    if not force and (time.time() - _OCR_UPD["ts"]) < _OCR_UPD_TTL and _OCR_UPD["data"] is not None:
        return _OCR_UPD["data"]
    if cache_only:
        if _OCR_UPD["data"] is not None:
            return _OCR_UPD["data"]
        return {"packages": [], "updates_available": 0, "pending": True}
    res = _ocr_scan_updates()
    res["checked_at"] = time.strftime("%H:%M:%S")
    _OCR_UPD["data"] = res
    _OCR_UPD["ts"] = time.time()
    return res


def ocr_update_check_async(delay=0.0, wait_idle=True):
    """开机后台预热更新缓存，不阻塞、不联网前界面就得等。未装则跳过。
    v1.0.614：同样等界面空闲——35 秒的镜像往返和界面请求并行时，会把体检接口拖到十几秒。"""
    def _go():
        try:
            if wait_idle:
                from core import idle
                idle.wait_until_idle(min_wait=delay, idle_for=3.0, deadline=300.0)
            elif delay:
                time.sleep(delay)
            if not ocr_install_status().get("installed"):
                return
            r = ocr_check_updates(force=True)
            log_event("[ocr] 更新缓存预热完成：%d 项可升级（%s）"
                      % (r.get("updates_available") or 0, r.get("checked_at")))
        except Exception as e:  # noqa: BLE001
            log_event(f"[ocr] 更新缓存预热失败：{type(e).__name__}: {e}")
    threading.Thread(target=_go, daemon=True, name="ocr-updates").start()
    return {"ok": True, "started": True}

def ocr_ready_or_install():
    """OCR 调用点统一入口：就绪返回 True；未就绪则触发后台在线安装并返回 False（本次调用降级）。"""
    if ensure_ocr_runtime():
        return True
    try:
        install_ocr_async()
    except Exception as e:  # noqa: BLE001
        log_event(f"[ocr] 触发 OCR 组件后台安装失败：{e}")
    return False


def _ocr_image_arr(arr):
    """对图片 ndarray 执行 OCR（rapidocr-onnxruntime，中英），返回文本。引擎懒加载单例。
    v1.0.361 审查修复（P7）：双检锁——原 check-then-set 无锁，两线程首次并发会各跑一次
    重量级 onnx 初始化（一份被丢弃）。"""
    global _OCR_ENGINE
    try:
        if _OCR_ENGINE is None:
            # v1.0.307：未就绪时触发后台在线安装（碰到 OCR 事务才下载，下载约 81MB / 占用约 226MB），
            # 本次调用明确降级并告知重试——不再无声拿空文本。
            if not ocr_ready_or_install():
                log_event(f"[read-file] OCR 组件未安装，已开始后台联网下载（{ocr_size_hint()}，一次性），"
                          "完成后自动可用；本次跳过 OCR，请稍后重试。")
                return ""
            with _OCR_ENGINE_LOCK:
                if _OCR_ENGINE is None:
                    # 幂等挂 dist/ocr 到 sys.path（v1.0.611 补）：ocr_ready_or_install() 在
                    # 「目录已存在」时只 return True、不插路径，而旁挂的 rapidocr 只有插了才 import 得到。
                    # 原先仅靠开机/安装流程顺带插过一次，那条顺带路径一断这里就静默 ImportError → 读到空文本。
                    ensure_ocr_runtime()
                    from rapidocr_onnxruntime import RapidOCR
                    _OCR_ENGINE = RapidOCR()
        result, _ = _OCR_ENGINE(arr)
        if not result:
            return ""
        return "\n".join(line[1] for line in result)
    except Exception as e:  # noqa: BLE001
        log_event(f"[read-file] OCR 失败（若提示 ModuleNotFoundError 请检查 dist/ocr 目录是否完整）：{e}")
        return ""


def _ocr_image_file(path):
    """对单张图片文件执行 OCR。"""
    import numpy as np
    try:
        from PIL import Image
        return _ocr_image_arr(np.asarray(Image.open(path).convert("RGB")))
    except Exception as e:  # noqa: BLE001
        log_event(f"[read-file] 图片 OCR 失败：{e}")
        return ""
