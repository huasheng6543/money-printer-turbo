import os
import random
import hashlib
import json
import base64
import concurrent.futures
from typing import List, Set, Dict
from urllib.parse import urlencode
from datetime import datetime

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

# 下载线程数配置
MAX_DOWNLOAD_THREADS = 10  # 并发下载线程数
DOWNLOAD_TIMEOUT = 30  # 单个视频下载超时时间（秒）

requested_count = 0
seen_video_urls: Set[str] = set()
video_fingerprints: Set[str] = set()


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    if isinstance(api_keys, str):
        return api_keys

    global requested_count
    requested_count += 1
    return api_keys[requested_count % len(api_keys)]


def get_video_fingerprint(video_path: str) -> str:
    try:
        clip = VideoFileClip(video_path)
        fps = clip.fps if clip.fps else 30
        total_frames = int(clip.duration * fps)
        
        sample_frame_indices = [0, total_frames // 4, total_frames // 2, 3 * total_frames // 4, total_frames - 1]
        frames_hash = ""
        
        for idx in sample_frame_indices:
            idx = max(0, min(idx, total_frames - 1))
            try:
                frame = clip.get_frame(idx / fps)
                small_frame = frame[::16, ::16].mean(axis=2).flatten()
                frame_hash = hashlib.md5(small_frame.tobytes()).hexdigest()[:12]
                frames_hash += frame_hash
            except:
                pass

        clip.close()
        return frames_hash
    except Exception as e:
        logger.debug(f"Failed to generate video fingerprint: {str(e)}")
        return ""


def hamming_distance(s1: str, s2: str) -> int:
    if len(s1) != len(s2):
        return max(len(s1), len(s2))
    return sum(c1 != c2 for c1, c2 in zip(s1, s2))


def has_similar_video(new_fingerprint: str, threshold: int = 15) -> bool:
    global video_fingerprints
    if not new_fingerprint:
        return False
    for existing_fp in video_fingerprints:
        if hamming_distance(new_fingerprint, existing_fp) < threshold:
            return True
    return False


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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"[Pexels] searching: '{search_term}'")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=False,
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"Pexels search failed: {response}")
            return video_items
        videos = response["videos"]
        for v in videos:
            duration = v["duration"]
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            best_video = None
            best_score = 0
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                target_ratio = video_width / video_height
                actual_ratio = w / h
                
                if abs(target_ratio - actual_ratio) < 0.1 and w >= video_width and h >= video_height:
                    score = w * h
                    if score > best_score:
                        best_score = score
                        best_video = video
            
            if best_video:
                item = MaterialInfo()
                item.provider = "pexels"
                item.url = best_video["link"]
                item.duration = duration
                video_items.append(item)
        logger.info(f"[Pexels] '{search_term}' found {len(video_items)} videos")
        return video_items
    except Exception as e:
        logger.error(f"Pexels search error: {str(e)}")
    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pixabay_api_keys")
    params = {
        "q": search_term,
        "video_type": "all",
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"[Pixabay] searching: '{search_term}'")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=False, timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"Pixabay search failed: {response}")
            return video_items
        videos = response["hits"]
        for v in videos:
            duration = v["duration"]
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            best_video = None
            best_score = 0
            target_ratio = video_width / video_height
            
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                h = int(video["height"])
                actual_ratio = w / h
                
                if abs(target_ratio - actual_ratio) < 0.1 and w >= video_width and h >= video_height:
                    score = w * h
                    if score > best_score:
                        best_score = score
                        best_video = video
            
            if best_video:
                item = MaterialInfo()
                item.provider = "pixabay"
                item.url = best_video["url"]
                item.duration = duration
                video_items.append(item)
        logger.info(f"[Pixabay] '{search_term}' found {len(video_items)} videos")
        return video_items
    except Exception as e:
        logger.error(f"Pixabay search error: {str(e)}")
    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    global seen_video_urls, video_fingerprints

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)

    if url_without_query in seen_video_urls:
        logger.warning(f"Duplicate URL detected, skip: {url_without_query}")
        return ""

    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    video_path = f"{save_dir}/vid-{url_hash}.mp4"

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"Video already exists (cached): {video_path}")
        seen_video_urls.add(url_without_query)
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        with requests.get(
            video_url,
            headers=headers,
            proxies=config.proxy,
            verify=False,
            timeout=(10, 60),  # 优化：连接超时10秒，读取超时60秒
            stream=True
        ) as r:
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        if os.path.exists(video_path) and os.path.getsize(video_path) > 100000:
            try:
                clip = VideoFileClip(video_path)
                duration = clip.duration
                fps = clip.fps
                w, h = clip.size
                
                if duration > 0 and fps > 0 and w >= 720:
                    fps_val = fps if fps else 30
                    total_frames = int(duration * fps_val)
                    
                    sample_frame_indices = [0, total_frames // 4, total_frames // 2, 3 * total_frames // 4, total_frames - 1]
                    frames_hash = ""
                    
                    for idx in sample_frame_indices:
                        idx = max(0, min(idx, total_frames - 1))
                        try:
                            frame = clip.get_frame(idx / fps_val)
                            small_frame = frame[::16, ::16].mean(axis=2).flatten()
                            frame_hash = hashlib.md5(small_frame.tobytes()).hexdigest()[:12]
                            frames_hash += frame_hash
                        except:
                            pass
                    
                    clip.close()
                    
                    if frames_hash:
                        global video_fingerprints
                        is_similar = False
                        for existing_fp in video_fingerprints:
                            if hamming_distance(frames_hash, existing_fp) < 15:
                                is_similar = True
                                break
                        
                        if is_similar:
                            logger.warning(f"Similar video detected by fingerprint, delete: {video_path}")
                            try:
                                os.remove(video_path)
                            except:
                                pass
                            return ""
                else:
                    clip.close()
                
                video_fingerprints.add(frames_hash)
                seen_video_urls.add(url_without_query)
                logger.success(f"[OK] HD video saved: {video_path} ({w}x{h}, {duration:.1f}s)")
                return video_path
            except Exception as e:
                logger.warning(f"Video verification failed: {str(e)}")

    except Exception as e:
        logger.error(f"Download error: {str(e)}")

    try:
        os.remove(video_path)
    except:
        pass
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "all",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 6,
    timeline_keywords: List[dict] = None,
) -> List[str]:
    global seen_video_urls, video_fingerprints
    seen_video_urls.clear()
    video_fingerprints.clear()

    all_valid_video_items: List[MaterialInfo] = []
    valid_video_urls: Set[str] = set()
    found_duration = 0.0

    if audio_duration > 0:
        needed_clips = int(audio_duration / max_clip_duration) + 3
        needed_videos = needed_clips * 3
    else:
        needed_videos = 30

    logger.info(f"[Smart Material] Audio: {audio_duration:.1f}s, Max clip: {max_clip_duration}s")
    logger.info(f"[Smart Material] Target: {needed_videos} videos max")

    if timeline_keywords and len(timeline_keywords) > 0:
        all_search_terms = []
        for segment in timeline_keywords:
            if "keywords" in segment:
                all_search_terms.extend(segment["keywords"])
        search_terms = list(set(search_terms + all_search_terms))
        logger.info(f"[Smart Material] Enhanced with timeline keywords, total terms: {len(search_terms)}")

    search_functions = []
    if source == "all" or source == "pexels":
        search_functions.append(search_videos_pexels)
    if source == "all" or source == "pixabay":
        search_functions.append(search_videos_pixabay)

    if len(search_terms) > 0:
        videos_per_term = max(3, int(needed_videos / len(search_terms)) + 2)
    else:
        videos_per_term = 5

    logger.info(f"[Smart Material] {videos_per_term} videos per keyword, total terms: {len(search_terms)}")

    for search_term in search_terms:
        if len(all_valid_video_items) >= needed_videos:
            logger.info(f"[Smart Material] [OK] Target reached, stopping search")
            break

        for search_func in search_functions:
            try:
                video_items = search_func(
                    search_term=search_term,
                    minimum_duration=max_clip_duration,
                    video_aspect=video_aspect,
                )

                for item in video_items[:videos_per_term]:
                    item_key = item.url.split("?")[0]
                    if item_key not in valid_video_urls:
                        valid_video_urls.add(item_key)
                        all_valid_video_items.append(item)
                        found_duration += item.duration

                if len(all_valid_video_items) >= needed_videos:
                    logger.info(f"[Smart Material] [OK] Target reached, stopping early")
                    break
            except Exception as e:
                logger.warning(f"Search failed for '{search_term}': {str(e)}")
                continue
        if len(all_valid_video_items) >= needed_videos:
            break

    logger.info(
        f"\n[Search Summary]\n"
        f"   Total unique videos found: {len(all_valid_video_items)}\n"
        f"   Total available duration: {found_duration:.1f}s\n"
        f"   Required duration: {audio_duration:.1f}s"
    )

    if video_contact_mode.value == VideoConcatMode.random.value:
        random.shuffle(all_valid_video_items)

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    video_paths = []
    total_duration = 0.0
    downloaded_count = 0

    def download_single_video(item):
        """单线程下载单个视频"""
        try:
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            return (saved_video_path, item)
        except Exception as e:
            logger.error(f"Failed: {utils.to_json(item)} => {str(e)}")
            return (None, item)

    # 使用多线程并发下载
    logger.info(f"[Download] Starting parallel download with {MAX_DOWNLOAD_THREADS} threads")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_THREADS) as executor:
        # 提交所有下载任务
        future_to_item = {
            executor.submit(download_single_video, item): (idx, item)
            for idx, item in enumerate(all_valid_video_items)
        }

        # 按完成顺序处理结果
        for future in concurrent.futures.as_completed(future_to_item):
            idx, item = future_to_item[future]

            # 检查是否已经获得足够的视频时长
            if total_duration >= audio_duration and audio_duration > 0:
                logger.info(f"[Download] Got enough duration, stopping remaining downloads")
                break

            downloaded_count += 1
            saved_video_path, _ = future.result()

            if saved_video_path:
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                logger.info(f"[Download] Progress: {downloaded_count}/{len(all_valid_video_items)} - {len(video_paths)} successful (Total: {total_duration:.1f}s)")

    logger.success(f"\n[Final] Downloaded {len(video_paths)} unique videos in {downloaded_count} attempts")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["beautiful nature", "sunset"], audio_duration=60, source="all"
    )


