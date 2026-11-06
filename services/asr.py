# -*- coding: utf-8 -*-
"""services.asr — 本地语音转写（v1.0.523-5 / v1.0.536 组件化）
=================================================================
语音输入的后端引擎：上传的音频（webm/opus，浏览器 MediaRecorder 产出）转文字。

v1.0.536 组件化（用户需求：exe 从 98MB 瘦回 ~40MB，语音按需安装）：
- 打包**不含** faster-whisper 依赖链（ctranslate2.dll 单文件 57MB）——spec excludes；
- 设置页「语音转写组件」一键安装：从 PyPI 下载 pinned 版本 wheel（win_amd64）
  解压到 产物库/组件/asr/，运行时 sys.path + add_dll_directory 动态加载；
- asr_available() 三态：'installed' / 'missing'（可一键安装）/ False（平台不支持）。

依赖组合（venv 实测 pin，内嵌 Python 3.13 win_amd64）：
  faster-whisper==1.2.1  ctranslate2==4.8.2  tokenizers==0.23.2
  huggingface_hub==2.2.0  av==16.1.0  numpy（复用 dist/ocr/ 旁挂，缺失则组件目录自装）
"""
import json
import os
import sys
import threading
import time
import zipfile

from core_utils import log_event, workspace_root

# v1.0.595 修「装好组件仍然转写失败」：模型下载被 401 拒。
# 日志实锤：File reconstruction error: CAS Client error: HTTP 401 Unauthorized,
#   domain https://cas-server.xethub.hf.co/v2/reconstructions/…
# 这是依赖漂移：v1.0.545 改用 pip bootstrap 自动解析依赖树后，装到的是
# huggingface_hub 2.2.0——它对走 Xet 存储的仓库默认用 Xet CAS 通道取文件，
# 而 CAS 域名是仓库元数据里带的【官方地址】，不走我们设的 HF_ENDPOINT 镜像，
# 匿名用户一律 401（旧的"手挑 wheel"时代 hub 版本老、没有 Xet，所以从来没碰到）。
# 关掉 Xet，退回普通 HTTP 下载，才会真正经过 hf-mirror。
# 必须在 huggingface_hub 被 import 之前设——constants.py 是 import 时读环境，
# 设晚了整程无效；这里放在 services.asr 模块顶部，而 hub 只可能在
# _ensure_component_path() 注入 sys.path 之后才可能被 import，顺序天然安全。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

_state = {"_model": None, "_size": None, "_failed": False, "_failed_at": 0.0, "_path_ready": None,
          "_failed_msg": ""}
_lock = threading.Lock()
# v1.0.595：模型加载失败从「整程拉黑」改成「冷却后允许重试」。
# 原实现 _failed=True 后永不重试，而首次失败最常见的原因是【网络/模型下载中断】
# （small 权重 ~480MB，走镜像可能要几分钟，中途断一下就把语音输入判了死刑，
# 用户只能重启软件）。现在 60s 后允许再试一次，成功即清零。
_ASR_RETRY_COOLDOWN = 60.0

# 组件安装（v1.0.545 pip bootstrap 方案）：手挑 wheel 的依赖树不可枚举
# （faster-whisper → tqdm；huggingface_hub → click/filelock/fsspec/hf-xet/httpx2/
#  packaging/pyyaml/typing-extensions…逐个补会一直踩坑）——改为：下载 pip 本体
# （纯 Python ~2MB）解压后用 `pip install --target` 让它**自动解析整棵依赖树**，
# index 走清华镜像（pypi 直连在用户网络慢 10 倍）。venv 实测可用组合 pin 死：
_PKGS = ["faster-whisper==1.2.1", "av==16.1.0"]
_PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
_HF_ENDPOINT = "https://hf-mirror.com"   # 模型下载镜像（GitHub/HF 直连不稳的环境）


def component_dir():
    d = os.path.join(workspace_root(), "组件", "asr")
    os.makedirs(d, exist_ok=True)
    return d


def _install_marker():
    return os.path.join(component_dir(), "_installed.json")


