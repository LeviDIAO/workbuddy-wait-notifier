#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 等待输入提醒器 (Windows, 事件驱动 + 零依赖)
====================================================

【两种模式】见 notifier_config.json -> "mode"：
  precise   : 仅在 WorkBuddy 真正发起提问卡(AskUserQuestion)时通知。
              最精准、不刷屏；无论窗口在前台还是后台都会通知。
  heuristic : 在 precise 基础上，额外当 assistant 以问号结尾的"纯文本"提问、
              且 WorkBuddy 窗口不在前台时通知（轻量启发式，覆盖普通文字提问）。

【开关入口】
  - notifier_config.json -> "enabled": false 时，本脚本启动即退出（0 内存占用）。
  - 直接双击 notifier-control.bat 即可一键开/关：
        关闭 = 结束 Python 进程（进程被杀，内存立即释放，占用为 0）；
        开启 = 重新拉起进程，按配置的模式监听。
  - 命令行：
        python workbuddy_wait_notifier.py            # 前台运行（Ctrl+C 退出）
        python workbuddy_wait_notifier.py --toggle   # 开 <-> 关 切换
        python workbuddy_wait_notifier.py --on       # 强制开启
        python workbuddy_wait_notifier.py --off      # 强制关闭
        python workbuddy_wait_notifier.py --precise  # 切换为精准模式
        python workbuddy_wait_notifier.py --heuristic# 切换为启发式模式

不轮询：用 Windows 原生的 ReadDirectoryChangesW 监听 ~/.workbuddy/projects 目录，
只有日志被写入时才被内核唤醒，空闲时阻塞在系统调用上，CPU/内存占用≈0。

依赖：仅 Python 3 标准库 + 系统 powershell.exe。无需 pip 安装。
（可选）想用更漂亮的 Toast：PowerShell 里 Install-Module BurntToast，脚本会自动优先使用。
"""

import os
import sys
import json
import glob
import atexit
import subprocess
import ctypes
import ctypes.wintypes as wt

# ---------------------- 路径 ----------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKBUDDY_ROOT = os.path.expanduser(r"~\.workbuddy")
PROJECTS_DIR = os.path.join(WORKBUDDY_ROOT, "projects")
CONFIG_PATH = os.path.join(APP_DIR, "notifier_config.json")
PID_PATH = os.path.join(APP_DIR, "notifier.pid")
LOG_PATH = os.path.join(APP_DIR, "notifier.log")
SNIPPET_LEN = 200            # 通知里最多显示多少字
# ------------------------------------------------

DEFAULT_CONFIG = {"enabled": True, "mode": "precise", "heuristic_notify_foreground": False}


def log(msg):
    """写运行日志到 notifier.log（立即 flush，便于排查）。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()
    except Exception:
        pass

offsets = {}  # 文件路径 -> 已读字节数

# ---- Windows 常量 ----
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
try:
    psapi = ctypes.windll.psapi
except Exception:
    psapi = None

kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
    wt.DWORD, wt.DWORD, wt.HANDLE,
]
kernel32.ReadDirectoryChangesW.restype = wt.BOOL
kernel32.ReadDirectoryChangesW.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.BOOL,
    wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p,
]


# ---------------------- 配置 ----------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if cfg.get("mode") not in ("precise", "heuristic"):
        cfg["mode"] = "precise"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------- 进程管理（开关） ----------------------
def write_pid():
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def cleanup_pid():
    try:
        if os.path.exists(PID_PATH):
            with open(PID_PATH, encoding="utf-8") as f:
                cur = f.read().strip()
            if cur == str(os.getpid()):
                os.remove(PID_PATH)
    except Exception:
        pass


atexit.register(cleanup_pid)