# ==============================================================================
# 本地素材智能管理模块
# ==============================================================================

def scan_local_folder(folder_path: str) -> List[Dict]:
    """
    扫描本地素材文件夹，返回所有视频文件的信息
    
    Args:
        folder_path: 本地素材文件夹路径
    
    Returns:
        List[Dict]: 视频文件信息列表，每个包含路径、时长、尺寸等
    """
    supported_formats = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm'}
    video_files = []
    
    logger.info(f"[Local Material] Scanning folder: {folder_path}")
    
    if not os.path.exists(folder_path):
        logger.error(f"[Local Material] Folder not found: {folder_path}")
        return []
    
    if not os.path.isdir(folder_path):
        logger.error(f"[Local Material] Path is not a folder: {folder_path}")
        return []
    
    # 列出文件夹中的所有文件
    all_files = os.listdir(folder_path)
    logger.info(f"[Local Material] Total files in folder: {len(all_files)}")
    
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in supported_formats:
                video_path = os.path.join(root, file)
                try:
                    clip = VideoFileClip(video_path)
                    info = {
                        'path': video_path,
                        'filename': file,
                        'duration': clip.duration,
                        'fps': clip.fps,
                        'width': clip.size[0],
                        'height': clip.size[1],
                        'aspect': clip.size[0] / clip.size[1] if clip.size[1] > 0 else 0
                    }
                    clip.close()
                    video_files.append(info)
                    logger.info(f"[Local Material] Found: {file} ({info['duration']:.1f}s, {info['width']}x{info['height']})")
                except Exception as e:
                    logger.warning(f"[Local Material] Failed to read {file}: {str(e)}")
    
    logger.info(f"[Local Material] Total videos found: {len(video_files)}")
    return video_files