def component_installed():
    """组件是否已安装（标记文件存在）。

    ⚠️ 只看标记文件会放过"装坏了的组件"——判可用性请用下面的 component_complete()。"""
    return os.path.isfile(_install_marker())


def installed_meta():
    """安装标记里的元数据（{ts, pkgs}）；读不到返回 {}。给组件体检显示版本用。"""
    try:
        with open(_install_marker(), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# faster-whisper 自己 import 链上的顶层包（v1.0.545 pip bootstrap 必然装齐的一组）。
# 只列"缺了就跑不起来"的，不列版本相关的可能项（httpx/requests/hf-xet 等），
# 否则换个依赖版本就会误判成残缺、触发无休止重装。
_REQUIRED_TOPS = ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
                  "numpy", "onnxruntime", "av", "tqdm", "click", "filelock",
                  "fsspec", "packaging", "yaml")


def component_complete():
    """组件是否【真的能跑】——文件级判据，绝不 import（见 asr_available 的 v1.0.539 教训）。

    修的是一个真实故障：v1.0.544 及更早用「手挑 wheel」装组件（标记里是 `wheels` 键），
    依赖树是残缺的（实测这份安装里没有 tqdm / click / filelock / fsspec / packaging），
    模型加载直接 `No module named 'tqdm'`，界面上只看到一句「转写失败」。
    v1.0.545 改成 pip bootstrap 自动解析整棵依赖树，但 component_installed() 只判
    标记文件在不在 → 那份残缺实例被【永久认证为已安装】，新的正确安装路径再也跑不起来，
    用户重装也没用。这里加两条判据：
      ① 标记必须是新格式（含 `pkgs` 键）——旧 `wheels` 标记一律视为待重装；
      ② 顶层依赖目录必须实存（缺一个就算残缺）。
    不通过时 asr_available() 返回 "missing"，前端的一键安装会重新跑 pip bootstrap。"""
    if not component_installed():
        return False
    try:
        with open(_install_marker(), encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(meta, dict) or "pkgs" not in meta:
        return False
    d = component_dir()
    for m in _REQUIRED_TOPS:
        if os.path.isdir(os.path.join(d, m)) or os.path.isfile(os.path.join(d, m + ".py")):
            continue
        log_event(f"[asr] 组件不完整（缺 {m}）→ 视为未安装，需重新安装")
        return False
    return True


def _ensure_component_path():
    """组件目录注入 sys.path + DLL 搜索路径（进程内一次）。"""
    if _state["_path_ready"]:
        return True
    if not component_complete():
        return False
    d = component_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        os.add_dll_directory(d)
        ctd = os.path.join(d, "ctranslate2")
        if os.path.isdir(ctd):
            os.add_dll_directory(ctd)
    except Exception:  # noqa: BLE001
        pass
    # numpy：优先复用 OCR 旁挂（frozen 下 dist/ocr/），缺失时组件目录自装版兜底
    try:
        import numpy  # noqa: F401
    except Exception:  # noqa: BLE001
        ocr_dir = os.path.join(os.path.dirname(sys.executable), "ocr") \
            if getattr(sys, "frozen", False) else None
        if ocr_dir and os.path.isdir(ocr_dir) and ocr_dir not in sys.path:
            sys.path.append(ocr_dir)
        # dist/ocr 是**旁挂目录**（v1.0.306 起 numpy/cv2/onnxruntime 不在 exe 里），只有
        # ensure_ocr_runtime() 会把那条路径插进来。这里原先只手拼 ocr_dir 到 sys.path 末尾，
        # 而 frozen 下 numpy 的伴生 .libs 还要 os.add_dll_directory 才加载得动——实测冷启动
        # 先按语音时，能量探测就在这一手拼路径上抛 No module named 'numpy'，日志留痕、界面
        # 只给一句「转写失败」。改为直接复用 OCR 那侧已验证过的挂载函数（幂等，进程内一次）。
        try:
            from core.ocr import ensure_ocr_runtime
            ensure_ocr_runtime()
            import numpy  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log_event(f"[asr] numpy 仍不可导入（转写会照旧尝试，能量探测可能失效）：{type(e).__name__}: {e}")
    _state["_path_ready"] = True
    return True


def asr_available():
    """ASR 引擎是否可用（v1.0.539 收紧）：
    - **frozen（打包版）纯文件判定**——组件装好且依赖齐=installed，否则 missing。
      绝不 import：打包版 excludes 掉了 faster-whisper，任何 import 都会命中
      sys.path 里可能残留的半解压组件目录（v1.0.532-538 三次挂死的共同根因
      ——旧进程 sys.path 污染存活多日）。
    - 开发模式（非 frozen）保留 venv 探测（site-packages 完整，import 快速安全）。"""
    if os.name != "nt":
        return False
    if component_complete():
        return "installed"
    if not getattr(sys, "frozen", False):
        try:
            import faster_whisper  # noqa: F401
            return "installed"
        except ImportError:
            return "missing"
        except Exception:  # noqa: BLE001
            return "missing"
    return "missing"


# ---------------- 一键安装（pypi wheel 下载解压） ----------------
# v1.0.539 死锁修复：_INSTALL_LOCK 改 **RLock（可重入）**——原 threading.Lock 在
# install_component 的 with 块内调用 _set_install（同线程重入）→ 自死锁：请求线程
# 永久卡死、_job 从未启动（staging 目录空）、后续 status 全部拿不到锁挂死（用户实测
# 一键安装卡「启动安装…」+ status 超时；此前两轮误判为"下载慢"）。
_INSTALL_STATE = {"running": False, "phase": "", "pct": 0, "ok": None, "error": ""}
_INSTALL_LOCK = threading.RLock()


def install_progress():
    with _INSTALL_LOCK:
        return dict(_INSTALL_STATE)


def _set_install(**kw):
    with _INSTALL_LOCK:
        _INSTALL_STATE.update(kw)



def _download(url, dest, label):
    """流式下载（3 次重试），进度写入 _INSTALL_STATE。
    v1.0.536：**国内镜像加速**——files.pythonhosted 国际链路下载 60MB 可达 20 分钟+
    （实测），清华 PyPI 镜像路径结构完全兼容，优先走镜像、失败回落官方。"""
    from core_utils import _lazy_requests
    mirror_url = url.replace("https://files.pythonhosted.org",
                             "https://pypi.tuna.tsinghua.edu.cn")
    for attempt, u in enumerate([mirror_url, url, mirror_url, url]):   # 镜像↔官方交替重试
        try:
            r = _lazy_requests.get(u, stream=True, timeout=(5, 60))
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        _set_install(phase=f"下载 {label}", pct=int(done * 100 / total))
            return dest
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise RuntimeError(f"{label} 下载失败（4 次尝试后）：{e}") from e
            time.sleep(2)


def install_component():
    """一键安装语音组件：pypi 查询 pinned 版本 wheel → 下载 → 解压到 组件/asr/。
    后台线程执行（进度走 install_progress）；完成写 _installed.json 标记。"""
    with _INSTALL_LOCK:
        if _INSTALL_STATE["running"]:
            return {"ok": False, "error": "安装已在进行中"}
        _set_install(running=True, phase="准备", pct=0, ok=None, error="")
    try:
        def _job():
            try:
                from core_utils import _lazy_requests
                req = _lazy_requests
                # v1.0.538 原子化：先解压到临时目录，全部成功后一次性 rename——
                # 半解压状态绝不暴露（半初始化的 ctranslate2.dll 会把 import 挂死）
                final = component_dir()
                stage = final + "_staging"
                if os.path.isdir(stage):
                    import shutil
                    shutil.rmtree(stage, ignore_errors=True)
                os.makedirs(stage, exist_ok=True)
                dest = stage
                # ---- 第 1 步：bootstrap pip（纯 Python wheel ~2MB，解压即用）----
                _set_install(phase="准备 pip 安装器", pct=2)
                meta = req.get("https://pypi.org/pypi/pip/json", timeout=(5, 30)).json()
                _urls = [u for u in meta.get("urls") or [] if u["filename"].endswith("py3-none-any.whl")]
                if not _urls:
                    raise RuntimeError("PyPI 上没有 pip 的通用 wheel")
                pip_whl = os.path.join(dest, "_pip_boot.whl")
                _download(_urls[0]["url"], pip_whl, "pip 安装器")
                _set_install(phase="解压 pip 安装器", pct=5)
                pip_dir = os.path.join(dest, "_pip")
                with zipfile.ZipFile(pip_whl) as zf:
                    # v1.0.616（审查 P2）：三条解压路径里只有这一条没做 zip-slip 校验
                    # （core/portable.py、core/ocr.py 都有 _safe_extract）——镜像被换或
                    # 中间人塞条目时，wheel 里的 ../ 路径能写到暂存目录之外。
                    from core.portable import _safe_extract
                    _safe_extract(zf, pip_dir)
                os.remove(pip_whl)
                # ---- 第 2 步：pip install --target（自动解析整棵依赖树，清华镜像）----
                _set_install(phase="pip 安装组件及全部依赖（约 5-10 分钟）", pct=8)
                sys.path.insert(0, pip_dir)
                try:
                    from pip._internal.cli.main import main as _pip_main
                    # v1.0.547：frozen 无控制台环境 sys.stdout/stderr/.__stdout__/.__stderr__
                    # 全为 None——pip 内部直接访问 sys.__stdout__（redirect_stdout 不覆盖它）。
                    # 四路全替换为内存缓冲，pip 跑完恢复原值。
                    import io as _io
                    _buf = _io.StringIO()
                    _saved = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
                    sys.stdout = _buf; sys.stderr = _buf
                    sys.__stdout__ = _buf; sys.__stderr__ = _buf
                    # v1.0.551：pip dictConfig 会 flush **所有已注册 handler**——frozen
                    # 环境里 colorama 包装的 handler（stream=None）一 flush 即崩
                    # （'NoneType' has no attribute 'flush'，用户完整栈实锤）。
                    # pip 运行前临时摘除全部 root handlers，跑完恢复。
                    import logging as _lg
                    _root = _lg.getLogger()
                    _saved_handlers = list(_root.handlers)
                    for _h in _saved_handlers:
                        _root.removeHandler(_h)
                    # v1.0.551 补：logging.shutdown/_clearExistingHandlers flush 的是
                    # **全局 _handlerList**（弱引用表，进程里曾创建过的每个 handler 都在，
                    # 与挂载在哪个 logger 无关）——root 摘除挡不住 colorama 的 None 流
                    # handler。清空该表（一次性；FileHandler 每条记录自 flush，重启进程
                    # 自然重建，无持久副作用）。
                    _lg._handlerList.clear()
                    try:
                        _rc = _pip_main(["install", "--no-warn-script-location",
                                         "--no-input", "--target", dest,
                                         "--index-url", _PYPI_INDEX] + _PKGS)
                        if _rc != 0:
                            raise RuntimeError(f"pip install 退出码 {_rc}：{_buf.getvalue()[-150:]}")
                    finally:
                        for _h in _saved_handlers:
                            _root.addHandler(_h)
                        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = _saved
                finally:
                    try:
                        sys.path.remove(pip_dir)
                    except ValueError:  # noqa: BLE001
                        pass
                    # v1.0.613：pip 引导目录是解压进 dest（=staging）的，装完不删就会被
                    # 原子提交一起搬进正式组件目录里躺着（本机实测 `组件/asr/_pip` 5.4MB，
                    # 运行期永远用不到）。
                    try:
                        import shutil as _sh0
                        _sh0.rmtree(pip_dir, ignore_errors=True)
                    except Exception:  # noqa: BLE001
                        pass
                _set_install(phase="验证组件可加载", pct=90)
                os.environ.setdefault("HF_ENDPOINT", _HF_ENDPOINT)
                # 原子提交：staging → 正式目录（旧目录若存在先清）
                import shutil as _sh
                if os.path.isdir(final):
                    _sh.rmtree(final, ignore_errors=True)
                os.rename(stage, final)
                json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "pkgs": list(_PKGS)},
                          open(_install_marker(), "w", encoding="utf-8"), ensure_ascii=False)
                _state["_path_ready"] = None   # 触发重新注入
                _state["_model"] = None        # 模型缓存重建
                # v1.0.595：装完组件必须一并清掉失败标志——原实现只重置 path/model，
                # 之前那次「加载失败」的拉黑还在，用户装好组件后仍报转写失败、非重启不可。
                _state["_failed"] = False
                _state["_failed_at"] = 0.0
                _set_install(running=False, phase="完成", pct=100, ok=True)
                from core.workspace_io import invalidate_size_cache
                invalidate_size_cache()   # 组件目录一次多出上百 MB，别让产物库页继续报旧体积
                log_event("[asr] 语音组件安装完成（产物库/组件/asr/）")
            except Exception as e:  # noqa: BLE001
                _set_install(running=False, phase="失败", ok=False, error=str(e)[:200])
                import traceback as _tb
                log_event("[asr] 组件安装失败：" + str(e) + " | " + _tb.format_exc())   # v1.0.550：完整栈定位
                # v1.0.613：失败也要清 staging。原先只在"下次安装开头"才 rmtree，于是本机
                # 10-13 那次 `WinError 183`（目录重名）之后，258.7MB 的半成品一直躺在
                # `组件/asr_staging` 里——用户看到的是白占一个半 G 的垃圾，还得自己发现。
                try:
                    import shutil as _s2
                    _s2.rmtree(component_dir() + "_staging", ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_job, name="asr-install", daemon=True).start()
        return {"ok": True, "hint": "组件安装已开始（约 75MB，从 PyPI 下载，仅一次）"}
    except Exception as e:  # noqa: BLE001
        _set_install(running=False, ok=False, error=str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}


