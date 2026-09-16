"""每天首次使用时，后台自动触发增量更新（不阻塞当次检索）。

在 MCP 工具入口调用 maybe_trigger_background_update()：
  - 若当天（自然日）已触发过，则跳过；
  - 否则启动一个脱离的独立子进程顺序执行
        ghradar update --min-stars 5000 --days 7
        ghradar embed
    并即时返回，不影响当次检索。

状态文件（均在 data/ 下）：
  - .last_auto_update      记录上次触发的日期（YYYY-MM-DD），同一天去重
  - .autoupdate.lock       文件锁，内容为后台子进程 PID；防止并发/残留锁
  - .autoupdate.pidmap??   （不单独建文件）锁内容即 PID，精确只读本进程写的那把锁
  - autoupdate.log         后台子进程 stdout+stderr 落盘日志（含时间戳）

锁失效判定（解决残留锁造成「永久停摆」）：
  1. 读出锁内 PID，用 pid_alive() 判断子进程是否存活：不存活 -> 视为陈旧锁，破锁重建；
  2. 锁 mtime 超过 LOCK_TTL（默认 86400s=24h）-> 视为陈旧锁，破锁重建；
  3. 两者都不满足 -> 判定后台任务仍在跑，本次跳过（保留原防并发语义）。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time

from . import config

# 都可以用环境变量覆盖（对应 ghradar CLI 的参数）
MIN_STARS = os.environ.get("GHRADAR_AUTO_MIN_STARS", "5000")
DAYS = os.environ.get("GHRADAR_AUTO_DAYS", "7")

# 锁存留多久后视为陈旧（秒）。正常一次 update+embed 远超不了这个时长。
LOCK_TTL = float(os.environ.get("GHRADAR_AUTO_LOCK_TTL", "86400"))

_AUTO_DATE = config.DATA_DIR / ".last_auto_update"
_AUTO_LOCK = config.DATA_DIR / ".autoupdate.lock"
_AUTO_LOG = config.DATA_DIR / "autoupdate.log"


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pid_alive(pid: int) -> bool:
    """判断 PID 是否存活（跨平台尽力而为，失败一律视为存活，避免误破锁）。"""
    if pid is None or pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows 无 signal：用「进程句柄零信号」探测。
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
            )
            if not handle:
                return False
            # 等待 0 毫秒：立即返回本轮态。STILL_ACTIVE(259) 表示还在跑。
            code = kernel32.WaitForSingleObject(handle, 0)
            kernel32.CloseHandle(handle)
            return code == 0x00000102  # WAIT_TIMEOUT (0x102=258)
        else:
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        # 探测失败时宁可认为还活着，避免重复起任务。
        return True


def _read_lock_pid() -> int | None:
    """读取锁文件内记录的 PID；损坏/非法则返回 None。"""
    try:
        txt = _AUTO_LOCK.read_text(encoding="utf-8").strip()
        return int(txt) if txt else None
    except Exception:
        return None


def _lock_is_stale() -> bool:
    """判断现有锁是否陈旧（PID 已死 或 超时），陈旧即可安全破除。"""
    if not _AUTO_LOCK.exists():
        return True
    pid = _read_lock_pid()
    if pid_alive(pid):
        # 子进程还活着：再看是否已超过 TTL（保护性兜底，防僵尸锁/挂死）
        try:
            age = time.time() - _AUTO_LOCK.stat().st_mtime
            return age > LOCK_TTL
        except Exception:
            return False
    return True  # PID 不存在/已死 -> 陈旧


def _acquire_lock() -> bool:
    """尝试独占创建锁文件并写入当前 PID。返回是否成功拿到锁。

    若已有锁但判定为陈旧，则先移除再重试（破锁重建）。
    破锁动作极窄：只针对「PID 不存在」「PID 已死」「超时」三种情况，
    绝不误删一个 PID 依然存活且在 TTL 内的活跃锁。
    """
    try:
        fd = os.open(str(_AUTO_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        if _lock_is_stale():
            try:
                _AUTO_LOCK.unlink(missing_ok=True)
            except Exception:
                return False
            # 破锁后重试一次独占创建
            try:
                fd = os.open(str(_AUTO_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                _append_log(f"[{_now()}] stale lock broken and re-acquired (pid={os.getpid()})")
                return True
            except FileExistsError:
                return False
        return False
    except Exception:
        return False


def _append_log(line: str) -> None:
    """仅用于父进程侧少量的元信息落盘（后台子进程自己的输出走文件描述符）。"""
    try:
        with open(str(_AUTO_LOG), "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


def maybe_trigger_background_update() -> None:
    """MCP 工具入口调用：当天第一次触发一次后台更新（不阻塞）。

    任何异常都不会抛出（宁可跳过，也不影响当次检索）。
    """
    try:
        config.ensure_dirs()
        today = _today()

        # 1) 同一天已触发过 -> 跳过
        try:
            if _AUTO_DATE.exists() and _AUTO_DATE.read_text(encoding="utf-8").strip() == today:
                return
        except Exception:
            pass

        # 2) 独占创建/破锁重建锁文件；有活跃后台任务在跑 -> 跳过
        if not _acquire_lock():
            return

        # 3) 先记录日期（防止同一天内再次触发）
        try:
            _AUTO_DATE.write_text(today, encoding="utf-8")
        except Exception:
            pass

        # 4) 启动脱离的独立子进程执行 update + embed。
        #    用 os.open 拿裸 fd 传给 Popen 的 stdout，绕过文件对象缓冲，
        #    stderr 并到 STDOUT，保证子进程所有输出实时落盘 autoupdate.log。
        _append_log(f"[{_now()}] auto-update triggered (update --min-stars {MIN_STARS} --days {DAYS}; then embed)")

        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"  # 子进程 print 不再块缓冲，日志实时可见

        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )

        log_fd = os.open(str(_AUTO_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            subprocess.Popen(
                [sys.executable, "-m", "ghradar.autoupdate", "run"],
                cwd=str(config.ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=flags,
            )
        finally:
            # 子进程已继承该 fd，父进程立即关闭副本。
            os.close(log_fd)
    except Exception:
        # 任何异常都静默处理，绝不影响当次检索。
        # 若锁已建但子进程未成功启动，这里保守不移除——等陈旧判定兜底。
        try:
            pass
        except Exception:
            pass


def run() -> None:
    """后台子进程入口：顺序执行 update + embed，结束后清理【本进程】的锁文件。

    只在锁内 PID 等于自己时才删锁，避免误删之后某次新触发重建的锁。
    """
    from .cli import main as cli_main

    try:
        cli_main(["update", "--min-stars", MIN_STARS, "--days", DAYS])
        cli_main(["embed"])
    except SystemExit as e:
        # argparse 退出码非零时也不中断清理流程
        if e.code not in (0, None):
            _append_log(f"[{_now()}] update/embed exited with code {e.code}")
    except Exception:
        _append_log(f"[{_now()}] update/embed failed (traceback suppressed)")
    finally:
        try:
            if _read_lock_pid() == os.getpid():
                _AUTO_LOCK.unlink(missing_ok=True)
                _append_log(f"[{_now()}] auto-update finished; lock released (pid={os.getpid()})")
        except Exception:
            pass


if __name__ == "__main__":
    run()