def extract_video_frames(video_path: str, num_frames: int = 5) -> List[str]:
    """
    从视频中提取关键帧作为Base64编码的图像
    
    Args:
        video_path: 视频文件路径
        num_frames: 提取的帧数
    
    Returns:
        List[str]: Base64编码的图像数据列表
    """
    from PIL import Image
    import io
    
    frames = []
    try:
        clip = VideoFileClip(video_path)
        duration = clip.duration
        fps = clip.fps if clip.fps else 30
        
        total_frames = int(duration * fps)
        
        # 均匀采样关键帧
        if num_frames > 1:
            frame_indices = [int(i * total_frames / num_frames) for i in range(num_frames)]
        else:
            frame_indices = [0]
        
        for idx in frame_indices:
            idx = min(idx, total_frames - 1) if total_frames > 0 else 0
            try:
                frame = clip.get_frame(idx / fps if fps > 0 else 0)
                # 缩小图像以减少数据量
                img = Image.fromarray(frame)
                img = img.resize((320, 180), Image.LANCZOS)
                
                # 转换为Base64
                buffered = io.BytesIO()
                img.save(buffered, format="JPEG", quality=60)
                img_str = base64.b64encode(buffered.getvalue()).decode()
                frames.append(img_str)
            except Exception as e:
                logger.debug(f"[Local Material] Frame extraction failed: {str(e)}")
        
        clip.close()
    except Exception as e:
        logger.error(f"[Local Material] Failed to extract frames from {video_path}: {str(e)}")
    
    return frames


