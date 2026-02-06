# OpenClaw Media Automation Backend

一个用于 **媒体自动化下载闭环** 的中间件服务（FastAPI）。  
它的核心用途是作为 **Skill Provider（技能提供者）**，供 AI Agent（例如 OpenClaw）通过对话式调用一组 API，按顺序完成：

1. 搜索剧集（Sonarr lookup）
2. 添加订阅（Sonarr add series）
3. 触发下载（Sonarr command：SeasonSearch / SeriesSearch）
4. 验证下载日志（SSH 远程读取 Docker compose logs）
5. 返回最终结果（success / downloading / error / timeout）

> ✅ 说明：本项目不直接下载媒体文件，而是“编排/触发 + 验证”的中间层。

---

## 功能概览

- **Sonarr 集成**
  - `/sonarr/search`：按关键字搜索剧集（返回精简信息）
  - `/sonarr/add`：通过 tvdbId 添加订阅到 Sonarr
  - `/sonarr/download`：触发全剧/指定季搜索下载命令

- **PikPak / Docker 日志验证（SSH）**
  - `/pikpak/verify`：通过 SSH 登录远程服务器，读取 `docker compose logs`，并基于日志关键字判断下载状态

---

## 技术栈

- Python 3.11+
- FastAPI
- Uvicorn
- Requests
- Paramiko（SSH）

---

## 项目结构（示例）

```text
.
├── main.py
├── README.md
├── .env.example
└── .gitignore
```
