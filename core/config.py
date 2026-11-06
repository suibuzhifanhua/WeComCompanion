# -*- coding: utf-8 -*-
"""core.config — 配置路径/脱敏/参数拾取/JSON 抽取与 Schema 校验/原子写（自 core_utils 拆分 v1.0.402）

v1.0.646：原 core/json_utils.py（94 行）并入本文件。二者本来就耦合——json_utils 的
`_ensure_jsonschema` 是从 core/config.py 迁出去的，config.py 又 re-export 回来；拆成两个
文件只是让这份来回依赖多绕一层。合并后 `core_utils` 的转发表面逐字不变。
"""
# -*- coding: utf-8 -*-
import os
import re
import json
from core.secrets import _is_sensitive_key  # noqa: v1.0.647 归位（原顶层 security.py）
# _fallback_dir：config_path() 的不可写回退目录，v1.0.402 拆分时漏导（回退分支必抛 NameError）
from core.paths import _app_dir, _fallback_dir  # noqa: F401

# ---------- jsonschema 可用性探测（v1.0.65 迁移；v1.0.324 惰性化：import 链 ~236ms） ----------
# 原实现在 core/config.py，后随 v1.0.402 拆分进 core/json_utils.py；v1.0.646 并回本文件。
# 惰性化原因：jsonschema 及其依赖的 import 链约 236ms，放在模块级会拖慢每次启动。
_jsonschema = None
_HAVE_JSONSCHEMA = None  # None=未探测


def _ensure_jsonschema():
    """首次使用时导入 jsonschema。可用返回模块并置 _HAVE_JSONSCHEMA=True；
    缺依赖置 False（禁用 schema 校验），与历史语义一致。"""
    global _jsonschema, _HAVE_JSONSCHEMA
    if _HAVE_JSONSCHEMA is None:
        try:
            import jsonschema as _js
            _jsonschema = _js
            _HAVE_JSONSCHEMA = True
        except Exception:  # noqa: BLE001
            _jsonschema = None
            _HAVE_JSONSCHEMA = False
    return _jsonschema


def config_path():
    """优先使用应用所在文件夹；不可写时回退到 %LOCALAPPDATA%\\WeComCompanion。"""
    app_dir = _app_dir()
    main = os.path.join(app_dir, "config.json")
    if os.path.exists(main):
        return main
    try:
        test = os.path.join(app_dir, ".wc_write_test")
        with open(test, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(test)
        return main
    except Exception:  # noqa: BLE001
        pass  # silent-ok: 写权限探测失败即回退 fallback 目录，是预期分支不是故障
    return os.path.join(_fallback_dir(), "config.json")


def mask_key(k):
    if not k:
        return ""
    return k[:4] + "****" + k[-4:] if len(k) > 8 else "****"


def _mask_args(args):
    """对工具参数做脱敏（敏感 key 值打码）并截断到 200 字符，用于日志。"""
    def walk(o):
        if isinstance(o, dict):
            return {k: ("******" if _is_sensitive_key(k) else walk(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return json.dumps(walk(args or {}), ensure_ascii=False)[:200]
def _pick_int(args, key):
    """取整数参数，不是整数则当 None（与旧 _pick 行为一致）。"""
    v = args.get(key)
    return v if isinstance(v, int) else None


def _pick_flex(args, key="pick", default=None):
    """v1.0.413（pick 序号漂移修复）：工具 pick 参数弹性解析——
    int / 数字字符串 → int（索引寻址）；其他非空字符串 → 原样返回
    （标题/URL 模糊寻址，由 wecom_bridge.pick_window 解析）。
    背景：窗口开关后 CDP target 序号漂移，模型手里的旧 pick 要么越界
    要么指向错窗口——标题寻址（如"待办事宜"）跨窗口开关稳定。

    v1.0.592 修复（P0）：default 由 0 改为 None。原实现 `args.get(key, 0)` 让
    【省略 pick】等于【显式 pick=0】，而 `windows._page_ws` 的「跟随窗口 → 正文最长页」
    自动选择分支只在 `pick is None` 时可达（`if pick is not None: … return pages[_pi]`），
    于是那条分支对所有 agent 工具永久不可达——v1.0.192 起的点击跟随、registry 里
    写明的「不传则自动选正文最长的窗口」「省略则自动选当前窗口」全部落空，多窗口时
    一律打在 index 0（通常是企微主聊天窗）。
    同时归一模型显式传的 null：`str(None)`="None" 会被当成标题子串去模糊匹配，
    匹不到 → 返回「没有可用的企微窗口」这种误导性报错；现在 null/空串/"None" 一律视为省略。"""
    v = args.get(key, default)
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    if not s or s == "None":
        return default
    return s


# ---------- 以下为 v1.0.646 自 core/json_utils.py 并入（函数体逐字未改） ----------
# ---------- 原子写（v1.0.510 优化 P2-13：原 16 处手写 tmp+os.replace 样板收敛） ----------
def atomic_write_json(path, obj, indent=2, ensure_ascii=False):
    """原子写 JSON 文件：先写 <path>.tmp 再 os.replace，崩溃不会留半截文件。
    调用方如需并发安全请自带锁（本函数只保证单次写不产生损坏文件）。"""
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    """原子写文本文件（UTF-8）。语义同 atomic_write_json。"""
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------- 结构化抽取 ----------
def extract_json(text):
    if not text:
        return None
    s = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass  # 预期：JSON 提取失败返回 None，由调用方兜底
    opens = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if opens:
        start = min(opens)
        end = max(s.rfind("}"), s.rfind("]"))
        if end > start:
            try:
                return json.loads(s[start:end + 1])
            except Exception:  # noqa: BLE001
                pass  # 预期：截取段非合法 JSON 时返回 None
    return None


def validate_schema(data, schema):
    if _ensure_jsonschema() is None:
        return "jsonschema 未安装，跳过校验"
    try:
        _jsonschema.validate(instance=data, schema=schema)
        return None
    except Exception as e:  # noqa: BLE001
        return f"Schema 校验不通过：{e}"


def _valid_json_schema(schema):
    """校验 output_schema 是否为合法 JSON Schema；环境无 jsonschema 库时退化为基本类型检查。"""
    if not isinstance(schema, dict):
        return False
    if _ensure_jsonschema():
        try:
            _jsonschema.Draft7Validator.check_schema(schema)
            return True
        except Exception:  # noqa: BLE001
            return False
    return schema.get("type") in ("object", "array", "string", "number", "integer", "boolean", "null")
