"""hub_core.py — Streamlit 应用管理核心逻辑（与 UI 解耦，便于测试）。

职责：
  1. 读取 / 写入应用注册表 apps.json
  2. 以独立进程启动 / 停止各个被管 Streamlit 应用
  3. 检测存活状态、回收游离端口、托管运行日志
不依赖 streamlit，纯标准库 + psutil。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - 可选依赖兜底
    psutil = None


# ---------------------------------------------------------------- 路径常量
HUB_DIR = Path(__file__).resolve().parent          # Streamlitapp-Hub/
APPS_ROOT = HUB_DIR / "apps"                        # 每个子目录 = 一个被管应用
REGISTRY_FILE = HUB_DIR / "apps.json"
LOGS_DIR = HUB_DIR / "logs"
DEFAULT_PORT_START = 8510                           # Hub 从该端口起向下分配


# ---------------------------------------------------------------- 数据模型
@dataclass
class ManagedApp:
    """注册表里登记的一个被管应用。"""

    name: str                                   # 唯一显示名，用作日志命名（小写+下划线）
    entry: str                                  # 入口文件名，如 "app.py"（相对项目目录）
    path: str = ""                              # 项目目录；为空则默认 apps/<name>/
    description: str = ""
    port: int | None = None                     # 为空则由 Hub 自动分配
    auto_start: bool = False                    # Hub 启动时是否自动拉起
    icon: str = ":material/dashboard:"          # Material Symbols 图标名
    python: str = ""                            # Python 解释器；空=用 Hub 自身

    def __post_init__(self) -> None:
        self._slug = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in (self.name or "")
        ).lower()
        self._log_file = LOGS_DIR / f"{self._slug}.log"

    @property
    def dir(self) -> Path:
        if self.path:
            p = Path(self.path)
            return p if p.is_absolute() else (HUB_DIR / p)
        return APPS_ROOT / self.name

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def log_path(self) -> Path:
        return self._log_file

    @property
    def entry_path(self) -> Path:
        """入口文件的绝对路径。校验并给出可读错误。"""
        p = (self.dir / self.entry).resolve()
        base = self.dir.resolve()
        try:
            inside = os.path.commonpath([str(p), str(base)]).startswith(str(base))
        except ValueError:
            inside = False
        if not inside or not p.is_file():
            raise FileNotFoundError(
                f"[{self.name}] 找不到入口文件: {p}（项目目录 {self.dir}）"
            )
        return p


# ---------------------------------------------------------------- Schema 字段
_FIELDS = {"name", "entry", "path", "description", "port", "auto_start", "icon", "python"}


def _as_managed(raw: dict) -> ManagedApp:
    keep = {k: v for k, v in raw.items() if k in _FIELDS}
    return ManagedApp(**keep)


# ---------------------------------------------------------------- 注册表 I/O
def load_registry_raw() -> list[dict]:
    """读取 apps.json，返回 raw dict 列表；不存在则返回空。"""
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text("utf-8"))
        apps = data.get("apps", [])
        return [a for a in apps if isinstance(a, dict)]
    except (json.JSONDecodeError, OSError) as exc:  # pragma: no cover
        raise RuntimeError(f"读取 apps.json 失败：{exc}") from exc


def save_registry(apps: list[ManagedApp]) -> None:
    """把 ManagedApp 列表写回 apps.json（含 schema_version）。"""
    payload = {
        "schema_version": 1,
        "_comment": (
            "由 Hub 自动维护。每个元素是一个可被管理的 Streamlit 应用；"
            "entry 为相对 apps/<name>/ 的入口文件路径；port 留空则自动分配。"
        ),
        "apps": [asdict(a) for a in apps],
    }
    REGISTRY_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def registry_snapshot() -> list[ManagedApp]:
    """加载 apps.json 并转换成 ManagedApp；port 未填的分配一个空闲端口。"""
    taken = set()
    managed: list[ManagedApp] = []
    for raw in load_registry_raw():
        a = _as_managed(raw)
        if a.port:
            taken.add(a.port)
        managed.append(a)

    for app in managed:
        if not app.port:
            app.port = _next_free_port(DEFAULT_PORT_START, exclude=taken)
            taken.add(app.port)
    return managed


def _next_free_port(start: int, exclude: set[int]) -> int:
    """从 start 起找第一个未占用且不在 exclude 里的端口。"""
    for p in range(start, start + 200):
        if p not in exclude and not is_port_in_use(p):
            return p
    raise RuntimeError(f"在 {start}~{start+199} 区间内找不到空闲端口")


# ---------------------------------------------------------------- 进程控制
def _proc_name(pid: int) -> str:
    try:
        if psutil:
            return (psutil.Process(pid).name() or "").lower()
    except Exception:
        pass
    return ""


def is_port_in_use(port: int) -> bool:
    """探测端口是否被监听（同时测 IPv4/IPv6 双栈）。"""
    checks = [
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port)),
    ]
    for family, addr in checks:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.bind(addr)
        except OSError:
            return True
    return False


# ---------- 批量获取所有应用状态（一次遍历连接） ----------
def get_all_apps_status(apps: list[ManagedApp]) -> list[tuple[str, int | None]]:
    """批量获取所有应用的状态和 PID，一次遍历网络连接。

    返回列表，顺序与 apps 一致，每个元素为 (status, pid)。
    """
    if not apps:
        return []

    # 先检查入口是否存在，标记 missing（entry_path 缺失会抛 FileNotFoundError）
    results = []
    need_check = []  # 需要检查端口的应用索引
    for idx, app in enumerate(apps):
        try:
            app.entry_path              # 入口缺失/越界 → FileNotFoundError
        except FileNotFoundError:
            results.append(("missing", None))
        else:
            results.append(None)  # 占位
            need_check.append(idx)

    if not need_check:
        return results

    # 获取网络连接
    try:
        conns = psutil.net_connections(kind="inet") if psutil else ()
    except (psutil.AccessDenied, OSError):
        conns = ()

    # 构建端口 -> (status, pid) 映射
    port_status = {}
    for c in conns:
        port = getattr(c.laddr, "port", None)
        if port is None:
            continue
        if c.status == psutil.CONN_LISTEN:
            name = _proc_name(c.pid) or ""
            if not name or "python" in name or "streamlit" in name:
                port_status[port] = ("running", c.pid)
            else:
                port_status[port] = ("occupied", None)

    # 填充 results
    for idx in need_check:
        app = apps[idx]
        port = app.port
        if port in port_status:
            results[idx] = port_status[port]
        else:
            # 未在连接列表中找到，使用 socket bind 检测
            if is_port_in_use(port):
                results[idx] = ("occupied", None)
            else:
                results[idx] = ("stopped", None)

    return results


# 保留单独获取单个应用状态的函数（向后兼容，但 app.py 不再使用）
def get_app_status_info(app: ManagedApp) -> tuple[str, int | None]:
    """返回 (状态, PID)。单独获取，不推荐批量场景使用。"""
    return get_all_apps_status([app])[0]


# 保留旧函数以兼容（但 app.py 已改为调用批量函数）
def is_process_alive(port: int) -> bool:
    status, _ = get_app_status_info(ManagedApp(name="dummy", entry="dummy.py", port=port))
    return status == "running"


def pid_on_port(port: int) -> int | None:
    _, pid = get_app_status_info(ManagedApp(name="dummy", entry="dummy.py", port=port))
    return pid


# ---------------------------------------------------------------- 解释器解析
def resolve_interpreter(app: ManagedApp) -> str:
    raw = app.python.strip()
    if not raw:
        return sys.executable

    cand = Path(raw)
    if cand.is_file():
        return str(cand)

    for tail in ("Scripts/python.exe", "bin/python"):
        p = (cand / tail)
        if p.exists() and p.is_file():
            return str(p)

    conda_env_py = _conda_env_interpreter(raw)
    if conda_env_py:
        return conda_env_py

    return sys.executable


def interpreter_display(app: ManagedApp) -> str:
    exe = resolve_interpreter(app)
    try:
        p = Path(exe).resolve()
    except Exception:
        return "python"

    if not app.python.strip():
        hub_base_name, _src = _hub_interpreter_label(sys.executable)
        base_dir_envs = str(p).lower().replace("\\", "/")
        if "/envs/" in base_dir_envs:
            return f"conda:{p.parent.name}"
        if hub_base_name and ("anaconda3" in p.parts or "miniconda3" in p.parts):
            return hub_base_name
        _venv = _venv_label_from_exe(p)
        return _venv or p.name

    raw = app.python.strip()
    is_path_like = ("\\" in raw) or ("/" in raw)
    if raw and not is_path_like:
        # 无需再次解析 conda env：解析结果已落在 envs/<raw>/ 下即为该环境
        marker = f"/envs/{raw}/".lower()
        if marker in str(p).lower().replace("\\", "/"):
            return f"conda:{raw}"
    vlabel = _venv_label_from_exe(p)
    if vlabel and not Path(raw).is_file():
        return f"venv:{vlabel}"
    return p.name


def _hub_interpreter_label(exe: str) -> tuple[str, int]:
    try:
        p = Path(exe)
        base_candidates = []
        for idx, part in enumerate(p.parts):
            low = part.lower()
            if low.startswith("anacon") or low.startswith("minicon"):
                base_candidates.append((part, tuple(p.parts[idx:])))
        if not base_candidates:
            return "", 0
        best_part, _rest = base_candidates[-1]
        joined_low = str(p).lower()
        is_env = "/envs/" in joined_low.replace("\\", "/")
        label = ("base" if not is_env else "conda") + f" ({best_part})"
        return label, 0
    except Exception:
        return "", 0


def _venv_label_from_exe(exe_path: Path) -> str | None:
    try:
        parts = [x.name for x in exe_path.parents]
        scripts_parent = exe_path.parent
        if scripts_parent.name.lower() in ("scripts", "bin") and len(parts) >= 2:
            env_dir = str(exe_path).lower().replace("\\", "/")
            if "/envs/" in env_dir:
                return None
            label = exe_path.parent.parent.name or scripts_parent.parent.name
            return label or None
    except Exception:
        pass
    return None


# conda env 解释器解析缓存：key=env 名 → (过期时刻, 路径|None)。
# 兜底走 conda run 一次约 10s；缓存在进程内跨 Streamlit rerun 复用，命中后毫秒级。
_INTERP_CACHE: dict[str, tuple[float, str | None]] = {}
_INTERP_CACHE_HIT_TTL = 600.0    # 命中缓存 10 分钟
_INTERP_CACHE_MISS_TTL = 60.0    # 未命中（env 不存在）缓存 1 分钟，给新建 env 留机会


def _conda_env_interpreter(name: str) -> str | None:
    """带缓存的 conda env 解释器解析（见 _conda_env_interpreter_uncached）。"""
    now = time.monotonic()
    cached = _INTERP_CACHE.get(name)
    if cached is not None and now < cached[0]:
        return cached[1]
    result = _conda_env_interpreter_uncached(name)
    ttl = _INTERP_CACHE_HIT_TTL if result else _INTERP_CACHE_MISS_TTL
    _INTERP_CACHE[name] = (now + ttl, result)
    return result


def _conda_env_interpreter_uncached(name: str) -> str | None:
    roots: list[Path] = []
    cp = os.environ.get("CONDA_PREFIX")
    if cp and Path(cp).parent.name == "envs":
        _push_root(roots, Path(cp).parent)
    for base in (Path.home(), _anaconda_base_dir()):
        _push_root(roots, base / "envs")                # base 本身即 conda 根
        for guess in ("anaconda3", "miniconda3"):
            _push_root(roots, base / guess / "envs")    # 或 conda 根位于 base 之下
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower().rstrip("\\/")
        if key in seen or not (root / name).is_dir():
            continue
        seen.add(key)
        py = _find_env_python(root / name)
        if py:
            return py
    try:
        r = subprocess.run(
            ["conda", "run", "-n", name, "python",
             "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=25)
        p = (r.stdout or "").strip()
        if p and Path(p).is_file():
            return p
    except Exception:
        pass
    return None


def _push_root(roots: list[Path], root: Path | None) -> None:
    if root is not None and str(root) not in [str(x) for x in roots]:
        roots.append(Path(root))


def _anaconda_base_dir() -> Path:
    exe = Path(sys.executable)
    for cand in (exe.parent, exe.parent.parent):
        if (cand / "envs").is_dir():
            return cand
        if str(cand).lower().endswith(("\\scripts", "/scripts")) \
                and (cand.parent / "Scripts/python.exe").exists():
            return cand.parent
    return Path.home()


def _find_env_python(env_dir: Path) -> str | None:
    """在 env 目录里定位 Python 解释器，兼容三种布局：
      - 标准 conda：Scripts/python.exe（win）/ bin/python（unix）
      - --prefix 风格：python.exe 直接位于 env 根目录（如本机 study 环境）
    """
    for tail in ("Scripts/python.exe", "python.exe", "bin/python", "python"):
        p = env_dir / tail
        if p.is_file():
            return str(p)
    return None


# ---------------------------------------------------------------- 启动/停止
def start_app(app: ManagedApp) -> tuple[bool, str]:
    if is_port_in_use(app.port):
        return False, f"[{app.name}] 端口 {app.port} 已被占用"

    entry = app.entry_path
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    interpreter = resolve_interpreter(app)

    cmd = [
        interpreter,
        "-X", "utf8",                      # 强制 Python UTF-8 模式
        "-m",
        "streamlit",
        "run",
        str(entry),
        f"--server.port={app.port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    log_fp = open(app.log_path, "ab", buffering=0)
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(app.dir),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            env=env,
        )
    except Exception as exc:
        log_fp.close()
        return False, f"[{app.name}] 启动失败：{exc}"

    log_fp.close()
    for _ in range(30):
        time.sleep(0.5)
        if _has_listener(app.port):
            return True, f"[{app.name}] 已启动（PID={proc.pid}，端口 {app.port}）"

    if pid_on_port(app.port) is not None or is_port_in_use(app.port):
        return True, f"[{app.name}] PID={proc.pid}，端口 {app.port}（已监听但未完全就绪）"
    if proc.poll() is not None:
        tail = _tail_log(app.log_path, n=10)
        hint = f"\n最近日志：\n{tail}" if tail else ""
        return False, f"[{app.name}] 启动后即退出（exit={proc.returncode}）{hint}"
    return True, f"[{app.name}] PID={proc.pid}，端口 {app.port}（进程存活但未就绪）"


def stop_app(app: ManagedApp) -> tuple[bool, str]:
    if not _has_listener(app.port):
        return True, f"[{app.name}] 未运行（端口 {app.port} 无监听）"

    for pid in dict.fromkeys(_netstat_listening_pids(app.port)):
        if not _listening_pid_alive(pid):
            continue
        hub_core_force_kill_tree(pid)

    for pid in dict.fromkeys(_netstat_listening_pids(app.port)):
        if _listening_pid_alive(pid):
            taskkill_tree(pid)

    ok = not _has_listener(app.port)
    tail_hint = ""
    if not ok:
        left = sorted(set(p for p in _netstat_listening_pids(app.port)))
        tail_hint = f"※ 仍被 PID {left} 占用"
    return ok, f"[{app.name}] {'已停止' if ok else '未完全回收'}（端口 {app.port}）{tail_hint}".strip()


def _has_listener(port: int) -> bool:
    return len(_netstat_listening_pids(port)) > 0


def _listening_pid_alive(pid: int) -> bool:
    try:
        if psutil and psutil.pid_exists(pid):
            return psutil.Process(pid).is_running()
        return False
    except Exception:
        return False


def hub_core_force_kill_tree(pid: int) -> None:
    if not psutil or not psutil.pid_exists(pid):
        return
    try:
        proc = psutil.Process(pid)
        for child in reversed(proc.children(recursive=True)):
            _try_kernel(child)
        _try_kernel(proc)
    except Exception:
        pass


def taskkill_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False, timeout=15)
        except Exception:
            pass
    else:
        import signal as _sig
        try:
            os.kill(int(pid), _sig.SIGKILL)
        except (OSError, ValueError):
            pass


def _try_kernel(proc) -> bool:
    try:
        proc.kill()
        return True
    except Exception:
        return False


def _pids_on_port(port: int) -> list[int]:
    if not psutil:
        return []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    pids = [c.pid for c in conns
            if getattr(c.laddr, "port", None) == port and c.status == psutil.CONN_LISTEN]
    return list(dict.fromkeys(pids))


def _netstat_listening_pids(port: int) -> list[int]:
    try:
        r = subprocess.run(["netstat", "-ano"],
                           capture_output=True, text=True, timeout=15)
        pids = []
        for line in (r.stdout or "").splitlines():
            toks = line.split()
            if len(toks) < 5:
                continue
            local, state, pidt = toks[1], toks[3], toks[-1]
            if state == "LISTENING" and pidt.isdigit() and portstr_matches(local, port):
                pids.append(int(pidt))
        return list(dict.fromkeys(pids))
    except Exception:
        return []


def portstr_matches(local_addr: str, port: int) -> bool:
    try:
        return local_addr.rsplit(":", 1)[1] == str(port)
    except Exception:
        return False


def pid_on_port(port: int) -> int | None:
    for pid in _pids_on_port(port):
        return pid
    return None


# ---------------------------------------------------------------- 日志读取
def _tail_log(path: Path | None, n: int = 20) -> str:
    """读取日志文件末尾若干行，自动尝试 UTF-8 失败后回退 GBK。"""
    if not path or not path.exists() or path.stat().st_size == 0:
        return ""
    lines: list[str] = []
    try:
        with open(path, "rb") as fh:
            size = path.stat().st_size
            fh.seek(max(0, size - 65536))
            for raw in deque(fh, maxlen=n):
                try:
                    lines.append(raw.decode("utf-8").rstrip())
                except UnicodeDecodeError:
                    lines.append(raw.decode("gbk", errors="replace").rstrip())
    except OSError:
        return ""
    return "\n".join(lines)


def read_log(app: ManagedApp, lines: int = 200) -> tuple[str, int]:
    if not app.log_path.exists():
        return "", 0
    text = _tail_log(app.log_path, n=lines)
    count = sum(1 for _ in text.splitlines()) if text else 0
    return text, count


# ---------------------------------------------------------------- 注册表初始化（无硬编码）
WORKSPACE_SEED = []


def ensure_registry() -> list[ManagedApp]:
    if not REGISTRY_FILE.exists():
        save_registry(WORKSPACE_SEED)
    return registry_snapshot()