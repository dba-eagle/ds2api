#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 提示词复制 + 回复自动捕获助手 (稳健版)
监控 prompt.txt -> 复制到剪贴板；监听剪贴板 -> 保存回复（忽略空白差异）
"""

import os
import sys
import time
import subprocess
import threading
import hashlib
from pathlib import Path
from datetime import datetime

try:
    import pyperclip

    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ─── 配置 ──────────────────────────────────────────────
# 使用双反斜杠格式
WORK_DIR = Path(r"\\wsl.localhost\Ubuntu\opt\ds2api\.ds2api_manual")
PROMPT_FILE = WORK_DIR / "prompt.txt"

# 主要保存路径（WSL）
RESPONSE_FILE = WORK_DIR / "response.txt"
# 备用保存路径（本地Windows）
LOCAL_RESPONSE_FILE = Path(__file__).parent / "response.txt_local"

# WSL配置
WSL_DISTRO = "Ubuntu"
WSL_PATH_LINUX = "/opt/ds2api/.ds2api_manual"

MIN_REPLY_LENGTH = 50
DEBUG = os.getenv("COPTER_DEBUG", "false").lower() == "true"


# ─── 日志 ──────────────────────────────────────────────
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def debug(msg: str):
    if DEBUG:
        log(f"[DEBUG] {msg}")


# ─── 全局变量：记录最后复制的 prompt（去除空白后） ──
last_copied_prompt = ""  # 存储已处理的文本（已 strip）


# ─── WSL权限设置 ─────────────────────────────────────────
def setup_wsl_permissions() -> bool:
    """使用WSL root权限设置目录权限"""
    if sys.platform != "win32":
        log("ℹ️ 非Windows环境，跳过WSL权限设置")
        return True

    log("🔧 开始设置WSL权限...")

    try:
        # 使用WSL root权限设置目录权限
        commands = [
            f'wsl -u root -d {WSL_DISTRO} -- bash -c "chmod -R 777 {WSL_PATH_LINUX} 2>/dev/null"',
            f'wsl -u root -d {WSL_DISTRO} -- bash -c "touch {WSL_PATH_LINUX}/response.txt 2>/dev/null"',
            f'wsl -u root -d {WSL_DISTRO} -- bash -c "chmod 666 {WSL_PATH_LINUX}/response.txt 2>/dev/null"',
        ]

        success = True
        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode != 0:
                    log(f"⚠️ WSL命令执行失败: {cmd}")
                    log(f"   错误: {result.stderr}")
                    success = False
                else:
                    debug(f"✅ 执行成功: {cmd}")
            except subprocess.TimeoutExpired:
                log(f"⚠️ WSL命令超时: {cmd}")
                success = False
            except Exception as e:
                log(f"⚠️ WSL命令异常: {e}")
                success = False

        if success:
            log("✅ WSL权限设置完成")
            return True
        else:
            log("⚠️ 部分WSL命令执行失败，将使用备用路径")
            return False

    except Exception as e:
        log(f"❌ 设置WSL权限失败: {e}")
        return False


def write_via_wsl(content: str) -> bool:
    """通过WSL root权限直接写入文件"""
    try:
        # 将内容中的特殊字符转义
        escaped_content = content.replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
        # 使用WSL root写入
        cmd = f'wsl -u root -d {WSL_DISTRO} -- bash -c "echo \\"{escaped_content}\\" > {WSL_PATH_LINUX}/response.txt"'

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            log(f"✅ 通过WSL root保存成功")
            return True
        else:
            debug(f"WSL写入失败: {result.stderr}")
            return False
    except Exception as e:
        debug(f"WSL写入异常: {e}")
        return False


# ─── 剪贴板操作 ────────────────────────────────────────
def copy_to_clipboard(text: str) -> bool:
    """复制文本到剪贴板，并记录为最后复制的 prompt（去除空白）"""
    global last_copied_prompt
    if not text:
        return False
    try:
        if HAS_PYPERCLIP:
            pyperclip.copy(text)
        else:
            subprocess.run(["clip"], input=text, text=True, capture_output=True, timeout=2)
        # 存储规范化后的内容（去除首尾空白）
        last_copied_prompt = text.strip()
        log(f"✅ 已复制 {len(text)} 字符到剪贴板")
        return True
    except Exception as e:
        log(f"❌ 复制失败: {e}")
        return False


def get_clipboard_text() -> str:
    """读取剪贴板当前文本"""
    try:
        if HAS_PYPERCLIP:
            return pyperclip.paste()
        else:
            result = subprocess.run(
                ["powershell", "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=2
            )
            return result.stdout
    except Exception:
        return ""


# ─── 文件监控处理器 ────────────────────────────────────
class PromptHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_mtime = 0
        self._lock = threading.Lock()
        self.processing = False

    def on_modified(self, event):
        if event.is_directory or Path(event.src_path).name != "prompt.txt":
            return
        debug(f"watchdog 修改: {event.src_path}")
        threading.Thread(target=self._handle_prompt, daemon=True).start()

    def on_created(self, event):
        if event.is_directory or Path(event.src_path).name != "prompt.txt":
            return
        debug(f"watchdog 创建: {event.src_path}")
        threading.Thread(target=self._handle_prompt, daemon=True).start()

    def _handle_prompt(self):
        with self._lock:
            if self.processing:
                debug("上一处理未完成，跳过")
                return
            self.processing = True

        try:
            time.sleep(0.3)
            if not PROMPT_FILE.exists():
                debug("prompt.txt 不存在")
                return

            mtime = PROMPT_FILE.stat().st_mtime
            if mtime <= self.last_mtime:
                debug(f"mtime 未变: {mtime} <= {self.last_mtime}")
                return
            self.last_mtime = mtime

            content = PROMPT_FILE.read_text(encoding="utf-8")
            stripped = content.strip()
            if not stripped:
                debug("内容为空，跳过")
                return

            log(f"📄 读取到新 prompt，长度: {len(content)} 字符")
            if copy_to_clipboard(content):  # 复制原始内容，但内部会存储 strip 后的
                log("💡 请手动粘贴到 DeepSeek 输入框 (Ctrl+V)")
                log("💡 等待您点击网页复制按钮...")
            else:
                log("⚠️ 复制失败")

        except Exception as e:
            log(f"❌ 处理出错: {e}")
        finally:
            self.processing = False


# ─── 剪贴板监听 ────────────────────────────────────────
def save_response(content: str) -> bool:
    """尝试保存回复，按优先级尝试多种方法"""

    # 方法1：通过WSL root直接写入
    log("🔧 尝试通过WSL root保存...")
    if write_via_wsl(content):
        return True

    # 方法2：直接写入Windows路径
    log("🔧 尝试直接写入WSL挂载路径...")
    try:
        RESPONSE_FILE.write_text(content, encoding="utf-8")
        log(f"✅ 回复已保存到 {RESPONSE_FILE}")
        return True
    except Exception as e:
        log(f"⚠️ 直接写入失败: {e}")

    # 方法3：使用备用本地路径
    log("🔧 使用备用本地路径...")
    try:
        LOCAL_RESPONSE_FILE.write_text(content, encoding="utf-8")
        log(f"✅ 回复已保存到备用路径 {LOCAL_RESPONSE_FILE}")
        log("💡 提示：WSL路径权限问题，已使用本地备用路径")
        return True
    except Exception as e:
        log(f"❌ 保存到备用路径失败: {e}")
        return False


def clipboard_monitor():
    """后台线程：轮询剪贴板变化，智能捕获回复（忽略空白差异）"""
    global last_copied_prompt
    # 启动时读取当前剪贴板内容，设为已处理（去除空白）
    initial = get_clipboard_text()
    if initial and len(initial.strip()) >= MIN_REPLY_LENGTH:
        last_copied_prompt = initial.strip()
        log(f"🔄 启动时剪贴板已有内容，已记录为已处理 (长度 {len(initial)})")
    else:
        last_copied_prompt = ""

    log("🔄 开始监听剪贴板变化 (用于捕获回复)")
    last_hash = ""

    while True:
        try:
            current = get_clipboard_text()
            if current:
                current_stripped = current.strip()
                current_hash = hashlib.md5(current_stripped.encode("utf-8")).hexdigest()

                # 如果去除空白后与最后复制的 prompt/已保存回复相同，则忽略
                if current_stripped == last_copied_prompt:
                    debug("剪贴板内容与最后处理的内容（忽略空白）相同，忽略")
                    time.sleep(1)
                    continue

                # 长度过短忽略
                if len(current_stripped) < MIN_REPLY_LENGTH:
                    debug(f"内容过短 ({len(current_stripped)} 字符)，忽略")
                    time.sleep(1)
                    continue

                # 若内容变化且与已记录的不同，视为新回复
                if current_hash != last_hash:
                    last_hash = current_hash
                    log(f"📝 检测到回复内容，长度: {len(current_stripped)}")
                    if save_response(current_stripped):
                        log("✅ 内容已返回给 claude code")
                        # 更新记录，避免重复保存
                        last_copied_prompt = current_stripped
                    else:
                        log("❌ 保存回复失败，内容未记录")
                    time.sleep(1)
            else:
                # 剪贴板为空时重置哈希
                last_hash = ""
        except Exception as e:
            debug(f"剪贴板监听出错: {e}")
        time.sleep(0.5)


# ─── 主程序 ────────────────────────────────────────────
def main():
    print(r"""
    ╔══════════════════════════════════════════════════════════════╗
    ║   DeepSeek 提示词复制 + 回复自动捕获助手 (稳健版)         ║
    ║   监控 prompt.txt -> 复制到剪贴板；监听剪贴板 -> 保存回复  ║
    ║   支持通过WSL root权限自动设置文件权限                     ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    log(f"工作目录: {WORK_DIR}")
    log(f"主输出文件: {RESPONSE_FILE}")
    log(f"备用输出文件: {LOCAL_RESPONSE_FILE}")
    log(f"调试模式: {DEBUG}")
    log(f"WSL发行版: {WSL_DISTRO}")
    log(f"WSL路径: {WSL_PATH_LINUX}")

    if not WORK_DIR.exists():
        log(f"❌ 错误: 路径不存在 {WORK_DIR}")
        log(f"💡 请确保WSL已启动并且路径正确")
        sys.exit(1)

    # 启动时设置WSL权限
    if not setup_wsl_permissions():
        log("⚠️ WSL权限设置失败，将使用备用路径")

    # 初始化处理器
    handler = PromptHandler()
    if PROMPT_FILE.exists():
        handler.last_mtime = PROMPT_FILE.stat().st_mtime
        log(f"初始化 last_mtime = {handler.last_mtime} (忽略现有文件)")

    # 启动 watchdog
    observer = Observer()
    observer.schedule(handler, str(WORK_DIR), recursive=False)
    observer.start()
    log("✅ 文件监控 (watchdog) 已启动")

    # 主动轮询
    def poll_loop():
        while True:
            try:
                if PROMPT_FILE.exists():
                    mtime = PROMPT_FILE.stat().st_mtime
                    if mtime > handler.last_mtime:
                        debug(f"轮询发现新修改: mtime={mtime}")
                        threading.Thread(target=handler._handle_prompt, daemon=True).start()
            except Exception as e:
                debug(f"轮询出错: {e}")
            time.sleep(2)

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()
    log("✅ 主动轮询已启动 (每2秒检查)")

    # 启动剪贴板监听
    clip_thread = threading.Thread(target=clipboard_monitor, daemon=True)
    clip_thread.start()

    log("📌 等待 ds2api 写入 prompt.txt ...")
    log("💡 流程: 自动复制 prompt -> 您手动粘贴发送 -> 回复后点击网页复制按钮 -> 自动保存回复")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        log("\n收到退出信号")
    finally:
        observer.stop()
        observer.join()
        log("已退出")


if __name__ == "__main__":
    main()
