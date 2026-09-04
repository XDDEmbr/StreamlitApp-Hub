"""app.py — Streamlit App Hub 主界面。

一个用于管理本机多个 Streamlit 应用的仪表盘：
  - 一键启动 / 停止 / 重启各应用（独立进程，日志落盘 logs/）
  - 状态、端口、PID、运行时长自动刷新
  - 在 Web 端增删改应用注册表（apps.json），并生成应用目录骨架
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st

import hub_core
from hub_core import ManagedApp

st.set_page_config(
    page_title="Streamlit App Hub",
    page_icon=":material/hub:",
    layout="wide",
    initial_sidebar_state="expanded",
)

HUB_URL_PREFIX = "http://localhost:"

# ------------------------------------------------------------------ 工具
def refresh_registry() -> list[ManagedApp]:
    """重新加载注册表到 session_state，返回最新列表。"""
    apps = hub_core.ensure_registry()
    st.session_state["apps"] = apps
    return apps


def get_apps() -> list[ManagedApp]:
    if "apps" not in st.session_state:
        refresh_registry()
    return st.session_state["apps"]


def uptime_text(pid: int | None) -> str:
    if pid is None:
        return ""
    try:
        import psutil
        create = psutil.Process(pid).create_time()
        secs = int(time.time() - create)
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:  # noqa: BLE001
        return ""


def status_badge(status: str) -> None:
    if status == "running":
        st.badge("运行中", icon=":material/check_circle:", color="green")
    elif status == "occupied":
        st.badge("端口被占用", icon=":material/warning:", color="orange")
    elif status == "missing":
        st.badge("入口缺失", icon=":material/error:", color="red")
    else:
        st.badge("已停止", icon=":material/stop_circle:", color="gray")


# ------------------------------------------------------------------ 操作
def do_start(app: ManagedApp) -> None:
    try:
        ok, msg = hub_core.start_app(app)
    except FileNotFoundError as exc:
        st.session_state["last_msg"] = ("error", str(exc))
        return
    st.session_state["last_msg"] = ("success" if ok else "error", msg)
    refresh_registry()


def do_stop(app: ManagedApp) -> None:
    ok, msg = hub_core.stop_app(app)
    st.session_state["last_msg"] = ("success" if ok else "error", msg)


def do_restart(app: ManagedApp) -> None:
    hub_core.stop_app(app)
    time.sleep(0.5)
    ok, msg = hub_core.start_app(app)
    st.session_state["last_msg"] = ("success" if ok else "error", msg)


def save_registry_and_refresh(apps: list[ManagedApp]) -> None:
    hub_core.save_registry(apps)
    refresh_registry()


def show_last_msg() -> None:
    if "last_msg" not in st.session_state:
        return
    kind, msg = st.session_state.pop("last_msg")
    if kind == "success":
        st.toast(msg)
    else:
        st.toast(msg)


# ------------------------------------------------------------------ 缓存清理
def clear_app_caches() -> None:
    """清空 Streamlit 的函数缓存（cache_data / cache_resource）。

    各子应用以独立 subprocess（hub_core.start_app）运行，其进程内缓存在此无法触及；
    本函数主要清理 Hub 自身在当前进程中 @st.cache_data/@st.cache_resource 产生的缓存。
    """
    st.cache_data.clear()
    st.cache_resource.clear()


# ------------------------------------------------------------------ 对话框：新增应用
@st.dialog("新增应用", icon=":material/add:")
def add_app_dialog() -> None:
    st.caption("内置应用将创建目录 apps/<name>/ 与入口骨架文件；亦可直接登记工作区已有项目。")
    name = st.text_input("应用名", placeholder="my_app（小写字母、数字、下划线）")
    entry = st.text_input("入口文件", value="app.py",
                          help="相对项目目录的入口文件名，如 app.py")
    path = st.text_input("项目目录（可选）", placeholder="留空 = apps/<name>/",
                         help="绝对路径或相对 Hub 目录的路径，用于登记已存在的项目")
    description = st.text_area("描述", placeholder="这个应用做什么？")
    c1, c2 = st.columns(2)
    port = c1.number_input("端口", min_value=0, max_value=65535, value=0,
                           help="0 = 自动分配（从 8510 起）")
    auto_start = c2.toggle("Hub 启动时自动拉起")
    icon = st.text_input("图标", value=":material/dashboard:",
                         help="Material Symbols 图标名，如 :material/analytics:")
    python = st.text_input(
        "Python 环境（可选）", placeholder="留空=用 Hub 的 Python；conda env 名(如 study) / venv或python.exe绝对路径",
        help="例：study、llm-cpp → 用对应 conda 环境的 python。也可填 D:\\...\\venv（自动找 Scripts/python.exe）或某 python.exe 全路径")

    if st.button("创建", type="primary", width="stretch"):
        if not name or not name.replace("_", "").isalnum() or not entry:
            st.error("应用名仅允许字母、数字、下划线；入口文件不能为空。")
            return
        apps = get_apps()
        if any(a.name == name for a in apps):
            st.error(f"应用 [{name}] 已存在。")
            return
        target_port = int(port) if port else None
        if target_port and any(a.port == target_port for a in apps):
            st.error(f"端口 {target_port} 已被其他应用占用。")
            return
        app = ManagedApp(name=name, entry=entry, path=path, description=description,
                         port=target_port, auto_start=auto_start, icon=icon,
                         python=(python or "").strip())
        try:                                # entry_path 缺失会抛 FileNotFoundError
            entry_exists = app.entry_path.exists()
        except FileNotFoundError:
            entry_exists = False
        if entry_exists:
            if path:
                pass                        # 登记已有项目：入口已存在即可
            else:
                st.error(f"apps/{name}/{entry} 已存在，请直接编辑或换名。")
                return
        else:
            if path:
                st.error(f"项目目录 [{app.dir}] 下找不到入口文件 {entry}。")
                return
            # 内置应用：生成目录骨架
            app.dir.mkdir(parents=True, exist_ok=True)
            app.entry_path.write_text(_APP_SKELETON.format(app_name=name),
                                      encoding="utf-8")
        if not app.port:
            taken = {a.port for a in apps if a.port}
            from hub_core import _next_free_port
            app.port = _next_free_port(hub_core.DEFAULT_PORT_START, taken)
        save_registry_and_refresh(apps + [app])
        st.session_state["last_msg"] = ("success", f"已新增应用 [{name}]（端口 {app.port}）")
        st.rerun()


# ------------------------------------------------------------------ 对话框：编辑应用
@st.dialog("编辑应用", icon=":material/edit:")
def edit_app_dialog(app: ManagedApp) -> None:
    description = st.text_area("描述", value=app.description)
    c1, c2 = st.columns(2)
    port = c1.number_input("端口", min_value=0, max_value=65535, value=int(app.port or 0),
                           help="0 = 自动分配")
    auto_start = c2.toggle("Hub 启动时自动拉起", value=app.auto_start)
    icon = st.text_input("图标", value=app.icon)
    python = st.text_input(
        "Python 环境（可选）",
        value=getattr(app, "python", ""),
        placeholder="留空=用 Hub 的 Python；conda env 名 / venv或python.exe绝对路径")

    col_save, col_del = st.columns([3, 1])
    if col_save.button("保存", type="primary", width="stretch"):
        apps = get_apps()
        target_port = int(port) if port else None
        if target_port and any(a.name != app.name and a.port == target_port for a in apps):
            st.error(f"端口 {target_port} 已被其他应用占用。")
            return
        # 校验：配置了 python 但解析不出来 → 阻止保存并提示
        probe = ManagedApp(name=app.name, entry=app.entry, path=app.path,
                           port=app.port or 0, python=(python or "").strip())
        if (python or "").strip() and not Path(hub_core.resolve_interpreter(probe)).is_file():
            st.error(f"Python 环境 [{python}] 无法解析：请填 conda env 名 / venv目录 / python.exe全路径。")
            return
        for a in apps:
            if a.name == app.name:
                a.description = description
                a.port = target_port
                a.auto_start = auto_start
                a.icon = icon
                a.python = (python or "").strip()
        save_registry_and_refresh(apps)
        st.session_state["last_msg"] = ("success", f"[{app.name}] 配置已保存")
        st.rerun()
    if col_del.button("删除", icon=":material/delete:", width="stretch"):
        do_stop(app)
        apps = [a for a in get_apps() if a.name != app.name]
        save_registry_and_refresh(apps)
        st.session_state["last_msg"] = ("success", f"已从注册表删除 [{app.name}]（目录保留）")
        st.rerun()


# ------------------------------------------------------------------ 日志 fragment（展开时每 5s 自动追尾刷新）
@st.fragment(run_every=5)
def app_log_fragment(app: ManagedApp) -> None:
    """读取并渲染单个应用的运行日志；置于 expander 内，仅在展开时轮询。"""
    text, n_lines = hub_core.read_log(app, lines=120)
    if not text:
        st.caption("暂无日志（应用未启动或尚未产生输出）")
        return
    from datetime import datetime
    stamp = datetime.now().strftime("%H:%M:%S")
    c1, _c2 = st.columns([6, 3], vertical_alignment="center")
    c1.caption(f"最近 {n_lines} 行（每 5s 自动刷新）")
    _c2.markdown(f":gray[⏱ {stamp}]", unsafe_allow_html=True)
    st.code(text, language="log", height=240)


# ------------------------------------------------------------------ 应用卡片（接收预计算的状态）
def app_card(app: ManagedApp, status: str, pid: int | None) -> None:
    """显示单个应用卡片，状态和 PID 由外部提前计算传入。"""
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        top.markdown(f"#### {app.icon} {app.name}")
        status_badge(status)
        top.markdown(f"<div style='width:12px'></div>", unsafe_allow_html=True)
        if app.description:
            st.caption(app.description)
        else:
            st.caption("（无描述）")

        env_name = ""
        try:
            env_name = hub_core.interpreter_display(app)
        except Exception:  # noqa: BLE001
            pass
        st.caption(
            f"端口 :blue[{app.port}]  •  目录 `{app.dir}`"
            + (f"  •  运行 {uptime_text(pid)}" if status == "running" else "")
            + (f"  •  环境 `{env_name}`" if env_name else "")
        )

        c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1])
        if status == "running":
            c1.link_button(" 去打开应用", url=f"{HUB_URL_PREFIX}{app.port}",
                           width="stretch", icon=":material/open_in_new:",
                           type="primary")
            c2.button("停止", key=f"stop_{app.name}", width="stretch",
                      icon=":material/stop:", on_click=do_stop, args=(app,))
            c3.button("重启", key=f"restart_{app.name}", width="stretch",
                      icon=":material/restart_alt:", on_click=do_restart, args=(app,))
        else:
            c1.button("启动", key=f"start_{app.name}", width="stretch",
                      type="primary",
                      icon=":material/play_arrow:", on_click=do_start, args=(app,))
            if status == "missing":
                c2.button("重建入口", key=f"fix_{app.name}", width="stretch",
                          icon=":material/build:", on_click=fix_entry, args=(app,))
        c4.button("编辑", key=f"edit_{app.name}", width="stretch",
                  icon=":material/edit:", on_click=open_edit, args=(app,))

        with st.expander(f"查看日志（{app.log_path.name}）", icon=":material/article:"):
            app_log_fragment(app)


def fix_entry(app: ManagedApp) -> None:
    app.dir.mkdir(parents=True, exist_ok=True)
    try:
        entry_exists = app.entry_path.exists()
    except FileNotFoundError:
        entry_exists = False
    if not entry_exists:
        app.entry_path.write_text(_APP_SKELETON.format(app_name=app.name),
                                  encoding="utf-8")
        st.session_state["last_msg"] = ("success", f"[{app.name}] 入口文件已重建")
    else:
        st.session_state["last_msg"] = ("error", f"[{app.name}] 入口文件已存在，无需重建")


def open_edit(app: ManagedApp) -> None:
    st.session_state["editing"] = app.name


# ------------------------------------------------------------------ 汇总区（自动刷新，也使用批量状态）
@st.fragment(run_every=15)
def status_overview() -> None:
    apps = get_apps()
    status_list = hub_core.get_all_apps_status(apps)
    states = [s for s, _ in status_list]
    c1, c2, c3 = st.columns(3)
    c1.metric("应用总数", len(apps))
    c2.metric("运行中", states.count("running"))
    c3.metric("已停止", states.count("stopped") + states.count("missing"))


# ------------------------------------------------------------------ 主流程
def main() -> None:
    st.title(":material/hub: Streamlit App Hub")
    st.caption("统一管理本机的多个 Streamlit 应用：启停、状态监控、日志与注册表。")

    show_last_msg()

    # 清理缓存的反馈提示
    if ctime := st.session_state.pop("cache_cleared_at", None):
        st.toast(f"已清理全部 Streamlit 缓存（{ctime}）")

    # 侧栏：批量操作与说明
    with st.sidebar:
        st.subheader("控制台")
        apps = get_apps()
        # 这里也需要状态，但为了减少重复，可以复用主流程的status_list？但侧栏在卡片之前，单独获取一次（开销不大）
        # 或者重新获取一次，但为了保持一致，我们在此处也调用批量函数
        status_list_sidebar = hub_core.get_all_apps_status(apps)
        running = [app for app, (status, _) in zip(apps, status_list_sidebar) if status == "running"]
        stopped = [app for app, (status, _) in zip(apps, status_list_sidebar) if status in ("stopped", "missing")]

        b1, b2 = st.columns(2)
        if b1.button("全部启动", icon=":material/play_circle:", width="stretch",
                     disabled=not stopped):
            for a in stopped:
                do_start(a)
            st.rerun()
        if b2.button("全部停止", icon=":material/stop_circle:", width="stretch",
                     disabled=not running):
            for a in running:
                do_stop(a)
            st.rerun()

        if st.button("注册新应用", icon=":material/add:", width="stretch",
                     type="primary"):
            add_app_dialog()

        # 清理各被管应用的 Streamlit 缓存（cache_data / cache_resource）
        if st.button("清理全部缓存", icon=":material/cleaning_services:", width="stretch",
                     help="清空所有应用的 st.cache_data / st.cache_resource 函数缓存，下次访问将重新计算。"):
            clear_app_caches()
            # 记录时间供主流程 toast 反馈
            st.session_state["cache_cleared_at"] = time.strftime("%H:%M:%S")
            st.rerun()


        st.space("medium")
        st.caption(
            "被管应用可位于 `apps/<name>/`（内置骨架）或任意目录（登记 path）。"
            "注册表持久化在 `apps.json`，日志落在 `logs/`。"
        )
        st.caption(f"Hub 运行于 :blue[{st.get_option('server.port')}]，被管应用端口自 8510 起分配。")

        with st.expander("端口段约定", icon=":material/info:"):
            st.write(
                f"- Hub 自身：端口 {st.get_option('server.port')}\n"
                "- 被管应用：8510 起自动分配，可在编辑里固定\n"
                "- 停止应用会结束监听该端口的进程树\n"
                "- 端口被非 Python 进程占用时会标记为“端口被占用”"
            )

    status_overview()

    apps = get_apps()
    if not apps:
        st.info("注册表为空。点击右上角「注册新应用」开始。", icon=":material/info:")
        return

    # ---------- 一次批量获取所有应用状态 ----------
    status_list = hub_core.get_all_apps_status(apps)

    # 卡片网格：2 列
    cols = st.columns(2)
    for idx, app in enumerate(apps):
        status, pid = status_list[idx]
        with cols[idx % 2]:
            app_card(app, status, pid)

    # 对话框触发
    if st.session_state.get("editing"):
        target = next((a for a in apps if a.name == st.session_state["editing"]), None)
        st.session_state.pop("editing")
        if target:
            edit_app_dialog(target)


_APP_SKELETON = '''"""apps/{app_name} — 骨架入口，可直接替换为你的应用代码。"""
import streamlit as st

st.set_page_config(page_title="{app_name}", page_icon=":material/dashboard:")

st.title("{app_name}")
st.caption("由 Streamlit App Hub 管理的子应用")

col = st.columns(1)[0]
col.metric("Status", "Ready")
if st.button("Click me"):
    st.balloons()
'''


if __name__ == "__main__":
    # Hub 启动时自动拉起 auto_start 应用（仅本次会话一次）
    if not st.session_state.get("_auto_started"):
        st.session_state["_auto_started"] = True
        # 这里也使用批量获取来判断
        apps = get_apps()
        status_list = hub_core.get_all_apps_status(apps)
        for app, (status, _) in zip(apps, status_list):
            if app.auto_start and status not in ("running", "occupied"):
                do_start(app)
    main()