# ---------------- 引擎加载与转写 ----------------
def _model_size():
    try:
        from core.config_store import load_config
        a = load_config().get("asr")
        if isinstance(a, dict) and a.get("model_size"):
            return str(a["model_size"])
    except Exception:  # noqa: BLE001
        pass
    return "small"


def _get_model():
    """懒加载模型（进程级单例；换 model_size 时重载）。失败返回 None（不反复重试）。
    v1.0.611：失败原因记进 _failed_msg 并随错误一起透出——原先只留一句「引擎不可用」，
    用户看到的是「转写失败」，而真相可能是模型下载 401 / 依赖残缺 / 冷却期内还没重试。"""
    with _lock:
        if _state["_failed"] and (time.time() - (_state.get("_failed_at") or 0.0)) < _ASR_RETRY_COOLDOWN:
            return None
        _state["_failed"] = False   # 冷却期已过：本次真去试一次，成败再记账（v1.0.595）
        size = _model_size()
        if _state["_model"] is not None and _state["_size"] == size:
            return _state["_model"]
        try:
            if not _ensure_component_path():
                _state["_failed"] = True
                _state["_failed_at"] = time.time()
                _state["_failed_msg"] = "语音组件未安装或依赖残缺（组件体检里可一键重装）"
                return None
            from faster_whisper import WhisperModel
            os.environ.setdefault("HF_ENDPOINT", _HF_ENDPOINT)
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # 见文件头：Xet CAS 不走镜像，匿名 401
            t0 = time.time()
            model = WhisperModel(size, device="cpu", compute_type="int8")
            _state["_model"] = model
            _state["_size"] = size
            _state["_failed"] = False
            _state["_failed_at"] = 0.0
            _state["_failed_msg"] = ""
            log_event(f"[asr] whisper 模型加载完成（{size}/int8/cpu，{time.time()-t0:.1f}s）")
            return model
        except Exception as e:  # noqa: BLE001
            _state["_failed"] = True
            _state["_failed_at"] = time.time()
            _state["_failed_msg"] = (f"{type(e).__name__}: {e}")[:180]
            log_event(f"[asr] 模型加载失败（{_ASR_RETRY_COOLDOWN:.0f}s 后会自动重试）：{e}")
            return None


