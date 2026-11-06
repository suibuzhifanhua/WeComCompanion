# -*- coding: utf-8 -*-
"""core.frontend_check — index.html / js 结构校验的**实现**（v1.0.619 自 tools/ 上移）。

为什么在 core 而不是 tools：`services/self_repair.py` 的补丁验证分流要调这里
（.js → 语法校验，.html → div/id/CSS 全套）。tools/ 是**开发期目录**，build.py 的
hiddenimports 扫描显式跳过它（_SPEC_SCAN_PKGS 里没有 tools，且 tools/ 无 __init__.py），
所以冻结包里 `from tools import check_frontend` 必然 ModuleNotFoundError →
被 except 吞掉后伪装成「JS 语法校验失败：<导入错误>」，正式包的自我修复从此永远修不了前端，
报错还指着错的地方。上移进 core 后它随包一起被扫描、被 `--smoke` 探针导入。

tools/check_frontend.py 保留为 **CLI 壳**（build.py 的打包门禁仍按 subprocess 调用它），
函数一律从这里 re-export，不再有两份实现。

校验项（与原 tools 版逐字一致）：
  check_div_balance        div 开/闭配对 + 嵌套栈校验
  check_dup_ids            id 属性重复检测
  check_css_braces         <style> 内花括号配对
  check_script_style_close <script>/<style> 标签闭合
  check_js_file            单文件 JS 语法（node --check）
"""
import os
import re


def check_div_balance(html, errors):
    """div 开/闭配对：计数一致 + 嵌套栈不出现'先闭后开'。自闭合 <div/> 不存在，
    但需跳过注释内的 div（<!-- --> 中的标记不参与）。"""
    # 去掉 HTML 注释，避免注释里的示例标记干扰
    txt = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    stack = []
    for m in re.finditer(r"<(/?)div\b[^>]*?(/?)>", txt, re.I):
        closing, selfclose = m.group(1) == "/", m.group(2) == "/"
        if not closing and not selfclose:
            stack.append(m.start())
        elif closing:
            if not stack:
                errors.append(f"多余的 </div>（位置 {m.start()}）：没有对应的 <div>")
                return
            stack.pop()
    if stack:
        errors.append(f"有 {len(stack)} 个 <div> 未闭合（首个位置 {stack[0]}）")


def check_dup_ids(html, errors):
    """id 属性重复检测：同一文档内 id 必须唯一（导航/锚点/getElementById 依赖）。"""
    ids = re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html)
    seen, dups = set(), set()
    for i in ids:
        if i in seen:
            dups.add(i)
        seen.add(i)
    if dups:
        errors.append(f"重复 id：{', '.join(sorted(dups))}")


def _style_blocks(html):
    """提取全部 <style>...</style> 内容。"""
    return re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S | re.I)


def _script_blocks(html):
    """提取全部 <script>...</script> 内容（不含 src 引用外部脚本的判断）。"""
    return re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I)


def check_css_braces(html, errors):
    """<style> 内花括号配对：{ 与 } 数量一致（逐块检查，报告哪一块错位）。"""
    for idx, css in enumerate(_style_blocks(html), 1):
        o, c = css.count("{"), css.count("}")
        if o != c:
            errors.append(f"<style> #{idx} 花括号不配对：{{={o} 个, }}={c} 个")


def check_script_style_close(html, errors):
    """<script>/<style> 开闭标签数量一致（漏写 </script> 会吞掉后续整个文档）。"""
    for tag in ("script", "style"):
        opens = len(re.findall(rf"<{tag}\b", html, flags=re.I))
        closes = len(re.findall(rf"</{tag}>", html, flags=re.I))
        if opens != closes:
            errors.append(f"<{tag}> 标签开闭数量不一致：开 {opens} / 闭 {closes}")


def check_js_file(path, strict=False):
    """单文件 JS 语法校验（node --check）。返回 (ok, 输出尾部)。

    v1.0.619 两处改动：
      ① 不再假设 `node` 在 PATH：走 core.portable.find_node()（系统 PATH → 软件自装的
         便携运行时），与 dsh/npm 同一套定位口径。
      ② 找不到校验器时，strict 模式返回 **False**。原实现一律返回 True 并附一句
         "node 不在 PATH（跳过）"，而 self_repair 把返回值当"补丁已通过"用——
         没有 node 的机器上，前端补丁是**未经任何校验就落地**的（测试断言 ok is False
         也因此长期红）。打包门禁（人在看输出）继续用非 strict：跳过但不拦。
    """
    import subprocess
    from core import portable as _pb
    node = _pb.find_node()
    if not node:
        msg = "未找到 node（系统 PATH 与便携运行时均无），无法校验 JS 语法"
        return (False if strict else True), msg
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=30)
        out = (r.stderr or r.stdout or "").strip()
        return r.returncode == 0, out[-400:]
    except FileNotFoundError:
        # find_node() 之后仍报缺文件（并发卸载/权限），语义与"没有校验器"一致
        msg = "node 不可执行：" + str(node)
        return (False if strict else True), msg
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:400]


def run_all(root):
    """跑全套前端校验，返回 errors 列表（空=全部通过）。
    v1.0.652（审计 P1-14）：补 js/*.js 语法校验——旧实现只查 index.html 四项，
    而 build.py 注释与 README 都宣称"JS 语法不合格直接中止打包"，16 个 js 文件
    任一语法错误都会直达用户（前端整体瘫痪；index.html 结构坏过 3 次的同一暴露面）。
    有 node 必须全过；无 node 跳过并打 ⚠️（环境性红不进基线，不误伤无 node 构建机）。"""
    p = os.path.join(root, "index.html")
    with open(p, encoding="utf-8") as f:
        html = f.read()
    errors = []
    check_div_balance(html, errors)
    check_dup_ids(html, errors)
    check_css_braces(html, errors)
    check_script_style_close(html, errors)
    js_dir = os.path.join(root, "js")
    if os.path.isdir(js_dir):
        _node_missing_warned = False
        for fn in sorted(os.listdir(js_dir)):
            if not fn.endswith(".js"):
                continue
            okj, tailj = check_js_file(os.path.join(js_dir, fn), strict=False)
            if not okj:
                errors.append(f"js/{fn} 语法校验失败：{tailj}")
            elif (not _node_missing_warned) and tailj and ("未找到 node" in tailj or "node 不可执行" in tailj):
                _node_missing_warned = True
                print(f"[frontend-check] ⚠️ {tailj}——js/ 语法校验整体跳过（有 node 的环境会强制校验）")
    return errors
