import os
import random
import threading
import subprocess
import json
import re
import html
import shutil
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def _find_tiktok_cookies() -> str:
    if os.path.exists("tiktok.txt"):
        return os.path.abspath("tiktok.txt")
    parent_cookies = os.path.join(os.path.dirname(os.getcwd()), "tiktok.txt")
    if os.path.exists(parent_cookies):
        return os.path.abspath(parent_cookies)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    parent_of_root = os.path.dirname(project_root)
    for path in [
        os.path.join(project_root, "tiktok.txt"),
        os.path.join(parent_of_root, "tiktok.txt"),
    ]:
        if os.path.exists(path):
            return os.path.abspath(path)
    return "tiktok.txt"


def _find_aria2c() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))
    venv_aria2c = os.path.join(project_root, ".venv", "Scripts", "aria2c.exe")
    if os.path.exists(venv_aria2c):
        return venv_aria2c
    venv_aria2c_unix = os.path.join(project_root, ".venv", "bin", "aria2c")
    if os.path.exists(venv_aria2c_unix):
        return venv_aria2c_unix
    sys_aria2c = shutil.which("aria2c")
    if sys_aria2c:
        return sys_aria2c
    return "aria2c.exe"


def search_videos_tiktok(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    is_hook: bool = False,
) -> List[MaterialInfo]:
    is_profile = "@" in search_term and "video/" not in search_term
    is_direct_video = search_term.startswith("http") and "tiktok.com" in search_term and not is_profile

    if is_direct_video:
        item = MaterialInfo()
        item.provider = "douyin"
        item.url = search_term
        item.duration = 60
        return [item]

    profile_url = ""
    if is_profile:
        profile_url = search_term
        if not profile_url.startswith("http"):
            if not profile_url.startswith("@"):
                profile_url = "@" + profile_url
            profile_url = f"https://www.tiktok.com/{profile_url}"
    else:
        # Load themes configuration
        tiktok_config = config.app.get("tiktok", {})
        selected_theme = tiktok_config.get("selected_theme", "default")
        themes = tiktok_config.get("themes", {})
        theme_data = themes.get(selected_theme, {})
        
        if is_hook:
            accounts = theme_data.get("hooks") or tiktok_config.get("default_hooks") or config.app.get("tiktok_accounts") or ["@zachking"]
            logger.info(f"TikTok sourcing: Using Hook channels from theme '{selected_theme}'")
        else:
            accounts = theme_data.get("bodies") or tiktok_config.get("default_bodies") or config.app.get("tiktok_accounts") or ["@zachking"]
            logger.info(f"TikTok sourcing: Using Body channels from theme '{selected_theme}'")
            
        if not isinstance(accounts, list) or not accounts:
            accounts = ["@zachking"]
            
        account = random.choice(accounts)
        if not account.startswith("@"):
            account = "@" + account
        profile_url = f"https://www.tiktok.com/{account}"

    # Try searching with cookies first (if they exist)
    cookies_path = _find_tiktok_cookies()
    cmd_with_cookies = [
        "yt-dlp",
        "--cookies", cookies_path,
        "--flat-playlist",
        "--dump-single-json",
        profile_url,
        "--playlist-end", "5"
    ]
    
    cmd_no_cookies = [
        "yt-dlp",
        "--impersonate", "chrome",
        "--flat-playlist",
        "--dump-single-json",
        profile_url,
        "--playlist-end", "5"
    ]

    result_json = None
    
    # Try with cookies
    if os.path.exists(cookies_path):
        logger.info(f"Running yt-dlp to search TikTok videos with cookies: {' '.join(cmd_with_cookies)}")
        try:
            res = subprocess.run(cmd_with_cookies, capture_output=True, text=True, check=True, encoding="utf-8")
            result_json = res.stdout
        except Exception as e:
            logger.warning(f"Failed to search TikTok videos using cookies, falling back to no-cookies: {str(e)}")

    # Fallback to no cookies (with Chrome impersonation)
    if not result_json:
        logger.info(f"Running yt-dlp to search TikTok videos without cookies: {' '.join(cmd_no_cookies)}")
        try:
            res = subprocess.run(cmd_no_cookies, capture_output=True, text=True, check=True, encoding="utf-8")
            result_json = res.stdout
        except Exception as e:
            logger.error(f"Failed to search TikTok videos without cookies: {str(e)}")
            return []

    try:
        data = json.loads(result_json)
        video_items = []
        entries = data.get("entries", [])
        for entry in entries:
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            if not entry_url and entry.get("id"):
                entry_url = f"https://www.tiktok.com/@placeholder/video/{entry['id']}"
            if not entry_url:
                continue
            
            duration = entry.get("duration")
            if duration is None:
                duration = 60
            try:
                duration = int(float(duration))
            except (TypeError, ValueError):
                duration = 60

            if duration < minimum_duration:
                continue

            item = MaterialInfo()
            item.provider = "douyin"
            item.url = entry_url
            item.duration = duration
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"Failed to parse TikTok videos JSON: {str(e)}")
        return []


