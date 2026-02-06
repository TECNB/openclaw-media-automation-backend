import uvicorn
import time
import paramiko
import os
import logging
import sys
import json
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import requests
from typing import Optional, List, Union

# 建议安装 python-dotenv 来加载 .env 文件: pip install python-dotenv
from dotenv import load_dotenv
load_dotenv() 

# ==========================================
# 0. 日志配置 (Logging Configuration)
# ==========================================
# 配置日志格式：时间 - 日志级别 - 消息
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)  # 输出到控制台
        # 如果需要输出到文件，可以取消下面这行的注释
        # logging.FileHandler("openclaw.log", encoding='utf-8')
    ]
)
logger = logging.getLogger("OpenClaw")

app = FastAPI(title="OpenClaw Media Automation Backend")

# ==========================================
# 1. 配置区域 (Configuration) - 已脱敏
# ==========================================

# Sonarr Config
SONARR_HOST = os.getenv("SONARR_HOST", "127.0.0.1")
SONARR_PORT = os.getenv("SONARR_PORT", "8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "your_sonarr_api_key_here")
SONARR_BASE_URL = f"http://{SONARR_HOST}:{SONARR_PORT}/api/v3"

# SSH / Pikpak Config
SSH_HOST = os.getenv("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS", "your_ssh_password_here")
SSH_TARGET_DIR = os.getenv("SSH_TARGET_DIR", "/root/docker_pikpak")

COMMON_HEADERS = {
    "X-Api-Key": SONARR_API_KEY,
    "Content-Type": "application/json"
}

logger.info(f"System Configured - Sonarr: {SONARR_HOST}:{SONARR_PORT} | SSH: {SSH_HOST}:{SSH_PORT}")

# ==========================================
# 2. 数据模型 (Pydantic Models)
# ==========================================

class SearchRequest(BaseModel):
    keyword: str

class AddRequest(BaseModel):
    tvdb_id: int

class DownloadRequest(BaseModel):
    series_id: int
    season: Union[str, int]

class VerifyLogRequest(BaseModel):
    keyword: str
    season: Optional[Union[str, int]] = None
    series_id: Optional[int] = None  # 新增: 需要此字段来触发 Sonarr 重新扫描
    timeout_seconds: int = 40

# ==========================================
# 3. 业务服务 (Services)
# ==========================================

class SonarrService:
    def get_first_root_folder(self):
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/rootfolder", headers=COMMON_HEADERS, timeout=10)
            resp.raise_for_status()
            folders = resp.json()
            if folders: 
                path = folders[0]['path']
                logger.info(f"[Sonarr] Found root folder: {path}")
                return path
        except Exception as e:
            logger.error(f"[Sonarr] Failed to get root folder: {str(e)}")
        return None

    def get_first_quality_profile(self):
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/qualityprofile", headers=COMMON_HEADERS, timeout=10)
            resp.raise_for_status()
            profiles = resp.json()
            if profiles: 
                pid = profiles[0]['id']
                logger.info(f"[Sonarr] Found quality profile ID: {pid}")
                return pid
        except Exception as e:
            logger.error(f"[Sonarr] Failed to get quality profile: {str(e)}")
        return None

    def search(self, term: str):
        logger.info(f"[Sonarr] Searching series with term: '{term}'")
        params = {"term": term}
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/series/lookup", headers=COMMON_HEADERS, params=params, timeout=20)
            resp.raise_for_status()
            results = resp.json()
            
            cleaned_results = []
            for item in results[:5]:
                cleaned_results.append({
                    "title": item.get('title'),
                    "year": item.get('year'),
                    "tvdbId": item.get('tvdbId'),
                    "status": item.get('status'),
                    "is_in_library": True if item.get('id') else False,
                    "existing_id": item.get('id')
                })
            
            logger.info(f"[Sonarr] Search returned {len(cleaned_results)} results for '{term}'")
            return cleaned_results
        except Exception as e:
            logger.error(f"[Sonarr] Search failed: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def get_series_id_by_title(self, title: str) -> Optional[int]:
        """
        辅助方法：根据标题查找库中已存在的剧集 ID。
        用于在 verify 接口未传入 series_id 时的自动补全。
        """
        try:
            # 复用 search 方法进行查找
            results = self.search(title)
            # 1. 尝试完全匹配且在库中的
            for item in results:
                if item.get('existing_id') and item.get('title').lower() == title.lower():
                    return item.get('existing_id')
            
            # 2. 如果没有完全匹配，返回第一个在库中的结果 (模糊匹配)
            for item in results:
                if item.get('existing_id'):
                    return item.get('existing_id')
                    
        except Exception as e:
            logger.error(f"[Sonarr] Failed to lookup series ID by title: {e}")
        return None

    def add_series(self, tvdb_id: int):
        logger.info(f"[Sonarr] Attempting to add series TVDB ID: {tvdb_id}")
        
        # 1. Lookup
        lookup_resp = requests.get(f"{SONARR_BASE_URL}/series/lookup", headers=COMMON_HEADERS, params={"term": f"tvdb:{tvdb_id}"})
        lookup_resp.raise_for_status()
        results = lookup_resp.json()
        
        if not results:
            logger.warning(f"[Sonarr] TVDB ID {tvdb_id} not found via lookup")
            raise HTTPException(status_code=404, detail="TVDB ID not found")
        
        series_data = results[0]

        # 2. Check existence
        if series_data.get('id'):
            msg = f"Series '{series_data.get('title')}' is already in library."
            logger.info(f"[Sonarr] {msg}")
            return {
                "status": "already_exists",
                "message": msg,
                "series_id": series_data.get('id'),
                "seasons": self._extract_seasons(series_data)
            }

        # 3. Get config
        root_path = self.get_first_root_folder()
        profile_id = self.get_first_quality_profile()

        if not root_path or not profile_id:
            logger.error("[Sonarr] Configuration error: Missing Root Folder or Profile")
            raise HTTPException(status_code=500, detail="Sonarr Config Error: Missing Root Folder or Profile")

        # 4. Prepare payload
        series_data['rootFolderPath'] = root_path
        series_data['qualityProfileId'] = profile_id
        series_data['monitored'] = True
        series_data['addOptions'] = {'searchForMissingEpisodes': False}

        # 5. Add
        add_resp = requests.post(f"{SONARR_BASE_URL}/series", headers=COMMON_HEADERS, json=series_data)
        add_resp.raise_for_status()
        new_series = add_resp.json()

        logger.info(f"[Sonarr] Successfully added series: {new_series.get('title')} (ID: {new_series.get('id')})")
        return {
            "status": "success",
            "message": f"Successfully added '{new_series.get('title')}'",
            "series_id": new_series.get('id'),
            "seasons": self._extract_seasons(new_series)
        }

    def trigger_download(self, series_id: int, season: Union[str, int]):
        logger.info(f"[Sonarr] Triggering download for Series ID: {series_id}, Season: {season}")
        
        payload = {"seriesId": series_id}
        season_str = str(season).lower().strip()
        
        if season_str == "all":
            payload["name"] = "SeriesSearch"
        else:
            try:
                payload["name"] = "SeasonSearch"
                payload["seasonNumber"] = int(season_str)
            except ValueError:
                raise HTTPException(status_code=400, detail="Season must be 'all' or a number")

        try:
            resp = requests.post(f"{SONARR_BASE_URL}/command", headers=COMMON_HEADERS, json=payload)
            resp.raise_for_status()
            cmd_data = resp.json()
            logger.info(f"[Sonarr] Command triggered successfully. Command ID: {cmd_data.get('id')}")
            return {
                "status": "triggered",
                "command_id": cmd_data.get('id')
            }
        except Exception as e:
            logger.error(f"[Sonarr] Trigger download failed: {str(e)}")
            raise

    def rescan_series(self, series_id: int):
        """
        触发指定剧集的 'RescanSeries' 命令。
        作为后台任务运行。
        """
        if not series_id:
            logger.warning("[Sonarr] Cannot rescan series: No series_id provided.")
            return

        logger.info(f"[Sonarr] Background Task: Triggering RescanSeries for ID {series_id}")
        payload = {
            "name": "RescanSeries",
            "seriesId": series_id
        }
        
        try:
            resp = requests.post(f"{SONARR_BASE_URL}/command", headers=COMMON_HEADERS, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info(f"[Sonarr] Background Task: Rescan initiated successfully for Series ID {series_id}")
        except Exception as e:
            logger.error(f"[Sonarr] Background Task Failed: Could not rescan series {series_id}. Error: {str(e)}")


    def _extract_seasons(self, series_data):
        seasons = []
        for s in series_data.get('seasons', []):
            s_num = s.get('seasonNumber', 0)
            if s_num > 0:
                stats = s.get('statistics', {})
                seasons.append({
                    "season_number": s_num,
                    "episode_count": stats.get('episodeCount', 0),
                    "monitored": s.get('monitored', False)
                })
        return seasons


class PikpakService:
    def verify_download_start(self, keyword: str, season: Union[str, int, None], timeout: int):
        safe_keyword = keyword.replace('"', '\\"').replace('`', '')
        
        season_grep_suffix = ""
        if season and str(season).lower() != "all":
            try:
                s_num = int(season)
                s_pad = f"{s_num:02d}"
                s_raw = str(s_num)
                pattern = f"S{s_pad}|Season {s_pad}|Season {s_raw}"
                season_grep_suffix = f" | grep -iE \"{pattern}\""
            except ValueError:
                safe_season = str(season).replace('"', '\\"').replace('`', '')
                season_grep_suffix = f" | grep -i \"{safe_season}\""

        logger.info(f"[SSH/Pikpak] Starting log verification. Keyword: '{safe_keyword}', Timeout: {timeout}s")
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        found_progress = False
        last_log_snippet = ""
        result_response = {}

        try:
            client.connect(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS, timeout=10)
            start_time = time.time()
            
            while (time.time() - start_time) < timeout:
                cmd = f"cd {SSH_TARGET_DIR} && docker compose logs --tail=200 | grep -i \"{safe_keyword}\"{season_grep_suffix}"
                
                stdin, stdout, stderr = client.exec_command(cmd)
                output = stdout.read().decode('utf-8').strip()
                exit_status = stdout.channel.recv_exit_status()
                
                if exit_status == 0 and output:
                    last_log_snippet = output[-500:] # Capture last 500 chars for context

                    if "本地归档" in output or "文件已移至" in output:
                        logger.info(f"[SSH/Pikpak] Success pattern found for '{keyword}'")
                        result_response = {
                            "status": "success",
                            "message": "Download completed and moved to archive.",
                            "log_snippet": last_log_snippet
                        }
                        return result_response # Early return
                    
                    if "❌" in output and ("错误" in output or "Error" in output or "Failed" in output):
                        logger.error(f"[SSH/Pikpak] Error pattern found for '{keyword}'")
                        result_response = {
                            "status": "error",
                            "message": "Error detected in logs.",
                            "log_snippet": last_log_snippet
                        }
                        return result_response

                    if "正在提交任务" in output or "任务添加成功" in output or "离线下载" in output:
                        if not found_progress:
                            logger.info(f"[SSH/Pikpak] Progress pattern detected for '{keyword}'...")
                        found_progress = True

                time.sleep(2)
            
            # Timeout loop finished
            if found_progress:
                logger.info(f"[SSH/Pikpak] Timeout but task was downloading. Keyword: '{keyword}'")
                result_response = {
                    "status": "downloading",
                    "message": "Task started but still downloading/processing.",
                    "log_snippet": last_log_snippet
                }
            else:
                logger.warning(f"[SSH/Pikpak] Timeout with NO matching logs. Keyword: '{keyword}'")
                result_response = {
                    "status": "timeout",
                    "found": False,
                    "message": f"Keyword '{keyword}' not found in logs after {timeout} seconds."
                }

        except Exception as e:
            logger.error(f"[SSH/Pikpak] SSH Exception: {str(e)}")
            result_response = {"status": "error", "message": str(e)}
        finally:
            client.close()
            # 最终返回前，在这里可以做一次 Debug 打印
            # (注意：如果是 Early return，上面已经 log 过了，这里主要捕获 Timeout/Exception 的情况)
            pass

        return result_response

# 实例化 Services
sonarr_service = SonarrService()
pikpak_service = PikpakService()

# ==========================================
# 4. API 路由 (Routes)
# ==========================================
# 辅助函数：统一记录响应日志
def log_api_response(endpoint: str, req_data: any, resp_data: any):
    """
    Log request and response data properly.
    Truncate very long responses if necessary to avoid flooding logs.
    """
    req_str = str(req_data)
    # 将 dict 转为 json 字符串以便阅读，如果包含中文 ensure_ascii=False 可以显示中文
    try:
        if hasattr(resp_data, "dict"):
            resp_str = json.dumps(resp_data.dict(), ensure_ascii=False)
        elif isinstance(resp_data, (dict, list)):
            resp_str = json.dumps(resp_data, ensure_ascii=False)
        else:
            resp_str = str(resp_data)
    except:
        resp_str = str(resp_data)

    # 截断过长的日志 (比如搜索结果太多时)
    if len(resp_str) > 1000:
        resp_str = resp_str[:1000] + "... (truncated)"
        
    logger.info(f"\n>>> [API END] {endpoint}\n    Request:  {req_str}\n    Response: {resp_str}\n")


# --- Sonarr Routes ---
@app.post("/sonarr/search")
def api_search(req: SearchRequest):
    logger.info(f"<<< [API START] /sonarr/search | Keyword: {req.keyword}")
    result = sonarr_service.search(req.keyword)
    log_api_response("/sonarr/search", req, result)
    return result

@app.post("/sonarr/add")
def api_add(req: AddRequest):
    logger.info(f"<<< [API START] /sonarr/add | TVDB ID: {req.tvdb_id}")
    result = sonarr_service.add_series(req.tvdb_id)
    log_api_response("/sonarr/add", req, result)
    return result

@app.post("/sonarr/download")
def api_download(req: DownloadRequest):
    logger.info(f"<<< [API START] /sonarr/download | Series: {req.series_id}, Season: {req.season}")
    result = sonarr_service.trigger_download(req.series_id, req.season)
    log_api_response("/sonarr/download", req, result)
    return result

# --- Pikpak Routes ---
@app.post("/pikpak/verify")
def api_verify_log(req: VerifyLogRequest, background_tasks: BackgroundTasks):
    """
    检查日志以确认下载状态。
    如果状态是 'success' 并且提供了 'series_id'，
    则在后台触发 Sonarr 剧集重新扫描 (Rescan Series)。
    如果 series_id 为 None，尝试根据 keyword 自动反查 ID。
    """
    logger.info(f"<<< [API START] /pikpak/verify | Keyword: {req.keyword}, Series ID: {req.series_id}")
    
    # 1. 执行验证 (虽然是阻塞操作，但检查日志通常很快)
    result = pikpak_service.verify_download_start(req.keyword, req.season, req.timeout_seconds)
    
    # 2. 检查成功状态 & 触发后台任务
    if result.get("status") == "success":
        target_series_id = req.series_id
        
        # 补救措施: 如果没有提供 ID，尝试通过标题查找
        if not target_series_id:
            logger.info(f"[API] No series_id provided. Attempting to lookup series ID for '{req.keyword}'...")
            target_series_id = sonarr_service.get_series_id_by_title(req.keyword)
            
        if target_series_id:
            logger.info(f"[API] Verification Success. Scheduling background rescan for Series ID: {target_series_id}")
            background_tasks.add_task(sonarr_service.rescan_series, target_series_id)
        else:
            logger.warning("[API] Verification Success, but could not determine series_id. Skipping Sonarr rescan.")

    log_api_response("/pikpak/verify", req, result)
    return result

if __name__ == "__main__":
    # 启动时打印欢迎信息
    logger.info("Starting OpenClaw Backend Server on 0.0.0.0:8000...")
    uvicorn.run(app, host="0.0.0.0", port=8000)