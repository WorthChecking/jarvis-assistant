"""
安全沙盒与拦截器 — AOP 面向切面编程 + unittest.mock Patch
全局接管并重写危险文件操作，封死删除/修改/重命名等底层调用。
拦截 subprocess.Popen 防止通过子进程执行危险命令。
命中黑名单时仅打印 Warning 并抛出自定义 PermissionError，严禁产生实际物理修改。
"""

import os
import shutil
import subprocess
import logging
import pathlib
import re
import tempfile
from typing import Optional
from unittest.mock import patch

logger = logging.getLogger("security_interceptor")


# ============================================================
# 自定义异常
# ============================================================

class SafetyPermissionError(PermissionError):
    """安全拦截器专用权限异常"""

    def __init__(self, blocked_func: str, target: str, reason: str):
        self.blocked_func = blocked_func
        self.target = target
        self.reason = reason
        super().__init__(
            f"[SAFETY] 操作已被安全拦截器阻断 | 函数: {blocked_func} | "
            f"目标: {target} | 原因: {reason}"
        )


# ============================================================
# 敏感路径黑名单
# ============================================================

_PROTECTED_DIR_PATTERNS: list[str] = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
    r"C:\Users\All Users",
]

_PROTECTED_DIR_SUFFIXES: list[str] = [
    "$Recycle.Bin",
    "System Volume Information",
]

_SUBPROCESS_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s", re.IGNORECASE),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breg\s+(add|delete|import)", re.IGNORECASE),
    re.compile(r"\bpowershell\s", re.IGNORECASE),
    re.compile(r"\bcmd\s+/c\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bnet\s+(user|localgroup|stop|start)\b", re.IGNORECASE),
    re.compile(r"\bcd\b.*\bC:\\Windows\b", re.IGNORECASE),
    re.compile(r"\bicacls\s", re.IGNORECASE),
    re.compile(r"\btakeown\s", re.IGNORECASE),
    re.compile(r"\bcd\b.*\bSystem32\b", re.IGNORECASE),
]

_SUBPROCESS_ALLOWED_PREFIXES: list[str] = [
    "nvidia-smi",
    "everything",
    "es.exe",
]

# ========== 白名单路径配置 ==========
# 1) 临时目录
TEMP_WHITELIST: list[str] = [
    tempfile.gettempdir(),
    os.path.join(os.environ.get('WINDIR', 'C:\\WINDOWS'), 'Temp'),
]

# 2) HuggingFace 缓存根目录（Windows 默认）
HF_CACHE_ROOT: str = os.path.expanduser(os.path.join("~", ".cache", "huggingface"))

# 3) ModelScope 缓存根目录（模型下载时用 shutil.move 移动临时文件）
MODELSCOPE_CACHE_ROOT: str = os.path.expanduser(os.path.join("~", ".cache", "modelscope"))


def is_whitelisted_path(path) -> bool:
    """判断是否在白名单路径中（临时目录 + HuggingFace 缓存 + ModelScope 缓存）"""
    try:
        abs_path = os.path.abspath(str(path))
        for td in TEMP_WHITELIST:
            td_abs = os.path.abspath(td)
            if abs_path.startswith(td_abs + os.sep) or abs_path == td_abs:
                return True
        hf_abs = os.path.abspath(HF_CACHE_ROOT)
        if abs_path.startswith(hf_abs + os.sep) or abs_path == hf_abs:
            return True
        ms_abs = os.path.abspath(MODELSCOPE_CACHE_ROOT)
        if abs_path.startswith(ms_abs + os.sep) or abs_path == ms_abs:
            return True
    except Exception:
        pass
    return False


def _is_protected_path(target: str) -> Optional[str]:
    """检查路径是否命中敏感目录黑名单，命中则返回原因字符串，否则返回 None"""
    try:
        resolved = pathlib.Path(target).resolve()
        target_str = str(resolved)
    except (OSError, ValueError):
        return None

    for pattern in _PROTECTED_DIR_PATTERNS:
        try:
            pattern_resolved = str(pathlib.Path(pattern).resolve())
            if target_str.lower().startswith(pattern_resolved.lower()):
                return f"目标路径位于受保护系统目录: {pattern}"
        except (OSError, ValueError):
            continue

    for suffix in _PROTECTED_DIR_SUFFIXES:
        if suffix.lower() in target_str.lower():
            return f"目标路径包含受保护标识: {suffix}"

    return None


def _is_dangerous_subprocess_cmd(args) -> Optional[str]:
    """检查 subprocess 命令是否包含危险模式，命中返回原因，否则 None"""
    if isinstance(args, str):
        cmd_str = args
    elif isinstance(args, (list, tuple)):
        cmd_str = " ".join(str(a) for a in args)
    else:
        return None

    for prefix in _SUBPROCESS_ALLOWED_PREFIXES:
        if cmd_str.lower().startswith(prefix):
            return None

    for pattern in _SUBPROCESS_DANGEROUS_PATTERNS:
        if pattern.search(cmd_str):
            return f"子进程命令命中危险模式: {pattern.pattern}"

    return None