def is_running_via_pid():
    if not os.path.exists(PID_PATH):
        return False
    try:
        with open(PID_PATH, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        h = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
        if h:
            kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def stop_running():
    if os.path.exists(PID_PATH):
        try:
            pid = int(open(PID_PATH, encoding="utf-8").read().strip())
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            os.remove(PID_PATH)
        except Exception:
            pass


def start_daemon():
    """以后台分离方式拉起自身（无控制台窗口），由新进程写 PID 并进入监听。"""
    try:
        subprocess.Popen(
            [sys.executable, __file__],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # CREATE_NO_WINDOW：无黑框，但仍留在你的交互桌面，能正常弹 GUI 通知
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
    except Exception as e:
        print("启动失败:", e)


def messagebox(text, title="WorkBuddy 提醒器"):
    try:
        user32.MessageBoxW(0, text, title, 0x40)  # MB_OK | MB_ICONINFORMATION
    except Exception:
        print(text)


# ---------------------- 前台检测 ----------------------
def is_workbuddy_foreground():
    """WorkBuddy 是否当前在前台（激活窗口）。失败安全返回 False。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = ctypes.c_uint()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        hproc = kernel32.OpenProcess(0x0400 | 0x0010, False, pid.value)  # QUERY|VM_READ
        if hproc:
            try:
                if psapi is not None:
                    buf = ctypes.create_unicode_buffer(1024)
                    psapi.GetModuleFileNameExW(hproc, None, buf, 1024)
                    if "workbuddy" in buf.value.lower():
                        return True
            finally:
                kernel32.CloseHandle(hproc)
        # 兜底：看前台窗口标题是否含 workbuddy
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            b2 = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, b2, length + 1)
            if "workbuddy" in b2.value.lower():
                return True
        return False
    except Exception:
        return False


# ---------------------- 通知 ----------------------
# 以"无窗口"方式启动子进程，避免后台 pythonw 每次调 powershell 都闪黑框
CREATE_NO_WINDOW = 0x08000000

# 系统提示音候选（不同 Win 版本文件名不同），依次尝试第一个存在的来播
SOUND_CANDIDATES = [
    r"C:\Windows\Media\Windows Notify System Generic.wav",
    r"C:\Windows\Media\Windows Notify.wav",
    r"C:\Windows\Media\Windows Proximity Notification.wav",
    r"C:\Windows\Media\notify.wav",
]


def _ps(cmd, capture=False, timeout=15):
    """无窗口运行 PowerShell；capture=True 时返回 CompletedProcess（用于读输出）。"""
    args = ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd]
    if capture:
        return subprocess.run(args, capture_output=True, encoding="utf-8",
                              errors="replace", creationflags=CREATE_NO_WINDOW, timeout=timeout)
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=CREATE_NO_WINDOW, timeout=timeout)


def show_toast(title, message):
    """弹 Windows 通知 + 铃声（可靠版，零黑框）。
    顺序：先播系统通知音（保证有铃声）-> BurntToast(仅当真装上且真发出) -> WinForms 弹窗兜底。
    WinForms MessageBox 是 100%% 可见、自带提示音的最可靠兜底；没装 BurntToast 时必走它。"""
    # 转义单引号（PowerShell 单引号字符串里用 '' 转义），避免内容破坏命令
    t = (title or "").replace("'", "''")
    m = (message or "").replace("'", "''")

    # 0) 播放系统通知音：直接播 wav（后台进程也稳出声）；候选都不存在再退回 SystemSounds
    try:
        cs = ", ".join("'%s'" % c.replace("'", "''") for c in SOUND_CANDIDATES)
        ps_sound = ("foreach ($f in @(%s)) { if (Test-Path $f) { "
                    "(New-Object System.Media.SoundPlayer $f).PlaySync(); break } }" % cs)
        _ps(ps_sound)
    except Exception:
        try:
            _ps("[System.Media.SystemSounds]::Notification.Play()")
        except Exception:
            pass

    # 1) BurntToast（若已安装，最漂亮；需 Install-Module BurntToast）
    #    只有真正执行了 New-BurntToastNotification 才认为成功（输出 BURNT_SENT 标记）
    ps_burnt = (
        "if (Get-Module -ListAvailable BurntToast) {"
        " New-BurntToastNotification -Text @('" + t + "','" + m + "') -Sound 'Notification.Default';"
        " Write-Output 'BURNT_SENT' }"
    )
    try:
        r = _ps(ps_burnt, capture=True)
        if "BURNT_SENT" in (r.stdout or ""):
            log("通知已发送(BurntToast)")
            return
    except Exception:
        pass

    # 2) 兜底：WinForms MessageBox（100%% 可见、自带提示音，最可靠；且无黑框）
    ps_box = ("Add-Type -AssemblyName System.Windows.Forms; "
              "[System.Windows.Forms.MessageBox]::Show('" + m + "','" + t + "')")
    try:
        _ps(ps_box, timeout=60)   # 放宽到 60s，避免慢点就误判失败
        log("通知已发送(MessageBox)")
        return
    except Exception:
        pass
    log("通知发送失败：所有方式都未成功")


# ---------------------- 文本抽取 ----------------------
def extract_question(obj):
    """从 AskUserQuestion 的 function_call 里抽出问题文本与选项。
    结构：arguments 是 JSON 字符串，含 questions:[{question, options:[{label, description}]}]。"""
    a = obj.get("arguments")
    if isinstance(a, str):
        try:
            d = json.loads(a)
        except Exception:
            d = {}
    elif isinstance(a, dict):
        d = a
    else:
        d = {}
    questions = d.get("questions") if isinstance(d, dict) else None
    if not isinstance(questions, list):
        return None
    parts = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        txt = (q.get("question") or q.get("header") or q.get("title") or "").strip()
        opts = [o.get("label") for o in q.get("options", []) if isinstance(o, dict) and o.get("label")]
        line = txt
        if opts:
            line += "  [" + " / ".join(opts) + "]"
        if line.strip():
            parts.append(line.strip())
    return "\n".join(parts) if parts else None


def extract_text(obj):
    """从 assistant 的 message 里抽出可读文本（content 为结构化数组）。"""
    c = obj.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for e in c:
            if isinstance(e, dict):
                t = e.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def is_text_question(text):
    """轻量启发式：仅当文本以问号（中/英文）结尾，才视为「在问你」。"""
    if not text:
        return False
    s = text.strip()
    return s.endswith("?") or s.endswith("？")


# ---------------------- 增量读取 + 触发 ----------------------
def process_new_lines(path, cfg):
    """流式读取文件新增的『完整行』；按模式触发通知。
    只前进到最后一个换行符之后，半行（agent 正在写入）留到下次，避免漏读/误读。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    start = offsets.get(path, 0)
    if size < start:          # 文件被重建/截断，从头读
        start = 0
    if size == start:
        return
    try:
        with open(path, "rb") as f:      # 二进制读，按字节算偏移更稳
            f.seek(start)
            data = f.read()
    except OSError:
        return
    nl = data.rfind(b"\n")
    if nl == -1:              # 还没凑齐一整行，等下次事件
        return
    # 容错解码：遇到非 UTF-8 字节（如 0xa0）直接当替换符，绝不让进程崩
    complete = data[:nl].decode("utf-8", errors="replace")
    offsets[path] = start + nl + 1   # 字节偏移，\n 为单字节

    mode = cfg.get("mode", "precise")
    for line in complete.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        # —— 精准触发点（两种模式都生效，无论前台/后台）——
        if obj.get("type") == "function_call" and obj.get("name") == "AskUserQuestion":
            q = extract_question(obj)
            if q:
                log("检测到 AskUserQuestion -> 触发通知")
                snippet = q.replace("\r", " ").replace("\n", " ")[:SNIPPET_LEN]
                show_toast("WorkBuddy 在等你回答（提问卡）", snippet)
            continue

        # —— 轻量启发式（仅 heuristic 模式）——
        if mode == "heuristic":
            if (obj.get("type") == "message" and obj.get("role") == "assistant"
                    and obj.get("status") == "completed"):
                text = extract_text(obj)
                if not is_text_question(text):
                    continue
                # 默认仅在 WorkBuddy 不在前台时提醒，避免你正看着时刷屏；
                # 若配置 heuristic_notify_foreground=true，则前台也提醒。
                if cfg.get("heuristic_notify_foreground", False) or not is_workbuddy_foreground():
                    log("检测到纯文本提问(heuristic) -> 触发通知")
                    snippet = text.strip().replace("\r", " ").replace("\n", " ")[:SNIPPET_LEN]
                    show_toast("WorkBuddy 在等你回答", snippet)


def discover_jsonl():
    return glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))


