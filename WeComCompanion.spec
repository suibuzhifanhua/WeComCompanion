# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('index.html', '.'), ('app.ico', '.'), ('js', 'js'), ('css', 'css'), ('wecom_bridge/js', 'wecom_bridge/js')]
binaries = []
# v1.0.555（P0-11）：本项目包内模块由 build._autogen_hiddenimports() 构建时自动生成（第三方项保留手工清单）
hiddenimports = ['PIL', 'agent', 'agent.auto_loop', 'agent.context', 'agent.loop', 'agent.loop_guard', 'agent.loop_ops', 'agent.messages', 'agent.session', 'agent.sse_parse', 'agent.sse_stream', 'agent.sys_prompts', 'agent.team', 'agent_tools', 'agent_tools.connectors', 'agent_tools.dispatch', 'agent_tools.dsh_bridge', 'agent_tools.executors', 'agent_tools.office_gen', 'agent_tools.os_ops', 'agent_tools.registry', 'agent_tools.skill_hub_data', 'agent_tools.skill_versions', 'agent_tools.skills', 'bs4', 'core', 'core.config', 'core.config_store', 'core.daemon', 'core.files', 'core.frontend_check', 'core.idle', 'core.instance_lock', 'core.logging', 'core.notify', 'core.ocr', 'core.paths', 'core.portable', 'core.relocate', 'core.result', 'core.safe_open', 'core.secrets', 'core.smoke', 'core.strings', 'core.study_js', 'core.wecom_state', 'core.workspace_io', 'core_utils', 'ddgs', 'desktop', 'desktop.close_guard', 'desktop.save_dialog_auto', 'desktop.shell', 'desktop.tray', 'docx', 'llm_client', 'olefile', 'openpyxl', 'pptx', 'pypdf', 'pypdfium2', 'pystray', 'pystray._base', 'pystray._win32', 'routes', 'routes.b_admin', 'routes.b_macro', 'routes.b_memory', 'routes.b_models', 'routes.b_ocr_admin', 'routes.b_repair', 'routes.chat', 'routes.conversations', 'routes.knowledge', 'routes.management', 'routes.personal', 'routes.security', 'routes.skills', 'routes.study', 'routes.templates', 'routes.wecom', 'routes.workspace', 'services', 'services.asr', 'services.batch_flow', 'services.conversations', 'services.daily_digest', 'services.knowledge_ops', 'services.learn_capture', 'services.memory_ops', 'services.model_bench', 'services.rule_check', 'services.scheduler', 'services.self_repair', 'services.skill_evolve', 'services.study_engine', 'services.task_fingerprint', 'services.todo_watch', 'services.usage_stats', 'services.version_store', 'services.wecom_archive', 'striprtf', 'waitress', 'waitress.runner', 'websocket', 'websocket._logging', 'webview', 'webview.http', 'webview.js', 'webview.menu', 'webview.platforms', 'webview.platforms.edgechromium', 'webview.platforms.winforms', 'webview.util', 'webview.window', 'wecom_bridge', 'wecom_bridge.act', 'wecom_bridge.cdp_client', 'wecom_bridge.download', 'wecom_bridge.frames', 'wecom_bridge.js_loader', 'wecom_bridge.os_windows', 'wecom_bridge.read', 'wecom_bridge.screenshot', 'wecom_bridge.targets', 'wecom_bridge.tucker', 'wecom_bridge.wecom_proc', 'wecom_bridge.win_send', 'wecom_bridge.windows', 'xlrd', 'yaml']
excludes = ['webview.platforms.cocoa', 'webview.platforms.gtk', 'webview.platforms.qt',
            # v1.0.535（QA 反馈：exe 40→98MB）：语音 ASR 依赖链改「设置页一键安装」旁挂
            # （pypi wheel 下载解压到 产物库/组件/asr/，services/asr.py 运行时动态加载）——
            # ctranslate2.dll 单文件 57MB，打进去不划算；onnxruntime 保留（OCR 用）。
            'faster_whisper', 'ctranslate2', 'tokenizers', 'huggingface_hub', 'av',
            'webview.platforms.qt5', 'webview.platforms.qt6', 'webview.platforms.cef',
            # 体积优化：源码零引用，排除 PyTorch 全家（约 360MB 未压缩）
            'torch', 'torchaudio', 'torchvision', 'triton', 'torchao',
            'torch.utils', 'torch.nn', 'torch.distributed', 'torch._C', 'torch._dynamo',
            # v1.0.306 启动优化：OCR 栈旁挂 dist/ocr/（见 hiddenimports 注释）。
            # numpy 仅被 OCR 路径使用（core_utils/learn_capture 的 OCR 函数），一并旁挂。
            'cv2', 'onnxruntime', 'rapidocr_onnxruntime', 'numpy', 'scipy',
            'shapely', 'pyclipper']
tmp_ret = collect_all('jsonschema')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# v1.0.306：rapidocr_onnxruntime / onnxruntime 的 collect_all 已移除（旁挂 dist/ocr/）


a = Analysis(
    ['backend.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WeComCompanion',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # v1.0.470（P2-22）：排除 .NET 互操作与 WebView2 依赖——UPX 压缩 pythonnet 的
    # Python.Runtime.dll / clr_loader 是启动崩溃与杀软误报的经典炸点（收益有限风险不对称）
    upx_exclude=["Python.Runtime.dll", "clr_loader/*", "pythonnet/*", "WebView2*", "e_sqlite3.dll"],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
    version='version_info.txt',
)