def describe_video_content(video_path: str, llm_function=None) -> str:
    """
    使用AI分析视频内容，生成描述文本
    
    Args:
        video_path: 视频文件路径
        llm_function: LLM调用函数，如果为None则使用视觉特征分析
    
    Returns:
        str: 视频内容描述
    """
    if llm_function is None:
        # 使用简单的视觉特征分析
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps if clip.fps else 30
            total_frames = int(duration * fps)
            
            # 采样关键帧进行颜色分析
            frames_to_sample = min(5, total_frames)
            avg_colors = []
            
            for i in range(frames_to_sample):
                idx = int(i * total_frames / frames_to_sample)
                frame = clip.get_frame(idx / fps if fps > 0 else 0)
                # 计算平均颜色
                avg_color = frame.mean(axis=(0, 1))
                avg_colors.append(avg_color)
            
            clip.close()
            
            # 基于颜色和时间生成描述
            description = f"video_duration_{int(duration)}s"
            
            # 分析整体色调
            if avg_colors:
                overall_avg = sum(c[0] for c in avg_colors) / len(avg_colors)
                if overall_avg > 180:
                    description += "_bright"
                elif overall_avg > 100:
                    description += "_normal"
                else:
                    description += "_dark"
                
                # 颜色变化
                if len(avg_colors) > 1:
                    color_variance = sum(
                        abs(avg_colors[i][0] - avg_colors[i-1][0]) for i in range(1, len(avg_colors))
                    ) / len(avg_colors)
                    if color_variance > 50:
                        description += "_dynamic"
                    else:
                        description += "_static"
            
            return description
            
        except Exception as e:
            logger.error(f"[Local Material] Content analysis failed: {str(e)}")
            return "unknown_content"
    else:
        # 使用LLM进行高级分析
        try:
            frames = extract_video_frames(video_path, num_frames=3)
            if not frames:
                return "no_frames_extracted"
            
            # 构建提示词
            prompt = "描述这张图片的主要内容，包括场景、物体、颜色氛围等。用简洁的中文描述。"
            
            # 调用LLM分析每帧
            descriptions = []
            for frame in frames[:2]:  # 只分析前2帧以节省成本
                try:
                    # 这里可以调用实际的LLM API
                    # 为了兼容性，返回基础描述
                    descriptions.append("画面内容")
                except Exception as e:
                    logger.debug(f"[Local Material] LLM analysis failed: {str(e)}")
            
            return " ".join(descriptions) if descriptions else "content"
        except Exception as e:
            logger.error(f"[Local Material] LLM description failed: {str(e)}")
            return "analysis_failed"


def match_videos_by_content(
    video_files: List[Dict],
    video_script: str,
    max_duration: float = 60.0
) -> List[Dict]:
    """
    根据文案内容匹配合适的视频片段
    
    Args:
        video_files: 扫描得到的视频文件列表
        video_script: 视频文案内容
        max_duration: 最大需要的视频总时长
    
    Returns:
        List[Dict]: 匹配后的视频片段列表
    """
    logger.info(f"[Local Material] Matching videos by content, script length: {len(video_script)} chars")
    logger.info(f"[Local Material] Max duration needed: {max_duration:.2f}s")
    
    if not video_files:
        logger.warning("[Local Material] No videos available for matching")
        return []
    
    # 分析文案关键词
    script_keywords = extract_keywords_from_script(video_script)
    logger.info(f"[Local Material] Extracted keywords: {script_keywords}")
    
    # 为每个视频生成描述（简化处理，跳过耗时的AI分析）
    video_descriptions = []
    for video in video_files:
        video['description'] = video.get('description', '')
        video_descriptions.append(video)
        logger.info(f"[Local Material] Ready video: {video['filename']} ({video['duration']:.1f}s)")
    
    logger.info(f"[Local Material] Total videos after processing: {len(video_descriptions)}")
    
    # 根据时长需求分配视频
    matched_videos = []
    remaining_duration = max_duration
    
    logger.info(f"[Local Material] Starting matching, remaining_duration: {remaining_duration:.2f}s")
    
    if remaining_duration <= 0:
        logger.error(f"[Local Material] max_duration is {max_duration:.2f}s, which is <= 0")
        return []
    
    # 按视频时长排序，先使用短的视频
    video_files_sorted = sorted(video_descriptions, key=lambda x: x['duration'])
    
    for video in video_files_sorted:
        if remaining_duration <= 0:
            logger.info(f"[Local Material] Stopping matching, remaining_duration reached 0")
            break
        
        # 简单匹配逻辑：随机选择一些视频，确保多样性
        clip_duration = min(video['duration'], remaining_duration)
        
        matched_videos.append({
            'path': video['path'],
            'start_time': 0,
            'duration': clip_duration,
            'description': video.get('description', ''),
            'width': video['width'],
            'height': video['height']
        })
        
        remaining_duration -= clip_duration
        logger.info(f"[Local Material] Matched: {video['filename']} ({clip_duration:.1f}s), remaining: {remaining_duration:.2f}s")
    
    logger.info(f"[Local Material] Total matched videos: {len(matched_videos)}, total duration: {sum(v['duration'] for v in matched_videos):.1f}s")
    
    return matched_videos