# ============================================================
# 原始函数备份（在模块加载时立即保存）
# ============================================================

_original_os_remove = os.remove
_original_os_rename = os.rename
_original_os_replace = os.replace
_original_shutil_rmtree = shutil.rmtree
_original_shutil_move = shutil.move
_original_os_unlink = os.unlink
_original_os_rmdir = os.rmdir
_original_shutil_copy2 = shutil.copy2
_original_shutil_copyfile = shutil.copyfile
_original_subprocess_Popen = subprocess.Popen
_original_subprocess_run = subprocess.run
_original_subprocess_call = subprocess.call
_original_subprocess_check_output = subprocess.check_output
_original_subprocess_check_call = subprocess.check_call


# ============================================================
# 拦截函数 — 文件操作
# ============================================================

def _blocked_remove(path, *args, **kwargs):
    target = str(path)
    if is_whitelisted_path(target):
        logger.info("[SAFETY PASS] os.remove 白名单放行 | 目标: %s", target)
        return _original_os_remove(path, *args, **kwargs)
    reason = _is_protected_path(target)
    if reason:
        logger.warning("[SAFETY BLOCK] os.remove 被拦截 | 目标: %s | 原因: %s", target, reason)
        raise SafetyPermissionError("os.remove", target, reason)
    logger.warning("[SAFETY BLOCK] os.remove 被全局拦截（删除操作已封死） | 目标: %s", target)
    raise SafetyPermissionError("os.remove", target, "删除操作已被全局封禁")


def _blocked_unlink(path, *args, **kwargs):
    target = str(path)
    if is_whitelisted_path(target):
        logger.info("[SAFETY PASS] os.unlink 白名单放行 | 目标: %s", target)
        return _original_os_unlink(path, *args, **kwargs)
    logger.warning("[SAFETY BLOCK] os.unlink 被全局拦截 | 目标: %s", target)
    raise SafetyPermissionError("os.unlink", target, "删除操作已被全局封禁")


def _blocked_rmdir(path, *args, **kwargs):
    target = str(path)
    if is_whitelisted_path(target):
        logger.info("[SAFETY PASS] os.rmdir 白名单放行 | 目标: %s", target)
        return _original_os_rmdir(path, *args, **kwargs)
    reason = _is_protected_path(target)
    if reason:
        logger.warning("[SAFETY BLOCK] os.rmdir 被拦截 | 目标: %s | 原因: %s", target, reason)
        raise SafetyPermissionError("os.rmdir", target, reason)
    logger.warning("[SAFETY BLOCK] os.rmdir 被全局拦截 | 目标: %s", target)
    raise SafetyPermissionError("os.rmdir", target, "目录删除操作已被全局封禁")


def _blocked_rename(src, dst, *args, **kwargs):
    src_str = str(src)
    dst_str = str(dst)
    for p in (src_str, dst_str):
        reason = _is_protected_path(p)
        if reason:
            logger.warning("[SAFETY BLOCK] os.rename 被拦截 | 涉及路径: %s | 原因: %s", p, reason)
            raise SafetyPermissionError("os.rename", p, reason)
    logger.warning("[SAFETY BLOCK] os.rename 被全局拦截 | 源: %s | 目标: %s", src_str, dst_str)
    raise SafetyPermissionError("os.rename", src_str, "重命名操作已被全局封禁")


def _blocked_replace(src, dst, *args, **kwargs):
    src_str = str(src)
    dst_str = str(dst)
    if is_whitelisted_path(src_str) or is_whitelisted_path(dst_str):
        logger.info("[SAFETY PASS] os.replace 白名单放行 | 源: %s | 目标: %s", src_str, dst_str)
        return _original_os_replace(src, dst, *args, **kwargs)
    for p in (src_str, dst_str):
        reason = _is_protected_path(p)
        if reason:
            logger.warning("[SAFETY BLOCK] os.replace 被拦截 | 涉及路径: %s | 原因: %s", p, reason)
            raise SafetyPermissionError("os.replace", p, reason)
    logger.warning("[SAFETY BLOCK] os.replace 被全局拦截 | 源: %s | 目标: %s", src_str, dst_str)
    raise SafetyPermissionError("os.replace", src_str, "替换操作已被全局封禁")


