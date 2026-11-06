# -*- coding: utf-8 -*-
"""conversations.py — 会话存储（v1.0.327）
产物库/会话/<id>.json：{id,title,created,updated,archived,messages:[{role,content,ts}]}
v1.0.645 起 agent 消息可带 {blocks:[…]}：重开会话时复原当时的版式（步骤卡/推理框/文件行）。
API 见 routes/conversations.py。归档时触发会话级经验总结沉淀到记忆。
"""
import datetime
import io
import json
import os
import threading
import uuid

from core_utils import log_event, workspace_root
from core.config import atomic_write_json  # v1.0.510（P2-13）

_CONV_LOCK = threading.RLock()   # v1.0.361：可重入（append/mutate 外层持锁内调 load）
_MAX_KEEP = 500          # 会话文件数量上限（超出淘汰最旧归档）
_HISTORY_WINDOW = 20     # 拼 LLM 上下文时取最近 N 条消息
# v1.0.628：单条消息进上下文的字符上限。会话**存档**保持全文（前端重开会话要看全文），
# 但拼给模型时每则只取开头若干字。此前 agent 轮因落库 bug 恒为空串，20 条历史实际
# 只有用户那半在占上下文；一旦补全（本轮修复），20×20000 字的存档会直接撑爆本地模型。
_HISTORY_MSG_CHARS = 1200
# ---- v1.0.645：会话「渲染日志」（blocks）----
# 用户诉求：「当时的对话输出的版式是怎么样的，即使退出软件重新再点开那个任务，版面也要一样」。
# 会话原本只存 {role,content,ts} 两色文本，所以重开时步骤卡、推理框、错误/已停止标记、
# 产物文件行全丢——那些东西当时只活在 DOM 里，没进过存储。
# blocks 就是补上这一层：落库时把这一轮的渲染步骤记下来，前端用**同一批**渲染函数重放。
# 裁剪放在服务端这一处（而不是每个调用方各裁一遍）：写入口将来还会多，漏一个就是把整个
# 会话 JSON 撑大的入口。上限值取的是"够把版式复原、又不至于让会话文件失控"。
_BLOCKS_MAX = 60
_BLOCK_FIELD_MAX = {"name": 60, "args": 2000, "res": 400, "path": 300, "text": 600, "tail": 400}
# v1.0.650（F2）：done 的统计条也是渲染日志的一部分。数字字段单独一条白名单，
# 与字符串字段同样"白名单外一律丢"——bool 不当数字收（True 存成 1 会显示"1 次操作"）。
_BLOCK_NUM_FIELDS = ("step", "n", "rounds", "tool_calls", "tool_errors", "batch_done",
                     "elapsed_s", "prompt_tokens", "completion_tokens")
_BLOCK_NUM_CAP = 10 ** 12


def _clamp_blocks(blocks):
    """把渲染日志压到可存档的尺寸。只认白名单字段——前端/采集方多写的键一律丢掉，
    免得哪天有人把整段页面 HTML 塞进 block 里跟着落盘。"""
    if not blocks:
        return []
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        k = str(b.get("k") or "")[:16]
        if not k:
            continue
        item = {"k": k}
        for field, cap in _BLOCK_FIELD_MAX.items():
            if field in b:
                item[field] = str(b.get(field) or "")[:cap]
        for field in _BLOCK_NUM_FIELDS:
            v = b.get(field)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            v = max(-_BLOCK_NUM_CAP, min(_BLOCK_NUM_CAP, v))
            item[field] = round(v, 1) if isinstance(v, float) else v
        if "ok" in b:
            item["ok"] = bool(b.get("ok"))
        out.append(item)
        if len(out) >= _BLOCKS_MAX:
            break
    dropped = len(blocks) - len(out)
    if dropped > 0:
        out.append({"k": "more", "n": dropped})
    return out


def conv_dir():
    d = os.path.join(workspace_root(), "会话")
    os.makedirs(d, exist_ok=True)
    return d