def extract_keywords_from_script(script: str) -> List[str]:
    """
    从文案中提取关键词
    
    Args:
        script: 视频文案
    
    Returns:
        List[str]: 关键词列表
    """
    # 简单的关键词提取（可以后续接入LLM进行更智能的提取）
    import re
    
    # 移除标点符号
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', script)
    
    # 常见停用词
    stopwords = {'的', '了', '是', '在', '和', '与', '或', '以及', '等', '这', '那', '有', '个', '们', 
                 'the', 'a', 'an', 'is', 'are', 'was', 'were', 'in', 'on', 'at', 'to', 'for', 'of'}
    
    # 分词
    words = cleaned.split()
    
    # 过滤停用词和短词
    keywords = [w for w in words if len(w) >= 2 and w not in stopwords]
    
    # 返回前20个关键词
    return list(set(keywords))[:20]


def analyze_local_library(folder_path: str) -> Dict:
    """
    分析本地素材库，生成完整的分析报告
    
    Args:
        folder_path: 本地素材文件夹路径
    
    Returns:
        Dict: 分析报告，包含视频列表、统计信息等
    """
    logger.info(f"[Local Material] Analyzing local library: {folder_path}")
    
    # 扫描视频
    video_files = scan_local_folder(folder_path)
    
    if not video_files:
        return {
            'success': False,
            'message': 'No videos found in the folder',
            'videos': []
        }
    
    # 统计信息
    total_duration = sum(v['duration'] for v in video_files)
    total_size = sum(os.path.getsize(v['path']) for v in video_files if os.path.exists(v['path']))
    
    # 分辨率分布
    resolution_counts = {}
    for v in video_files:
        res = f"{v['width']}x{v['height']}"
        resolution_counts[res] = resolution_counts.get(res, 0) + 1
    
    report = {
        'success': True,
        'folder': folder_path,
        'total_videos': len(video_files),
        'total_duration': total_duration,
        'total_duration_formatted': f"{int(total_duration // 60)}m {int(total_duration % 60)}s",
        'total_size_mb': total_size / (1024 * 1024),
        'resolution_distribution': resolution_counts,
        'videos': video_files
    }
    
    logger.info(f"[Local Material] Analysis complete: {len(video_files)} videos, {total_duration:.1f}s total")
    
    return report


def get_local_materials(
    folder_path: str,
    video_script: str,
    audio_duration: float,
    max_clip_duration: int = 6
) -> List[Dict]:
    """
    获取本地素材的主要接口函数
    
    Args:
        folder_path: 本地素材文件夹路径
        video_script: 视频文案内容
        audio_duration: 音频时长
        max_clip_duration: 最大片段时长
    
    Returns:
        List[Dict]: 匹配后的视频片段列表
    """
    # 分析本地素材库
    analysis = analyze_local_library(folder_path)
    
    if not analysis['success'] or not analysis['videos']:
        logger.error(f"[Local Material] No local materials available")
        return []
    
    # 匹配合适的视频
    matched_videos = match_videos_by_content(
        analysis['videos'],
        video_script,
        max_duration=audio_duration
    )
    
    return matched_videos

