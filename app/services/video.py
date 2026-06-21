import glob
import itertools
import os
import random
import gc
import shutil
from typing import List
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)
from moviepy.audio import fx as afx
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import utils

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
video_codec = "libx264"
fps = 30

def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]
        
    for file in files:
        try:
            os.remove(file)
        except:
            pass

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file and os.path.exists(bgm_file):
        return bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        return random.choice(files)

    return ""


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str = None,
    audio_duration: float = 0.0,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
) -> str:
    if audio_duration <= 0 and audio_file:
        try:
            audio_clip = AudioFileClip(audio_file)
            audio_duration = audio_clip.duration
            audio_clip.close()
        except Exception as e:
            logger.error(f"Failed to read audio duration: {str(e)}")
            audio_duration = 60.0
    elif audio_duration <= 0:
        audio_duration = 60.0
    logger.info(f"audio duration: {audio_duration} seconds")
    # Required duration of each clip
    req_dur = audio_duration / len(video_paths)
    req_dur = max_clip_duration
    logger.info(f"maximum clip duration: {req_dur} seconds")
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    
    logger.info(f"[Video Processing] Total downloaded videos: {len(video_paths)}")
    
    for video_path in video_paths:
        try:
            clip = VideoFileClip(video_path)
            clip_duration = clip.duration
            clip_w, clip_h = clip.size
            close_clip(clip)
            
            # 每个视频只使用一次，取前max_clip_duration秒或整个视频
            end_time = min(max_clip_duration, clip_duration)
            # 允许短视频被使用，只要时长至少有1秒
            if end_time >= 1.0:
                subclipped_items.append(SubClippedVideoClip(file_path=video_path, start_time=0, end_time=end_time, width=clip_w, height=clip_h))
        except Exception as e:
            logger.error(f"Skipping corrupted video file: {video_path} - {str(e)}")
            continue

    # random subclipped_items order
    if video_concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(subclipped_items)
        
    logger.info(f"[Video Processing] Total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration > audio_duration:
            break
        
        logger.debug(f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, current duration: {video_duration:.2f}s, remaining: {audio_duration - video_duration:.2f}s")
        
        try:
            clip = VideoFileClip(subclipped_item.file_path)
            if subclipped_item.start_time > 0 or subclipped_item.end_time < clip.duration:
                clip = clip.with_section_cut_out(0, subclipped_item.start_time)
                remaining_duration = clip.duration
                if remaining_duration > (subclipped_item.end_time - subclipped_item.start_time):
                    clip = clip.with_section_cut_out(subclipped_item.end_time - subclipped_item.start_time, remaining_duration)
            clip_duration = clip.duration
            # Not all videos are same size, so we need to resize them
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, target: {video_width}x{video_height}, ratio: {video_ratio:.2f}")
                
                if clip_ratio == video_ratio:
                    clip = clip.resized(new_size=(video_width, video_height))
                else:
                    if clip_ratio > video_ratio:
                        scale_factor = video_width / clip_w
                    else:
                        scale_factor = video_height / clip_h

                    new_width = int(clip_w * scale_factor)
                    new_height = int(clip_h * scale_factor)

                    background = ColorClip(size=(video_width, video_height), color=(0, 0, 0))
                    background = background.with_duration(clip_duration)
                    clip_resized = clip.resized(new_size=(new_width, new_height)).with_position("center")
                    clip = CompositeVideoClip([background, clip_resized])
                    
                    close_clip(clip_resized)
                    close_clip(background)
                    
            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            if video_transition_mode.value == VideoTransitionMode.none.value:
                clip = clip
            elif video_transition_mode.value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, 1)
            elif video_transition_mode.value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, 1)
            elif video_transition_mode.value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, 1, shuffle_side)
            elif video_transition_mode.value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, 1, shuffle_side)
            elif video_transition_mode.value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, 1),
                    lambda c: video_effects.fadeout_transition(c, 1),
                    lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if clip.duration > max_clip_duration:
                clip = clip.with_section_cut_out(max_clip_duration, clip.duration)
                
            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            clip.write_videofile(clip_file, logger=None, fps=fps, codec=video_codec)
            
            close_clip(clip)
        
            processed_clips.append(SubClippedVideoClip(file_path=clip_file, duration=clip.duration, width=clip_w, height=clip_h))
            video_duration += clip.duration
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
    
    # 如果视频时长小于音频时长，记录警告但不循环使用片段
    if video_duration < audio_duration:
        logger.warning(f"video duration ({video_duration:.2f}s) is shorter than audio duration ({audio_duration:.2f}s), using available clips only without looping.")
        logger.info(f"final video duration: {video_duration:.2f}s")
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        # 创建一个默认的黑色背景视频
        logger.info("creating default black background video")
        black_clip = ColorClip(size=(video_width, video_height), color=(0, 0, 0))
        black_clip = black_clip.with_duration(audio_duration)
        black_clip.write_videofile(
            combined_video_path,
            threads=threads,
            logger=None,
            fps=fps,
            codec=video_codec,
            audio=False,
        )
        close_clip(black_clip)
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files(processed_clips)
        logger.info("video combining completed")
        return combined_video_path
    
    # create initial video file as base
    base_clip_path = processed_clips[0].file_path
    temp_merged_video = f"{output_dir}/temp-merged-video.mp4"
    temp_merged_next = f"{output_dir}/temp-merged-next.mp4"
    
    # copy first clip as initial merged video
    shutil.copy(base_clip_path, temp_merged_video)
    
    # load audio from original audio file (to be added later in generate_video)
    original_audio = AudioFileClip(audio_file)
    
    # merge remaining video clips one by one
    for i, clip_item in enumerate(processed_clips[1:], 1):
        logger.info(f"merging clip {i}/{len(processed_clips)-1}, duration: {clip_item.duration:.2f}s")
        
        try:
            # load current base video and next clip to merge
            base_clip = VideoFileClip(temp_merged_video)
            next_clip = VideoFileClip(clip_item.file_path)
            
            # merge these two clips WITHOUT audio (video only)
            merged_clip = concatenate_videoclips([base_clip, next_clip], method="compose")

            # save merged result to temp file (video only, no audio)
            merged_clip.write_videofile(
                filename=temp_merged_next,
                threads=threads,
                logger=None,
                fps=fps,
                codec=video_codec,
                audio=False,  # Don't write audio in combine step
            )
            close_clip(base_clip)
            close_clip(next_clip)
            close_clip(merged_clip)
            
            # replace base file with new merged file
            delete_files(temp_merged_video)
            os.rename(temp_merged_next, temp_merged_video)
            
        except Exception as e:
            logger.error(f"failed to merge clip: {str(e)}")
            continue
    
    # close the original audio clip
    original_audio.close()
    
    # after merging, rename final result to target file name
    os.rename(temp_merged_video, combined_video_path)
    
    # clean temp files
    clip_files = [clip.file_path for clip in processed_clips]
    delete_files(clip_files)
    
    # clean up temp audio files
    temp_audio_files = [f for f in os.listdir(output_dir) if 'TEMP_MPY' in f]
    for f in temp_audio_files:
        try:
            os.remove(os.path.join(output_dir, f))
        except:
            pass
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    video_clip = VideoFileClip(video_path)
    video_clip.audio = None
    audio_clip = AudioFileClip(audio_path)
    audio_clip = audio_clip.with_volume_scaled(params.voice_volume)

    if subtitle_path and os.path.exists(subtitle_path) and params.subtitle_enabled:
        logger.info(f"loading subtitles from: {subtitle_path}")
        
        def create_text_clip(subtitle_item):
            params.font_size = int(params.font_size)
            params.stroke_width = int(params.stroke_width)
            
            # Handle both tuple format ((start, end), text) and simple string format for testing
            if isinstance(subtitle_item, tuple):
                phrase = subtitle_item[1]
            else:
                phrase = str(subtitle_item)
                
            max_width = video_width * 0.9
            wrapped_txt, txt_height = wrap_text(
                phrase, max_width=max_width, font=font_path, fontsize=params.font_size
            )
            interline = int(params.font_size * 0.25)
            size=(int(max_width), int(txt_height + params.font_size * 0.25 + (interline * (wrapped_txt.count("\n") + 1))))

            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=params.text_background_color,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
            )
            
            # Only set timing for actual subtitle items
            if isinstance(subtitle_item, tuple):
                duration = subtitle_item[0][1] - subtitle_item[0][0]
                _clip = _clip.with_start(subtitle_item[0][0])
                _clip = _clip.with_end(subtitle_item[0][1])
                _clip = _clip.with_duration(duration)
                
            if params.subtitle_position == "bottom":
                _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
            elif params.subtitle_position == "top":
                _clip = _clip.with_position(("center", video_height * 0.05))
            elif params.subtitle_position == "custom":
                margin = 10
                max_y = video_height - _clip.h - margin
                min_y = margin
                custom_y = (video_height - _clip.h) * (params.custom_position / 100)
                custom_y = max(min_y, min(custom_y, max_y))
                _clip = _clip.with_position(("center", custom_y))
            else:
                _clip = _clip.with_position(("center", "center"))
            return _clip

        sub = SubtitlesClip(subtitles=subtitle_path, encoding="utf-8", make_textclip=create_text_clip)
        video_clip = CompositeVideoClip([video_clip, sub])

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file)
            bgm_clip = bgm_clip.with_effects([
                afx.MultiplyVolume(params.bgm_volume),
                afx.AudioFadeOut(3),
                afx.AudioLoop(duration=video_clip.duration)
            ])
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip.audio = audio_clip
    video_clip.write_videofile(
        output_file,
        audio_codec=audio_codec,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 2,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    for material in materials:
        if not material.url:
            continue

        ext = utils.parse_extension(material.url)
        try:
            clip = VideoFileClip(material.url)
        except Exception:
            clip = ImageClip(material.url)

        width = clip.size[0]
        height = clip.size[1]
        if width < 480 or height < 480:
            logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
            continue

        if ext in const.FILE_TYPE_IMAGES:
            logger.info(f"processing image: {material.url}")
            # Create an image clip and set its duration to 3 seconds
            clip = (
                ImageClip(material.url)
                .with_duration(clip_duration)
                .with_position("center")
            )
            # Apply a zoom effect using the resize method.
            # A lambda function is used to make the zoom effect dynamic over time.
            # The zoom effect starts from the original size and gradually scales up to 120%.
            # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
            # Note: 1 represents 100% size, so 1.2 represents 120% size.
            zoom_clip = clip.resized(
                lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
            )

            # Optionally, create a composite video clip containing the zoomed clip.
            # This is useful when you want to add other elements to the video.
            final_clip = CompositeVideoClip([zoom_clip])

            # Output the video to a file.
            video_file = f"{material.url}.mp4"
            final_clip.write_videofile(video_file, fps=30, logger=None)
            close_clip(clip)
            material.url = video_file
            logger.success(f"image processed: {video_file}")
    return materials