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
from typing import Optional, List, Union, Literal

# 建议安装 python-dotenv 来加载 .env 文件: pip install python-dotenv
from dotenv import load_dotenv
load_dotenv() 

# ==========================================
# 0. 日志配置 (Logging Configuration)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("OpenClaw")

app = FastAPI(title="OpenClaw Media Automation Backend")

# ==========================================
# 1. 配置区域 (Configuration)
# ==========================================

# Sonarr Config
SONARR_HOST = os.getenv("SONARR_HOST", "127.0.0.1")
SONARR_PORT = os.getenv("SONARR_PORT", "8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
SONARR_BASE_URL = f"http://{SONARR_HOST}:{SONARR_PORT}/api/v3"

# Radarr Config
RADARR_HOST = os.getenv("RADARR_HOST", "127.0.0.1")
RADARR_PORT = os.getenv("RADARR_PORT", "7878")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")
RADARR_BASE_URL = f"http://{RADARR_HOST}:{RADARR_PORT}/api/v3"

# SSH / Pikpak Config
SSH_HOST = os.getenv("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS", "")
SSH_TARGET_DIR = os.getenv("SSH_TARGET_DIR", "/root/docker_pikpak")

# Headers builders
def get_sonarr_headers():
    return {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}

def get_radarr_headers():
    return {"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"}

logger.info(f"System Configured - Sonarr: {SONARR_PORT} | Radarr: {RADARR_PORT}")

# ==========================================
# 2. 数据模型 (Pydantic Models)
# ==========================================

class SearchRequest(BaseModel):
    keyword: str

# Sonarr Models
class SonarrAddRequest(BaseModel):
    tvdb_id: int

class SonarrDownloadRequest(BaseModel):
    series_id: int
    season: Union[str, int]

# Radarr Models
class RadarrAddRequest(BaseModel):
    tmdb_id: int

class RadarrDownloadRequest(BaseModel):
    movie_id: int

# Verification Model
class VerifyLogRequest(BaseModel):
    keyword: str
    season: Optional[Union[str, int]] = None
    # category 用于区分是 Sonarr 还是 Radarr，决定扫描逻辑
    category: Literal["sonarr", "radarr"] = "sonarr"
    # media_id 是通用的，对应 series_id 或 movie_id
    media_id: Optional[int] = None  
    timeout_seconds: int = 40

# ==========================================
# 3. 业务服务 (Services)
# ==========================================

class SonarrService:
    def get_first_root_folder(self):
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/rootfolder", headers=get_sonarr_headers(), timeout=10)
            resp.raise_for_status()
            folders = resp.json()
            if folders: 
                return folders[0]['path']
        except Exception as e:
            logger.error(f"[Sonarr] Failed to get root folder: {str(e)}")
        return None

    def get_first_quality_profile(self):
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/qualityprofile", headers=get_sonarr_headers(), timeout=10)
            resp.raise_for_status()
            profiles = resp.json()
            if profiles: 
                return profiles[0]['id']
        except Exception as e:
            logger.error(f"[Sonarr] Failed to get quality profile: {str(e)}")
        return None

    def search(self, term: str):
        logger.info(f"[Sonarr] Searching series: '{term}'")
        try:
            resp = requests.get(f"{SONARR_BASE_URL}/series/lookup", headers=get_sonarr_headers(), params={"term": term}, timeout=20)
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
            return cleaned_results
        except Exception as e:
            logger.error(f"[Sonarr] Search failed: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def get_series_id_by_title(self, title: str) -> Optional[int]:
        try:
            results = self.search(title)
            # 1. Exact match
            for item in results:
                if item.get('existing_id') and item.get('title').lower() == title.lower():
                    return item.get('existing_id')
            # 2. Fuzzy match
            for item in results:
                if item.get('existing_id'):
                    return item.get('existing_id')
        except Exception:
            pass
        return None

    def add_series(self, tvdb_id: int):
        logger.info(f"[Sonarr] Adding series TVDB ID: {tvdb_id}")
        
        # Lookup
        lookup_resp = requests.get(f"{SONARR_BASE_URL}/series/lookup", headers=get_sonarr_headers(), params={"term": f"tvdb:{tvdb_id}"})
        lookup_resp.raise_for_status()
        results = lookup_resp.json()
        
        if not results:
            raise HTTPException(status_code=404, detail="TVDB ID not found")
        
        series_data = results[0]
        if series_data.get('id'):
            return {"status": "already_exists", "series_id": series_data.get('id'), "seasons": self._extract_seasons(series_data)}

        root_path = self.get_first_root_folder()
        profile_id = self.get_first_quality_profile()

        series_data['rootFolderPath'] = root_path
        series_data['qualityProfileId'] = profile_id
        series_data['monitored'] = True
        series_data['addOptions'] = {'searchForMissingEpisodes': False}

        add_resp = requests.post(f"{SONARR_BASE_URL}/series", headers=get_sonarr_headers(), json=series_data)
        add_resp.raise_for_status()
        new_series = add_resp.json()

        return {
            "status": "success",
            "series_id": new_series.get('id'),
            "seasons": self._extract_seasons(new_series)
        }

    def trigger_download(self, series_id: int, season: Union[str, int]):
        logger.info(f"[Sonarr] Trigger download Series: {series_id}, Season: {season}")
        payload = {"seriesId": series_id}
        season_str = str(season).lower().strip()
        
        if season_str == "all":
            payload["name"] = "SeriesSearch"
        else:
            payload["name"] = "SeasonSearch"
            payload["seasonNumber"] = int(season_str)

        resp = requests.post(f"{SONARR_BASE_URL}/command", headers=get_sonarr_headers(), json=payload)
        resp.raise_for_status()
        return {"status": "triggered", "command_id": resp.json().get('id')}

    def rescan_series(self, series_id: int):
        if not series_id: return
        logger.info(f"[Sonarr] Background: RescanSeries ID {series_id}")
        payload = {"name": "RescanSeries", "seriesId": series_id}
        try:
            requests.post(f"{SONARR_BASE_URL}/command", headers=get_sonarr_headers(), json=payload, timeout=10)
        except Exception as e:
            logger.error(f"[Sonarr] Rescan failed: {e}")

    def _extract_seasons(self, series_data):
        seasons = []
        for s in series_data.get('seasons', []):
            if s.get('seasonNumber', 0) > 0:
                seasons.append({
                    "season_number": s.get('seasonNumber'),
                    "monitored": s.get('monitored')
                })
        return seasons

class RadarrService:
    def get_first_root_folder(self):
        try:
            resp = requests.get(f"{RADARR_BASE_URL}/rootfolder", headers=get_radarr_headers(), timeout=10)
            resp.raise_for_status()
            folders = resp.json()
            if folders: return folders[0]['path']
        except Exception as e:
            logger.error(f"[Radarr] Failed to get root folder: {str(e)}")
        return None

    def get_first_quality_profile(self):
        try:
            resp = requests.get(f"{RADARR_BASE_URL}/qualityprofile", headers=get_radarr_headers(), timeout=10)
            resp.raise_for_status()
            profiles = resp.json()
            if profiles: return profiles[0]['id']
        except Exception as e:
            logger.error(f"[Radarr] Failed to get quality profile: {str(e)}")
        return None

    def search(self, term: str):
        logger.info(f"[Radarr] Searching movie: '{term}'")
        try:
            resp = requests.get(f"{RADARR_BASE_URL}/movie/lookup", headers=get_radarr_headers(), params={"term": term}, timeout=20)
            resp.raise_for_status()
            results = resp.json()
            
            cleaned_results = []
            for item in results[:5]:
                cleaned_results.append({
                    "title": item.get('title'),
                    "year": item.get('year'),
                    "tmdbId": item.get('tmdbId'), # 注意这里是 tmdbId
                    "status": item.get('status'),
                    "is_in_library": True if item.get('id') else False,
                    "existing_id": item.get('id')
                })
            return cleaned_results
        except Exception as e:
            logger.error(f"[Radarr] Search failed: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
    
    def get_movie_id_by_title(self, title: str) -> Optional[int]:
        # 类似 Sonarr 的反查逻辑
        try:
            results = self.search(title)
            for item in results:
                if item.get('existing_id'):
                    return item.get('existing_id')
        except Exception:
            pass
        return None

    def add_movie(self, tmdb_id: int):
        logger.info(f"[Radarr] Adding movie TMDB ID: {tmdb_id}")
        
        # Lookup first
        lookup_resp = requests.get(f"{RADARR_BASE_URL}/movie/lookup", headers=get_radarr_headers(), params={"term": f"tmdb:{tmdb_id}"})
        lookup_resp.raise_for_status()
        results = lookup_resp.json()
        
        if not results:
            raise HTTPException(status_code=404, detail="TMDB ID not found")
        
        movie_data = results[0]
        if movie_data.get('id'):
            return {"status": "already_exists", "movie_id": movie_data.get('id'), "message": "Movie is already in library"}

        root_path = self.get_first_root_folder()
        profile_id = self.get_first_quality_profile()

        # Radarr Specific Payload
        movie_data['rootFolderPath'] = root_path
        movie_data['qualityProfileId'] = profile_id
        movie_data['monitored'] = True
        movie_data['minimumAvailability'] = "released" # 或者是 announced
        movie_data['addOptions'] = {'searchForMovie': False}

        add_resp = requests.post(f"{RADARR_BASE_URL}/movie", headers=get_radarr_headers(), json=movie_data)
        add_resp.raise_for_status()
        new_movie = add_resp.json()

        return {
            "status": "success",
            "movie_id": new_movie.get('id'),
            "title": new_movie.get('title')
        }

    def trigger_download(self, movie_id: int):
        logger.info(f"[Radarr] Trigger download Movie ID: {movie_id}")
        # Radarr 命令通常是 MoviesSearch, 参数是 movieIds (list)
        payload = {
            "name": "MoviesSearch", 
            "movieIds": [movie_id]
        }
        
        resp = requests.post(f"{RADARR_BASE_URL}/command", headers=get_radarr_headers(), json=payload)
        resp.raise_for_status()
        return {"status": "triggered", "command_id": resp.json().get('id')}

    def rescan_movie(self, movie_id: int):
        if not movie_id: return
        logger.info(f"[Radarr] Background: RefreshMovie ID {movie_id}")
        # Radarr 使用 RefreshMovie 来更新元数据和扫描文件
        payload = {"name": "RefreshMovie", "movieId": movie_id}
        try:
            requests.post(f"{RADARR_BASE_URL}/command", headers=get_radarr_headers(), json=payload, timeout=10)
        except Exception as e:
            logger.error(f"[Radarr] Rescan failed: {e}")


class PikpakService:
    def verify_download_start(self, keyword: str, season: Union[str, int, None], timeout: int):
        safe_keyword = keyword.replace('"', '\\"').replace('`', '')
        
        season_grep_suffix = ""
        # 只有当 season 存在且不是 "all" 时才进行 grep 过滤，这对电影模式很重要
        if season and str(season).lower() not in ["all", "none", ""]:
            try:
                s_num = int(season)
                s_pad = f"{s_num:02d}"
                s_raw = str(s_num)
                pattern = f"S{s_pad}|Season {s_pad}|Season {s_raw}"
                season_grep_suffix = f" | grep -iE \"{pattern}\""
            except ValueError:
                pass # 如果转换失败，忽略 season 过滤

        logger.info(f"[SSH] Verifying. Keyword: '{safe_keyword}', Suffix: '{season_grep_suffix}'")
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        last_log = ""
        found_progress = False

        try:
            client.connect(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS, timeout=10)
            start_time = time.time()
            
            while (time.time() - start_time) < timeout:
                cmd = f"cd {SSH_TARGET_DIR} && docker compose logs --tail=200 | grep -i \"{safe_keyword}\"{season_grep_suffix}"
                stdin, stdout, stderr = client.exec_command(cmd)
                output = stdout.read().decode('utf-8').strip()
                
                if output:
                    last_log = output[-300:]
                    if "本地归档" in output or "文件已移至" in output:
                        return {"status": "success", "message": "Archived", "log_snippet": last_log}
                    if "正在提交任务" in output or "任务添加成功" in output:
                        found_progress = True

                time.sleep(2)
            
            if found_progress:
                return {"status": "downloading", "message": "Downloading", "log_snippet": last_log}
            else:
                return {"status": "timeout", "found": False, "message": "No logs found"}

        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            client.close()

# 实例化 Services
sonarr_service = SonarrService()
radarr_service = RadarrService()
pikpak_service = PikpakService()

# ==========================================
# 4. API 路由 (Routes)
# ==========================================

# --- Sonarr Routes ---
@app.post("/sonarr/search")
def sonarr_search(req: SearchRequest):
    return sonarr_service.search(req.keyword)

@app.post("/sonarr/add")
def sonarr_add(req: SonarrAddRequest):
    return sonarr_service.add_series(req.tvdb_id)

@app.post("/sonarr/download")
def sonarr_download(req: SonarrDownloadRequest):
    return sonarr_service.trigger_download(req.series_id, req.season)

# --- Radarr Routes (NEW) ---
@app.post("/radarr/search")
def radarr_search(req: SearchRequest):
    logger.info(f"[API] Radarr Search: {req.keyword}")
    return radarr_service.search(req.keyword)

@app.post("/radarr/add")
def radarr_add(req: RadarrAddRequest):
    logger.info(f"[API] Radarr Add TMDB: {req.tmdb_id}")
    return radarr_service.add_movie(req.tmdb_id)

@app.post("/radarr/download")
def radarr_download(req: RadarrDownloadRequest):
    logger.info(f"[API] Radarr Download MovieID: {req.movie_id}")
    return radarr_service.trigger_download(req.movie_id)

# --- Shared Verification Route ---
@app.post("/pikpak/verify")
def api_verify_log(req: VerifyLogRequest, background_tasks: BackgroundTasks):
    logger.info(f"[API] Verify | Category: {req.category} | Keyword: {req.keyword}")
    
    # 1. 执行验证
    result = pikpak_service.verify_download_start(req.keyword, req.season, req.timeout_seconds)
    
    # 2. 成功后触发 Rescan
    if result.get("status") == "success":
        media_id = req.media_id
        
        # 如果 ID 缺失，尝试反查
        if not media_id:
            if req.category == "sonarr":
                media_id = sonarr_service.get_series_id_by_title(req.keyword)
            else:
                media_id = radarr_service.get_movie_id_by_title(req.keyword)
        
        # 触发对应的后台扫描
        if media_id:
            logger.info(f"[API] Triggering background rescan for {req.category} ID: {media_id}")
            if req.category == "sonarr":
                background_tasks.add_task(sonarr_service.rescan_series, media_id)
            else:
                background_tasks.add_task(radarr_service.rescan_movie, media_id)

    return result

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)