def _audio_level(path):
    """解码并量一下音频能量 → (rms, peak, 秒)。取不到时返回 (None,None,None)，不影响主流程。

    存在的意义：把「麦克风根本没收到声音」和「收到了但模型/VAD 没认出来」分开。
    实测用户录 4.9s 中文，日志是「转写完成：0 字（0.0s，音频 4.9s）」——0.0 秒跑完
    说明模型根本没算：Silero VAD 默认 threshold=0.5，整段被判非语音就直接空手而归。
    到底是数字静音还是轻声，靠 peak 一眼可辨。
    """
    try:
        import numpy as np
        from faster_whisper import decode_audio
        a = decode_audio(path, sampling_rate=16000)
        if a is None or len(a) == 0:
            return 0.0, 0.0, 0.0
        f = a.astype("float64")
        return float(np.sqrt(np.mean(f * f))), float(np.max(np.abs(f))), len(a) / 16000.0
    except Exception as e:  # noqa: BLE001
        log_event(f"[asr] 音频能量探测失败（忽略，按原流程转写）：{e}")
        return None, None, None


# 峰值低于此值视为"数字级静音"（约 -48dBFS）：正常说话的 peak 在 0.05~0.6。
_SILENT_PEAK = 0.004


def last_error():
    """最近一次模型加载失败的原因（成功加载后清空）。给组件体检与状态接口透出用。"""
    return _state.get("_failed_msg") or ""


