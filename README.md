# Streamlit App Hub

一个用于**统一管理本机多个 Streamlit 应用**的仪表盘。

## 功能

- **注册表驱动**：`apps.json` 记录每个被管应用的名字、入口文件、项目目录、端口、是否自动拉起
- **进程管理**：一键启动 / 停止 / 重启，各应用以独立进程运行（不同端口），日志落盘 `logs/`
- **实时状态**：每 5 秒刷新状态、端口、PID、运行时长；IPv6/SO_REUSEPORT 兼容探测
- **Web 端维护**：新增 / 编辑 / 删除注册表条目，可登记任意目录的项目或生成内置骨架
- **批量操作**：全部启动 / 全部停止


在 Hub 界面点「启动」即可拉起对应项目，点「打开」直接访问其独立端口。

## 目录结构

```
Streamlitapp-Hub/
├── app.py                # Hub 主界面（本应用）
├── hub_core.py           # 进程管理核心逻辑（不依赖 streamlit）
├── apps.json             # 应用注册表（由 Hub 自动维护）
├── logs/                 # 每个被管应用的运行日志
└── .streamlit/config.toml
```

## 快速开始

```bash
cd D:\AI-Code-Program\Streamlitapp-Hub
streamlit run app.py        # 默认端口 8501
```

浏览器打开 `http://localhost:8501`。

> Hub 自身占用一个端口（默认 8501），被管应用从 **8510** 起分配，互不冲突。

## 新增 / 编辑一个被管应用

在侧边栏「注册新应用」（或卡片上的「编辑」）即可：

- **登记已有项目**：填应用名 + 入口文件名 + `path`（项目的绝对路径），端口留空自动分配
- **内置骨架**：不填 `path`，Hub 会创建 `apps/<name>/app.py` 骨架

注册表也可直接手改 `apps.json`：

```json
{
  "name": "my_app",
  "entry": "app.py",            // 相对项目目录
  "path": "",                   // 空 = apps/<name>/
  "description": "",
  "port": null,                 // null = 自动分配(8510起)
  "auto_start": false,
  "icon": ":material/dashboard:",
  "python": ""                  // Python 环境，见下
}
```

改完刷新页面即可生效。

## Python 环境的配置

每个应用可独立选择启动时的 Python 解释器（`python` 字段，「新增/编辑」对话框里填）：

| `python` 填写 | 解析结果 |
|---|---|
| （留空） | 使用 Hub 自身的 Python |
| conda env 名，如 `study`、`llm-cpp` | `<anaconda_root>/envs/<name>/(Scripts\\python.exe\|bin/python)` |
| venv/conda 目录绝对路径 | 自动找该目录下的 `Scripts\python.exe`（win）/ `bin/python` |
| python.exe 绝对路径 | 原样使用 |

这样不同应用可以用**互相隔离的环境**运行，依赖互不干扰。保存时会校验环境可解析；卡片上也会显示当前解释器。

> 提示：若某应用用了 Hub 没有的包/版本，给它在独立 conda env（如 `study`）装好 streamlit 后，`python` 填该 env 名即可。

## 端口约定

| 角色 | 端口 |
|------|------|
| Hub 自身 | 8501 |
| 被管应用 | 8510/8511/8512 …（可在编辑对话框固定） |

停止应用会结束监听该端口的**整棵进程树**，确保无残留；若仍被占用会在状态里提示剩余 PID。

## 依赖

- Python 3.11+
- streamlit（1.60+）
- psutil