def search_videos_youtube(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    video_items = []
    suffixes = ["clip", "scene", "edit", "fight", "moment"]
    for suffix in suffixes:
        item = MaterialInfo()
        item.provider = "youtube"
        item.url = f"ytsearch1:{search_term} {suffix}"
        item.duration = 300
        video_items.append(item)
    return video_items


# AnimeTosho search removed


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - 鉴权: Authorization: Bearer <api_key>
      - 搜索端点: GET /videos?query=...,响应结构 {"hits": [...], ...}
      - 加 ?urls=true 在搜索响应里直接返回 mp4 直链
      - URL 是 signed JWT(绑定 API key,无过期时间)
      - Coverr 库以 16:9 横屏为主,9:16 portrait 占比极低(约 1%)
        因此本函数不做 aspect_ratio 过滤,由下游 video.py 的
        resize + letterbox 逻辑统一处理
      - duration 字段同时存在 number 和 string 两种形态,本函数都接受

    本函数使用 urls.mp4_download 字段作为下载地址 —— 按 Coverr 官方文档
    (https://api.coverr.co/docs/videos/#download-a-video) 的说法,
    GET 这个 URL 本身就被 Coverr 当作一次合法的 download 事件计入统计,
    无需再调用 PATCH /videos/:id/stats/downloads。
    """
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items

        for v in response["hits"]:
            # duration 在不同响应里可能是 number(11.625) 或 string("10.500000")
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def _is_valid_video_file(video_path: str) -> bool:
    if not os.path.exists(video_path) or os.path.getsize(video_path) <= 0:
        return False
    clip = None
    try:
        clip = VideoFileClip(video_path)
        duration = clip.duration
        fps = clip.fps
        if duration > 0 and fps > 0:
            return True
    except Exception as e:
        logger.warning(f"invalid video file: {video_path} => {str(e)}")
        try:
            os.remove(video_path)
        except Exception as remove_error:
            logger.warning(
                f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
            )
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception as close_error:
                logger.warning(
                    f"failed to close video clip: {video_path}, error: {str(close_error)}"
                )
    return False


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    if video_url.startswith("magnet:") or ".torrent" in video_url:
        url_hash = utils.md5(video_url)
    else:
        url_without_query = video_url.split("?")[0]
        url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        if _is_valid_video_file(video_path):
            logger.info(f"video already exists: {video_path}")
            return video_path
        else:
            logger.info(f"cached video is invalid or corrupted, removed: {video_path}")

    # TikTok download logic
    if "tiktok.com" in video_url:
        cookies_path = _find_tiktok_cookies()
        cmd_with_cookies = [
            "yt-dlp",
            "--cookies", cookies_path,
            "--sleep-interval", "5",
            "-o", video_path,
            video_url
        ]
        cmd_no_cookies = [
            "yt-dlp",
            "--impersonate", "chrome",
            "--sleep-interval", "5",
            "-o", video_path,
            video_url
        ]
        
        success = False
        if os.path.exists(cookies_path):
            logger.info(f"Running yt-dlp to download TikTok video with cookies: {' '.join(cmd_with_cookies)}")
            try:
                subprocess.run(cmd_with_cookies, check=True, timeout=300)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    logger.info(f"TikTok video saved: {video_path}")
                    success = True
            except Exception as e:
                logger.warning(f"Failed to download TikTok video using cookies, falling back to no-cookies: {str(e)}")

        if not success:
            logger.info(f"Running yt-dlp to download TikTok video without cookies: {' '.join(cmd_no_cookies)}")
            try:
                subprocess.run(cmd_no_cookies, check=True, timeout=300)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    logger.info(f"TikTok video saved without cookies: {video_path}")
                    success = True
            except Exception as e:
                logger.error(f"Failed to download TikTok video without cookies: {str(e)}")

        if not success:
            return ""



    # YouTube download logic
    if video_url.startswith("ytsearch:") or "youtube.com" in video_url or "youtu.be" in video_url:
        video_id_path_no_ext = os.path.join(save_dir, video_id)
        cmd = [
            "yt-dlp",
            "--impersonate", "chrome",
            "--sleep-interval", "3",
            "--merge-output-format", "mp4",
            "-o", f"{video_id_path_no_ext}.%(ext)s",
            video_url
        ]
        logger.info(f"Running yt-dlp to download YouTube video: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, timeout=120)
            
            downloaded_file = None
            for ext in [".mp4", ".mkv", ".webm", ".avi", ".flv"]:
                candidate = f"{video_id_path_no_ext}{ext}"
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    downloaded_file = candidate
                    break
                    
            if downloaded_file:
                if downloaded_file.lower().endswith(".mp4"):
                    pass
                else:
                    ffmpeg_cmd = ["ffmpeg", "-y", "-i", downloaded_file, "-c", "copy", video_path]
                    logger.info(f"Converting YouTube video to MP4: {' '.join(ffmpeg_cmd)}")
                    try:
                        subprocess.run(ffmpeg_cmd, check=True, timeout=180)
                        try:
                            os.remove(downloaded_file)
                        except Exception:
                            pass
                    except Exception as e:
                        logger.error(f"Failed to convert YouTube video to MP4: {str(e)}")
                        return ""
                
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    logger.info(f"YouTube video successfully saved: {video_path}")
                    return video_path
        except Exception as e:
            logger.error(f"Failed to download YouTube video: {str(e)}")
            return ""

    if "tiktok.com" not in video_url and not video_url.startswith("ytsearch:") and "youtube.com" not in video_url and "youtu.be" not in video_url:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }

        # if video does not exist, download it
        with open(video_path, "wb") as f:
            f.write(
                requests.get(
                    video_url,
                    headers=headers,
                    proxies=config.proxy,
                    verify=_get_tls_verify(),
                    timeout=(60, 240),
                ).content
            )

    if _is_valid_video_file(video_path):
        return video_path
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
) -> List[str]:
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr
    elif source == "douyin":
        search_videos = search_videos_tiktok

    elif source == "youtube":
        search_videos = search_videos_youtube

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    valid_video_urls = []
    found_duration = 0.0
    hook_items = []
    body_items = []
    for i, search_term in enumerate(search_terms):
        kwargs = {
            "search_term": search_term,
            "minimum_duration": max_clip_duration,
            "video_aspect": video_aspect,
        }
        if source == "douyin":
            kwargs["is_hook"] = (i == 0)
        video_items = search_videos(**kwargs)
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                if source == "douyin" and i == 0:
                    hook_items.append(item)
                else:
                    body_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    valid_video_items = hook_items + body_items
    logger.info(
        f"found total videos: {len(valid_video_items)} (hooks: {len(hook_items)}, bodies: {len(body_items)}), required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(hook_items)
        random.shuffle(body_items)
        valid_video_items = hook_items + body_items

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    按脚本文案顺序下载素材。

    默认下载逻辑会把所有关键词的候选素材合并成一个大列表；如果第一个
    关键词返回很多结果，最终下载时可能一直消耗这个关键词的素材，后续
    脚本主题就排不上时间线。这里按关键词分组后轮询下载：
    第 1 轮取每个关键词的第 1 个候选，第 2 轮取每个关键词的第 2 个候选。
    这样在不重写视频合成引擎的前提下，尽量保证素材顺序贴近文案顺序。
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for i, search_term in enumerate(search_terms):
        kwargs = {
            "search_term": search_term,
            "minimum_duration": max_clip_duration,
            "video_aspect": video_aspect,
        }
        if search_videos == search_videos_tiktok:
            kwargs["is_hook"] = (i == 0)
        video_items = search_videos(**kwargs)
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    total_duration = 0.0
    candidate_index = 0
    while candidate_groups and total_duration <= audio_duration:
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue

            has_candidate = True
            item = term_items[candidate_index]
            try:
                logger.info(
                    f"downloading ordered video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    f"failed to download ordered video: {utils.to_json(item)} => {str(e)}"
                )

        if not has_candidate:
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