def _blocked_rmtree(path, *args, **kwargs):
    target = str(path)
    if is_whitelisted_path(target):
        logger.info("[SAFETY PASS] shutil.rmtree 白名单放行 | 目标: %s", target)
        return _original_shutil_rmtree(path, *args, **kwargs)
    reason = _is_protected_path(target)
    if reason:
        logger.warning("[SAFETY BLOCK] shutil.rmtree 被拦截 | 目标: %s | 原因: %s", target, reason)
        raise SafetyPermissionError("shutil.rmtree", target, reason)
    logger.warning("[SAFETY BLOCK] shutil.rmtree 被全局拦截 | 目标: %s", target)
    raise SafetyPermissionError("shutil.rmtree", target, "递归删除目录操作已被全局封禁")


def _blocked_move(src, dst, *args, **kwargs):
    src_str = str(src)
    dst_str = str(dst)
    if is_whitelisted_path(src_str) or is_whitelisted_path(dst_str):
        logger.info("[SAFETY PASS] shutil.move 白名单放行 | 源: %s | 目标: %s", src_str, dst_str)
        return _original_shutil_move(src, dst, *args, **kwargs)
    for p in (src_str, dst_str):
        reason = _is_protected_path(p)
        if reason:
            logger.warning("[SAFETY BLOCK] shutil.move 被拦截 | 涉及路径: %s | 原因: %s", p, reason)
            raise SafetyPermissionError("shutil.move", p, reason)
    logger.warning("[SAFETY BLOCK] shutil.move 被全局拦截 | 源: %s | 目标: %s", src_str, dst_str)
    raise SafetyPermissionError("shutil.move", src_str, "移动操作已被全局封禁")


def _guarded_copy(src, dst, *args, **kwargs):
    """复制操作：仅拦截写入到受保护目录的情况，允许普通目录间的复制"""
    dst_str = str(dst)
    reason = _is_protected_path(dst_str)
    if reason:
        logger.warning("[SAFETY BLOCK] 复制操作被拦截 | 目标: %s | 原因: %s", dst_str, reason)
        raise SafetyPermissionError("copy", dst_str, reason)
    return _original_shutil_copy2(src, dst, *args, **kwargs)


def _guarded_copyfile(src, dst, *args, **kwargs):
    dst_str = str(dst)
    reason = _is_protected_path(dst_str)
    if reason:
        logger.warning("[SAFETY BLOCK] copyfile 被拦截 | 目标: %s | 原因: %s", dst_str, reason)
        raise SafetyPermissionError("shutil.copyfile", dst_str, reason)
    return _original_shutil_copyfile(src, dst, *args, **kwargs)


# ============================================================
# 拦截函数 — subprocess 子进程
# ============================================================

def _guarded_popen(args, **kwargs):
    """拦截 subprocess.Popen，检查命令是否命中危险模式"""
    reason = _is_dangerous_subprocess_cmd(args)
    if reason:
        cmd_str = args if isinstance(args, str) else " ".join(str(a) for a in args)
        logger.warning("[SAFETY BLOCK] subprocess.Popen 被拦截 | 命令: %s | 原因: %s", cmd_str, reason)
        raise SafetyPermissionError("subprocess.Popen", cmd_str, reason)
    return _original_subprocess_Popen(args, **kwargs)


def _guarded_subprocess_run(args, **kwargs):
    reason = _is_dangerous_subprocess_cmd(args)
    if reason:
        cmd_str = args if isinstance(args, str) else " ".join(str(a) for a in args)
        logger.warning("[SAFETY BLOCK] subprocess.run 被拦截 | 命令: %s | 原因: %s", cmd_str, reason)
        raise SafetyPermissionError("subprocess.run", cmd_str, reason)
    return _original_subprocess_run(args, **kwargs)


def _guarded_subprocess_call(args, **kwargs):
    reason = _is_dangerous_subprocess_cmd(args)
    if reason:
        cmd_str = args if isinstance(args, str) else " ".join(str(a) for a in args)
        logger.warning("[SAFETY BLOCK] subprocess.call 被拦截 | 命令: %s | 原因: %s", cmd_str, reason)
        raise SafetyPermissionError("subprocess.call", cmd_str, reason)
    return _original_subprocess_call(args, **kwargs)


def _guarded_subprocess_check_output(args, **kwargs):
    reason = _is_dangerous_subprocess_cmd(args)
    if reason:
        cmd_str = args if isinstance(args, str) else " ".join(str(a) for a in args)
        logger.warning("[SAFETY BLOCK] subprocess.check_output 被拦截 | 命令: %s | 原因: %s", cmd_str, reason)
        raise SafetyPermissionError("subprocess.check_output", cmd_str, reason)
    return _original_subprocess_check_output(args, **kwargs)


def _guarded_subprocess_check_call(args, **kwargs):
    reason = _is_dangerous_subprocess_cmd(args)
    if reason:
        cmd_str = args if isinstance(args, str) else " ".join(str(a) for a in args)
        logger.warning("[SAFETY BLOCK] subprocess.check_call 被拦截 | 命令: %s | 原因: %s", cmd_str, reason)
        raise SafetyPermissionError("subprocess.check_call", cmd_str, reason)
    return _original_subprocess_check_call(args, **kwargs)