def scan_all(cfg):
    for p in discover_jsonl():
        try:
            process_new_lines(p, cfg)
        except Exception as e:
            log("扫描 %s 出错（已跳过）: %s" % (p, e))


def watch(cfg):
    """用 ReadDirectoryChangesW 阻塞监听，仅在目录有变更时被唤醒。"""
    INVALID = ctypes.c_void_p(-1).value
    hdir = kernel32.CreateFileW(
        PROJECTS_DIR, FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if not hdir or hdir == INVALID:
        raise ctypes.WinError()

    buf = ctypes.create_string_buffer(65536)
    nbytes = wt.DWORD()
    log("监听已启动（模式=%s）" % cfg.get("mode"))
    print("监听已启动（模式=%s，Ctrl+C 退出）..." % cfg.get("mode"))
    try:
        while True:
            ok = kernel32.ReadDirectoryChangesW(
                hdir, buf, len(buf), True,
                FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE,
                ctypes.byref(nbytes), None, None)
            if not ok:
                break
            scan_all(cfg)   # 事件极少，扫描开销可忽略
    finally:
        kernel32.CloseHandle(hdir)


def run_normal():
    cfg = load_config()
    if not cfg.get("enabled", True):
        print("enabled=false，启动即退出（0 内存占用）。用 notifier-control.bat 或 --on 开启。")
        return
    if not os.path.isdir(PROJECTS_DIR):
        print(f"未找到 {PROJECTS_DIR}，请确认 WorkBuddy 已运行过。")
        sys.exit(1)
    write_pid()
    # 先把历史消息的偏移量顶到文件末尾，启动后只提醒「新」的
    for p in discover_jsonl():
        try:
            offsets[p] = os.path.getsize(p)
        except OSError:
            pass
    try:
        watch(cfg)
    except KeyboardInterrupt:
        print("\n已退出。")


def main():
    args = sys.argv[1:]

    # 强制关
    if "--off" in args:
        stop_running()
        cfg = load_config(); cfg["enabled"] = False; save_config(cfg)
        messagebox("WorkBuddy 提醒器已关闭\n（进程已退出，内存占用为 0）")
        return

    # 强制开
    if "--on" in args:
        cfg = load_config(); cfg["enabled"] = True; save_config(cfg)
        stop_running(); start_daemon()
        messagebox("WorkBuddy 提醒器已开启\n（事件驱动监听中）")
        return

    # 开 <-> 关 切换
    if "--toggle" in args:
        if is_running_via_pid():
            stop_running()
            cfg = load_config(); cfg["enabled"] = False; save_config(cfg)
            messagebox("已关闭\n（进程已退出，内存占用为 0）")
        else:
            cfg = load_config(); cfg["enabled"] = True; save_config(cfg)
            start_daemon()
            messagebox("已开启\n（事件驱动监听中）")
        return

    # 切换模式（精准 / 启发式）
    mode_arg = None
    if "--precise" in args:
        mode_arg = "precise"
    elif "--heuristic" in args:
        mode_arg = "heuristic"
    if mode_arg:
        cfg = load_config(); cfg["mode"] = mode_arg; save_config(cfg)
        if is_running_via_pid():
            stop_running(); start_daemon()
            messagebox(f"模式已切换为：{mode_arg}\n（并已重启生效）")
        else:
            messagebox(f"模式已设为：{mode_arg}\n（下次启动时生效）")
        return

    # 无参数：前台正常运行
    run_normal()


if __name__ == "__main__":
    main()