def transcribe_file(path, language="zh"):
    """转写音频文件，返回 (text, info_dict)。失败返回 (None, error)。"""
    model = _get_model()
    if model is None:
        _why = last_error() or ("组件已安装，但 %d 秒前刚失败过一次、正在冷却（%d 秒后自动重试）"
                                % (int(time.time() - (_state.get("_failed_at") or 0.0)),
                                   int(_ASR_RETRY_COOLDOWN)))
        return None, "转写引擎不可用：" + _why
    # 能量探测必须排在 _get_model() 之后：numpy 与 faster_whisper 都只在
    # _ensure_component_path() 把组件目录/OCR 旁挂目录注入 sys.path 之后才可导入。
    # （实测把它提到前面去，frozen 下立刻 No module named 'numpy'，探测永久失效。）
    rms, peak, dur = _audio_level(path)
    if peak is not None and peak < _SILENT_PEAK:
        log_event(f"[asr] 录到的音频近乎静音（peak={peak:.5f} rms={rms:.5f} {dur:.1f}s）")
        return None, ("麦克风没有收到声音（录音近乎静音）——检查系统默认输入设备、"
                      "Windows 隐私设置里的麦克风授权，或离麦克风近一点再说一遍")
    _lv = "peak=%s rms=%s" % ("%.4f" % peak if peak is not None else "?",
                              "%.4f" % rms if rms is not None else "?")   # 探测失败也不能把整条链带崩
    try:
        t0 = time.time()
        lang = language if language != "auto" else None
        segments, info = model.transcribe(path, language=lang, beam_size=3, vad_filter=True)
        text = "".join(seg.text for seg in segments).strip()
        if not text and (info.duration or 0) >= 1.0:
            # v1.0.596：VAD 判空时关阀重试一次。轻声/短促中文在 threshold=0.5 下
            # 会被整段丢掉，表现为「0 字 + 0.0 秒」；关掉 VAD 让模型自己判，实测能救回来。
            log_event(f"[asr] VAD 判为无语音（{info.duration:.1f}s，{_lv}）→ 关 VAD 重试")
            segments, info = model.transcribe(path, language=lang, beam_size=3, vad_filter=False)
            text = "".join(seg.text for seg in segments).strip()
        log_event(f"[asr] 转写完成：{len(text)} 字（{time.time()-t0:.1f}s，"
                  f"音频 {info.duration:.1f}s，{_lv}）")
        return text, {"duration": round(float(info.duration), 1), "language": info.language}
    except Exception as e:  # noqa: BLE001
        return None, f"转写失败：{str(e)[:150]}"


def transcribe_bytes(data, ext="webm"):
    """便捷入口：字节内容落临时文件再转写。"""
    if not data:
        return None, "空音频"
    if len(data) > 20 * 1048576:
        return None, "音频过大（>20MB），请说短一点"
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=f".{ext}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return transcribe_file(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