# ============================================================
# 激活 / 停用 拦截器
# ============================================================

_INTERCEPTOR_ACTIVE = False
_mock_patcher: Optional[patch] = None


def activate():
    """激活安全拦截器，全局替换危险函数 + mock patch subprocess"""
    global _INTERCEPTOR_ACTIVE, _mock_patcher
    if _INTERCEPTOR_ACTIVE:
        logger.info("[SAFETY] 拦截器已处于激活状态，跳过重复激活")
        return

    # 直接替换 os / shutil 模块级函数
    os.remove = _blocked_remove
    os.unlink = _blocked_unlink
    os.rmdir = _blocked_rmdir
    os.rename = _blocked_rename
    os.replace = _blocked_replace
    shutil.rmtree = _blocked_rmtree
    shutil.move = _blocked_move
    shutil.copy2 = _guarded_copy
    shutil.copyfile = _guarded_copyfile

    # 替换 subprocess 模块级函数
    subprocess.Popen = _guarded_popen
    subprocess.run = _guarded_subprocess_run
    subprocess.call = _guarded_subprocess_call
    subprocess.check_output = _guarded_subprocess_check_output
    subprocess.check_call = _guarded_subprocess_check_call

    _INTERCEPTOR_ACTIVE = True
    logger.info("[SAFETY] 安全拦截器已激活 — 删除/重命名/移动/危险子进程操作已被全局封禁")


def deactivate():
    """停用安全拦截器，恢复原始函数"""
    global _INTERCEPTOR_ACTIVE
    if not _INTERCEPTOR_ACTIVE:
        return

    os.remove = _original_os_remove
    os.unlink = _original_os_unlink
    os.rmdir = _original_os_rmdir
    os.rename = _original_os_rename
    os.replace = _original_os_replace
    shutil.rmtree = _original_shutil_rmtree
    shutil.move = _original_shutil_move
    shutil.copy2 = _original_shutil_copy2
    shutil.copyfile = _original_shutil_copyfile

    subprocess.Popen = _original_subprocess_Popen
    subprocess.run = _original_subprocess_run
    subprocess.call = _original_subprocess_call
    subprocess.check_output = _original_subprocess_check_output
    subprocess.check_call = _original_subprocess_check_call

    _INTERCEPTOR_ACTIVE = False
    logger.info("[SAFETY] 安全拦截器已停用 — 原始函数已恢复")


def is_active() -> bool:
    return _INTERCEPTOR_ACTIVE


# ============================================================
# 操作白名单校验器
# ============================================================

ACTION_WHITELIST: dict[str, set[str]] = {
    "device_control": {
        "volume_up", "volume_down", "volume_mute", "volume_set", "volume_get",
        "media_play_pause", "media_next", "media_prev",
        "window_minimize", "window_maximize", "system_lock",
    },
    "app_management": {
        "app_launch",
        "app_close",
    },
    "web_search": {
        "web_search",
        "open_url",
        "web_query",
        "news_query",
        "weather_query",
        "hackernews_query",
    },
    "file_search": {
        "file_search_everything",
    },
    "file_read": {
        "file_read_content",
    },
    "system_info": {
        "get_current_time",
    },
}

FILE_READ_EXTENSIONS: set[str] = {".txt", ".md", ".pdf", ".docx", ".csv"}

FILE_SEARCH_BLACKLIST: set[str] = {
    "$Recycle.Bin",
    "System Volume Information",
}


def validate_action(action_name: str, action_category: str, params: dict) -> tuple[bool, str]:
    """
    校验 LLM 返回的 Function Calling 是否在白名单内。
    返回 (是否合法, 原因说明)
    """
    if action_category not in ACTION_WHITELIST:
        return False, f"操作分类 '{action_category}' 不在白名单中"

    if action_name not in ACTION_WHITELIST[action_category]:
        return False, f"操作 '{action_name}' 不在分类 '{action_category}' 的白名单中"

    if action_category == "file_read":
        file_path = params.get("file_path", "")
        ext = pathlib.Path(file_path).suffix.lower()
        if ext not in FILE_READ_EXTENSIONS:
            return False, f"文件扩展名 '{ext}' 不在允许读取的范围内: {FILE_READ_EXTENSIONS}"
        reason = _is_protected_path(file_path)
        if reason:
            return False, f"文件路径被安全策略拦截: {reason}"

    if action_category == "file_search":
        query = params.get("query", "")
        for bl in FILE_SEARCH_BLACKLIST:
            if bl.lower() in query.lower():
                return False, f"搜索关键词命中黑名单: {bl}"

    return True, "OK"


# ============================================================
# 模块自激活：import 后自动生效
# ============================================================

activate()