def _path(cid):
    safe = str(cid).replace("/", "_").replace("\\", "_")
    return os.path.join(conv_dir(), safe + ".json")


def create(title=""):
    cid = "conv_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {"id": cid, "title": str(title or "")[:60], "created": now,
            "updated": now, "archived": False, "messages": []}
    _write(cid, data)
    _cleanup_old()
    return cid


def load(cid):
    try:
        with _CONV_LOCK:
            with io.open(_path(cid), encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _write(cid, data):
    """v1.0.361 审查修复（P18）：原子替换写（原直接覆盖，进程被杀留截断 JSON →
    会话静默消失）+ with 关闭句柄。"""
    data["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _CONV_LOCK:
        # v1.0.510（P2-13）：tmp+os.replace 样板收敛到 atomic_write_json
        atomic_write_json(_path(cid), data, indent=1)


def save(data):
    _write(data["id"], data)


def append_message(cid, role, content, blocks=None, display=None):
    """追加一条消息。
    `blocks`（v1.0.645）是这一轮的渲染日志，只给前端重放版式用——
    `history_messages()` 拼模型上下文时**不看它**，所以它再大也不会污染模型输入。
    `display`（v1.0.645）是"当时气泡上那行字"：带附件的那轮，界面显示的是
    「输入文本 + 附件引用行」，而喂给模型的是另一套拼装结果。存档仍以 `content`
    为上下文口径，重开时气泡按 `display` 画——两边各按各的真相，不互相将就。"""
    # v1.0.361 审查修复（P18）：读改写全程持锁——原 load→改→save 序列无锁，
    # SSE 生成线程追加与用户编辑/删除并发时后写整文件覆盖前写（丢消息）。
    with _CONV_LOCK:
        data = load(cid)
        if not data:
            return
        msg = {"role": role, "content": str(content)[:20000],
               "ts": datetime.datetime.now().strftime("%m-%d %H:%M")}
        _d = str(display or "")
        if _d and _d != str(content):
            msg["display"] = _d[:20000]
        _b = _clamp_blocks(blocks)
        if _b:
            msg["blocks"] = _b
        data["messages"].append(msg)
        if not data.get("title") and role == "user":
            data["title"] = str(content).strip()[:24]
        _write(cid, data)


def mutate_message(cid, role, ordinal, content=None, delete=False):
    """按「角色+同类序号」定位消息改/删（v1.0.334 气泡操作栏）。
    ordinal=该 role 在 messages 中的第几条（0 起）；前端 DOM 同 role 气泡序号与之
    一一对应（删一条后两侧顺序同时重排，保持对齐）。返回 (ok, error)。
    v1.0.361 审查修复（P18）：读改写全程持锁（同 append_message）。"""
    with _CONV_LOCK:
        data = load(cid)
        if not data:
            return False, "会话不存在"
        msgs = data.get("messages") or []
        idxs = [i for i, m in enumerate(msgs) if m.get("role") == role]
        if ordinal < 0 or ordinal >= len(idxs):
            return False, f"消息序号越界（{role} 共 {len(idxs)} 条）"
        target = idxs[ordinal]
        if delete:
            msgs.pop(target)
        else:
            msgs[target]["content"] = str(content or "")[:20000]
            msgs[target]["ts"] = datetime.datetime.now().strftime("%m-%d %H:%M") + " ✎"
        _write(cid, data)
    return True, ""


def clear_messages(cid):
    """v1.0.502：清空会话全部消息（清空按钮直通服务端，取代旧的「引导去删除」提示）。
    会话本身、标题、归档状态保留；对应 LLM 上下文随之归零。返回 (ok, error)。"""
    with _CONV_LOCK:
        data = load(cid)
        if not data:
            return False, "会话不存在"
        data["messages"] = []
        _write(cid, data)
    return True, ""


def history_messages(cid, window=_HISTORY_WINDOW):
    """拼 LLM 上下文用：最近 window 条（user/assistant 轮换，tool 类不存）。
    v1.0.628 两条口径（都是补上「agent 轮恒为空」那个 bug 之后才需要的）：
    ① 空轮跳过——{"role":"assistant","content":""} 对模型是纯噪音，思考型模型实测
       会在空 assistant 轮后续续思考而不输出正文（10-27 生成 PPT 连续三次 300s 零正文）；
    ② 每则截到 _HISTORY_MSG_CHARS 字——存档留全文，喂模型只留开头，
       否则 20 条 × 最长 2 万字的会话会把本地模型直接顶爆。"""
    data = load(cid)
    if not data:
        return []
    out = []
    for m in data.get("messages", []):
        if m.get("role") in ("user", "agent"):
            _c = str(m.get("content") or "").strip()
            if not _c:
                continue
            if len(_c) > _HISTORY_MSG_CHARS:
                _c = _c[:_HISTORY_MSG_CHARS] + "…（历史过长，此处只保留开头）"
            out.append({"role": "user" if m["role"] == "user" else "assistant",
                        "content": _c})
    return out[-window:]


def list_all():
    out = []
    d = conv_dir()
    for f in os.listdir(d):
        if not f.endswith(".json"):
            continue
        try:
            with io.open(os.path.join(d, f), encoding="utf-8") as fh:
                data = json.load(fh)
            out.append({"id": data.get("id"), "title": data.get("title", ""),
                        "archived": bool(data.get("archived")),
                        "updated": data.get("updated", ""),
                        "messages": [{"role": m.get("role")} for m in data.get("messages", [])]})
        except Exception as e:  # noqa: BLE001
            # v1.0.614：解析失败必须留痕。原先 `except: continue` 会让某个会话在列表里
            # 无声消失（本项目最擅长的那类"静默少一条"），用户只会以为记录没了。
            log_event(f"[conversations] 会话文件解析失败、已从列表跳过：{f} → {type(e).__name__}: {e}")
            continue
    out.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return out


def delete(cid):
    try:
        os.remove(_path(cid))
        return True
    except OSError:
        return False


def archive(cid, summarize=True, on=True):
    """归档 / 取消归档 + 会话级经验总结沉淀到记忆（v1.0.327 需求⑦）。

    v1.0.642：归档语义收口成"**离开任务模块，进设置的已归档任务，直到用户在那里永久删除**"。
    取消归档走同一个入口（on=False），不再另写一份读改写。"""
    data = load(cid)
    if not data:
        return False, ""
    data["archived"] = bool(on)
    save(data)
    note = ""
    if on and summarize and data.get("messages"):
        try:
            from services.memory_ops import summarize_conversation
            note = summarize_conversation(data)
        except Exception as e:  # noqa: BLE001
            log_event(f"[conversations] 归档总结失败（归档本身不受影响）：{e}")
    return True, note


def unarchive(cid):
    """把归档的任务放回任务模块（v1.0.642：入口在「设置 → 已归档任务」）。"""
    ok, _ = archive(cid, summarize=False, on=False)
    return ok


def _cleanup_old():
    """会话文件数超上限时的处理。

    v1.0.642 改了口径：**不再自动删除归档**。原来的实现是"超上限就挑最旧的归档删掉"，
    与"归档会保留在那里，直到你亲自去删"这句话直接矛盾——用户永远不知道少了哪条。
    现在只记一条告警事件留痕，删与不删交回给用户（设置里的「永久删除」）。
    上限 500 是宽松值（本机实测 96 条），真到那一天时宁可吵一句也不要静默丢记录。"""
    try:
        d = conv_dir()
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        if len(files) <= _MAX_KEEP:
            return
        log_event(f"[conversations] 会话文件已达 {len(files)} 个（上限 {_MAX_KEEP}）："
                  f"归档不再自动删除，请到「设置 → 已归档任务」清理")
    except Exception as e:  # noqa: BLE001
        log_event(f"[conversations] 检查会话数量上限失败：{e}")
