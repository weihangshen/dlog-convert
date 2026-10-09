#!/usr/bin/env python3
"""Interactive DJI D-Log to Rec.709 batch converter."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

try:
    import questionary
    import questionary.prompts.common as questionary_common
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style
except ImportError:  # A focused error is more useful than a traceback on first run.
    questionary = None
    questionary_common = None
    Keys = None
    Style = None


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".m4v",
    ".avi",
    ".mts",
    ".m2ts",
    ".webm",
}

QUALITY_LEVELS: dict[str, dict[str, Any]] = {
    "ultra_compact": {
        "label": "极限压缩",
        "description": "最小文件，适合临时预览",
        "h265_bpp": 0.020,
        "av1_bpp": 0.014,
        "h265_crf": 34,
        "av1_crf": 48,
    },
    "compact": {
        "label": "极省空间",
        "description": "适合快速分享和长时间记录",
        "h265_bpp": 0.030,
        "av1_bpp": 0.022,
        "h265_crf": 30,
        "av1_crf": 43,
    },
    "economy": {
        "label": "节省空间",
        "description": "适合分享和归档预览",
        "h265_bpp": 0.045,
        "av1_bpp": 0.032,
        "h265_crf": 27,
        "av1_crf": 38,
    },
    "standard": {
        "label": "标准",
        "description": "适合一般播放和日常保存",
        "h265_bpp": 0.060,
        "av1_bpp": 0.043,
        "h265_crf": 24,
        "av1_crf": 34,
    },
    "balanced": {
        "label": "均衡（推荐）",
        "description": "画质、体积和速度的常用平衡点",
        "h265_bpp": 0.075,
        "av1_bpp": 0.055,
        "h265_crf": 22,
        "av1_crf": 30,
    },
    "quality": {
        "label": "优质",
        "description": "适合高质量日常成片",
        "h265_bpp": 0.095,
        "av1_bpp": 0.070,
        "h265_crf": 20,
        "av1_crf": 27,
    },
    "high": {
        "label": "高画质",
        "description": "适合二次剪辑和大屏播放",
        "h265_bpp": 0.120,
        "av1_bpp": 0.090,
        "h265_crf": 18,
        "av1_crf": 24,
    },
    "very_high": {
        "label": "超高画质",
        "description": "优先保留复杂纹理和运动细节",
        "h265_bpp": 0.150,
        "av1_bpp": 0.115,
        "h265_crf": 16,
        "av1_crf": 21,
    },
    "master": {
        "label": "母版级",
        "description": "优先保留细节，文件较大",
        "h265_bpp": 0.180,
        "av1_bpp": 0.140,
        "h265_crf": 14,
        "av1_crf": 18,
    },
    "archive": {
        "label": "归档级",
        "description": "极高码率，适合重要素材长期保存",
        "h265_bpp": 0.250,
        "av1_bpp": 0.190,
        "h265_crf": 10,
        "av1_crf": 12,
    },
}


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    pixel_format: str
    video_bitrate: int
    size: int
    is_dji: bool
    is_dlog: bool
    detection_reason: str
    device_hint: str
    dji_color_mode: DjiColorMode | None = None


@dataclass(frozen=True)
class LutOption:
    path: Path
    device: str
    profile: str
    look: str


@dataclass(frozen=True)
class EncoderSpec:
    name: str
    label: str
    codec: str
    hardware: bool
    pixel_format: str


@dataclass(frozen=True)
class EncoderAvailability:
    spec: EncoderSpec
    available: bool
    reason: str = ""


ENCODER_CANDIDATES: tuple[EncoderSpec, ...] = (
    EncoderSpec("hevc_videotoolbox", "Apple VideoToolbox / 媒体引擎", "h265", True, "p010le"),
    EncoderSpec("hevc_nvenc", "NVIDIA NVENC", "h265", True, "p010le"),
    EncoderSpec("hevc_qsv", "Intel Quick Sync", "h265", True, "p010le"),
    EncoderSpec("hevc_amf", "AMD AMF", "h265", True, "p010le"),
    EncoderSpec("hevc_mf", "Windows Media Foundation", "h265", True, "p010le"),
    EncoderSpec("libx265", "CPU / x265", "h265", False, "yuv420p10le"),
    EncoderSpec("av1_videotoolbox", "Apple VideoToolbox / 媒体引擎", "av1", True, "p010le"),
    EncoderSpec("av1_nvenc", "NVIDIA NVENC", "av1", True, "p010le"),
    EncoderSpec("av1_qsv", "Intel Quick Sync", "av1", True, "p010le"),
    EncoderSpec("av1_amf", "AMD AMF", "av1", True, "p010le"),
    EncoderSpec("av1_mf", "Windows Media Foundation", "av1", True, "p010le"),
    EncoderSpec("libsvtav1", "CPU / SVT-AV1", "av1", False, "yuv420p10le"),
    EncoderSpec("libaom-av1", "CPU / libaom-AV1", "av1", False, "yuv420p10le"),
)


PROMPT_STYLE = (
    Style.from_dict(
        {
            "qmark": "fg:#00a86b bold",
            "question": "bold",
            "answer": "fg:#00a86b bold",
            "pointer": "fg:#00a86b bold",
            "highlighted": "fg:#00a86b bold",
            "selected": "fg:#00a86b",
            "dlog": "fg:#00c853 bold",
            "dji": "fg:#d9822b bold",
            "muted": "fg:#777777",
        }
    )
    if Style
    else None
)


BACK = object()

if questionary_common is not None:
    questionary_common.INDICATOR_SELECTED = "✓"
    questionary_common.INDICATOR_UNSELECTED = "◯"


def run_capture(command: Sequence[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def fraction_to_float(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _first_video_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if stream.get("disposition", {}).get("attached_pic") == 1:
            continue
        return stream
    return None


DjiColorMode = Literal["dlog", "dlog_m", "normal"]

# djmd video descriptor -> field 2 -> field 3 -> enum field 1.
# Pocket 4 D-Log uses 2; DJI Flip D-Log M uses 19 (0x13).
# An omitted enum in an empty color message is the protobuf default, normal.
DJI_COLOR_MODES: dict[int, DjiColorMode] = {0: "normal", 2: "dlog", 19: "dlog_m"}


def _read_protobuf_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Protobuf varint 被截断")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    raise ValueError("Protobuf varint 过长")


def parse_protobuf_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Parse the wire types used by DJI's djmd Protobuf packets."""
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_protobuf_varint(data, offset)
        field_number, wire_type = key >> 3, key & 0x07
        if field_number == 0:
            raise ValueError("无效的 Protobuf 字段编号")

        if wire_type == 0:
            value, offset = _read_protobuf_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("Protobuf fixed64 字段被截断")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            length, offset = _read_protobuf_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("Protobuf length-delimited 字段被截断")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("Protobuf fixed32 字段被截断")
            value, offset = data[offset:end], end
        else:
            raise ValueError(f"不支持的 Protobuf wire type：{wire_type}")
        fields.append((field_number, wire_type, value))
    return fields


def parse_ffprobe_hex_data(value: str) -> bytes:
    """Turn FFprobe's `-show_data` hex dump back into packet bytes."""
    chunks: list[bytes] = []
    for line in value.splitlines():
        match = re.match(r"^\s*[0-9a-fA-F]+:\s*(.*)$", line)
        if not match:
            continue
        hex_column = re.split(r"\s{2,}", match.group(1), maxsplit=1)[0]
        compact = re.sub(r"\s+", "", hex_column)
        if not compact or len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
            raise ValueError("FFprobe packet 十六进制数据无效")
        chunks.append(bytes.fromhex(compact))
    if not chunks:
        raise ValueError("FFprobe packet 中没有十六进制数据")
    return b"".join(chunks)


def _dji_color_mode_from_djmd_packet(packet: bytes) -> DjiColorMode | None:
    try:
        outer_fields = parse_protobuf_fields(packet)
    except ValueError:
        return None

    for field_number, wire_type, descriptor_data in outer_fields:
        if field_number != 2 or wire_type != 2 or not isinstance(descriptor_data, bytes):
            continue
        try:
            descriptor_fields = parse_protobuf_fields(descriptor_data)
        except ValueError:
            continue

        is_video_descriptor = False
        for number, descriptor_wire, identity_data in descriptor_fields:
            if number != 1 or descriptor_wire != 2 or not isinstance(identity_data, bytes):
                continue
            try:
                identity_fields = parse_protobuf_fields(identity_data)
            except ValueError:
                continue
            is_video_descriptor = any(
                identity_number == 3
                and identity_wire == 2
                and identity_value == b"video"
                for identity_number, identity_wire, identity_value in identity_fields
            )
            if is_video_descriptor:
                break
        if not is_video_descriptor:
            continue

        for number, descriptor_wire, video_data in descriptor_fields:
            if number != 2 or descriptor_wire != 2 or not isinstance(video_data, bytes):
                continue
            try:
                video_fields = parse_protobuf_fields(video_data)
            except ValueError:
                continue
            for video_number, video_wire, color_data in video_fields:
                if video_number != 3 or video_wire != 2 or not isinstance(color_data, bytes):
                    continue
                if not color_data:
                    return "normal"
                try:
                    color_fields = parse_protobuf_fields(color_data)
                except ValueError:
                    return None
                for color_number, color_wire, color_value in color_fields:
                    if color_number == 1 and color_wire == 0 and isinstance(color_value, int):
                        return DJI_COLOR_MODES.get(color_value)
                return None
    return None


def _djmd_stream_index(probe: dict[str, Any]) -> int | None:
    for fallback_index, stream in enumerate(probe.get("streams", [])):
        if str(stream.get("codec_tag_string", "")).lower() != "djmd":
            continue
        try:
            return int(stream.get("index", fallback_index))
        except (TypeError, ValueError):
            return fallback_index
    return None


def probe_dji_color_mode(path: Path, ffprobe: str, stream_index: int | None) -> DjiColorMode | None:
    if stream_index is None:
        return None
    result = run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            str(stream_index),
            "-read_intervals",
            "%+#1",
            "-show_packets",
            "-show_entries",
            "packet=data",
            "-show_data",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        packet_dump = json.loads(result.stdout).get("packets", [])[0].get("data", "")
        packet = parse_ffprobe_hex_data(packet_dump)
    except (AttributeError, IndexError, json.JSONDecodeError, ValueError):
        return None
    return _dji_color_mode_from_djmd_packet(packet)


def video_info_from_probe(
    path: Path,
    probe: dict[str, Any],
    dji_color_mode: DjiColorMode | None = None,
) -> VideoInfo:
    stream = _first_video_stream(probe)
    if stream is None:
        raise ValueError("没有可转换的视频轨道")

    format_info = probe.get("format", {})
    tags = format_info.get("tags", {})
    encoder = str(tags.get("encoder", ""))
    searchable_metadata = json.dumps(
        {
            "format_tags": tags,
            "stream_tags": [item.get("tags", {}) for item in probe.get("streams", [])],
        },
        ensure_ascii=False,
    ).lower()
    codec_tags = {
        str(item.get("codec_tag_string", "")).lower() for item in probe.get("streams", [])
    }
    is_dji = dji_color_mode is not None or (
        "dji" in encoder.lower()
        or path.name.upper().startswith("DJI_")
        or bool(codec_tags.intersection({"djmd", "dbgi"}))
    )
    metadata_log = bool(re.search(r"d[\s_-]?log(?:[\s_-]?m)?", searchable_metadata))
    is_dlog = is_dji and (
        dji_color_mode in {"dlog", "dlog_m"}
        or (dji_color_mode is None and metadata_log)
    )

    if dji_color_mode == "dlog_m":
        reason = "DJI djmd 色彩模式标记为 D-Log M"
    elif dji_color_mode == "dlog":
        reason = "DJI djmd 色彩模式标记为 D-Log"
    elif dji_color_mode == "normal":
        reason = "DJI djmd 色彩模式为普通"
    elif metadata_log:
        reason = "DJI 元数据标记为 D-Log"
    elif is_dji:
        reason = "检测到 DJI 素材，但没有可靠的 D-Log 标记"
    else:
        reason = "未检测到 DJI/D-Log 标记"

    duration = float(stream.get("duration") or format_info.get("duration") or 0)
    fps = fraction_to_float(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    bitrate = int(stream.get("bit_rate") or 0)
    size = int(format_info.get("size") or path.stat().st_size)
    return VideoInfo(
        path=path,
        duration=duration,
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        fps=fps,
        codec=str(stream.get("codec_name") or "unknown"),
        pixel_format=str(stream.get("pix_fmt") or "unknown"),
        video_bitrate=bitrate,
        size=size,
        is_dji=is_dji,
        is_dlog=is_dlog,
        detection_reason=reason,
        device_hint=encoder.strip(),
        dji_color_mode=dji_color_mode,
    )


def probe_video(path: Path, ffprobe: str) -> VideoInfo:
    result = run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise ValueError(detail[-1] if detail else "FFprobe 无法读取")
    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("FFprobe 返回了无效 JSON") from exc
    color_mode = probe_dji_color_mode(path, ffprobe, _djmd_stream_index(probe))
    return video_info_from_probe(path, probe, color_mode)


def discover_paths(directory: Path, recursive: bool, output_dir: Path) -> list[Path]:
    iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
    output_resolved = output_dir.resolve()
    paths = []
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            if output_resolved in path.resolve().parents:
                continue
        except OSError:
            pass
        paths.append(path)
    return sorted(paths, key=lambda item: item.name.casefold())


def sort_videos_for_selection(videos: Iterable[VideoInfo]) -> list[VideoInfo]:
    return sorted(
        videos,
        key=lambda item: (item.path.name.casefold(), str(item.path).casefold()),
    )


def scan_videos(paths: Sequence[Path], ffprobe: str) -> tuple[list[VideoInfo], list[tuple[Path, str]]]:
    videos: list[VideoInfo] = []
    errors: list[tuple[Path, str]] = []
    if not paths:
        return videos, errors

    workers = min(8, max(1, len(paths)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_video, path, ffprobe): path for path in paths}
        completed = 0
        for future in as_completed(futures):
            path = futures[future]
            completed += 1
            print(f"\r正在分析视频 [{completed}/{len(paths)}] {path.name[:54]:54}", end="", flush=True)
            try:
                videos.append(future.result())
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append((path, str(exc)))
    print("\r" + " " * 90 + "\r", end="", flush=True)
    return sort_videos_for_selection(videos), errors


def parse_lut_filename(path: Path) -> LutOption:
    stem = path.stem
    match = re.match(
        r"^(?P<device>.+?)\s+(?P<profile>D[\s_-]?Log(?:\s+M)?)\s+to\s+Rec\.?\s*709"
        r"(?P<look>.*?)\s+V(?:ersion\s*)?\d+(?:\.\d+)*$",
        stem,
        flags=re.IGNORECASE,
    )
    if not match:
        return LutOption(path, stem, "D-Log", "标准")
    look = match.group("look").strip(" _-")
    return LutOption(
        path=path,
        device=re.sub(r"\s+", " ", match.group("device").strip()),
        profile=re.sub(r"\s+", " ", match.group("profile").strip()),
        look=look.title() if look else "标准",
    )


def platform_lut_directory(lut_root: Path) -> tuple[Path, str]:
    system = platform.system()
    preferred = "Mac" if system == "Darwin" else "Win"
    directory = lut_root / preferred
    if directory.is_dir():
        return directory, preferred
    alternatives = [item for item in lut_root.iterdir() if item.is_dir()] if lut_root.is_dir() else []
    if alternatives:
        return alternatives[0], alternatives[0].name
    raise FileNotFoundError(f"找不到系统对应的 LUT 目录：{directory}")


def load_luts(lut_root: Path) -> tuple[list[LutOption], str]:
    directory, platform_name = platform_lut_directory(lut_root)
    luts = [parse_lut_filename(path) for path in sorted(directory.glob("*.cube"))]
    if not luts:
        raise FileNotFoundError(f"{directory} 中没有 .cube LUT")
    return luts, platform_name


def human_duration(seconds: float) -> str:
    if seconds <= 0:
        return "时长未知"
    minutes, second = divmod(round(seconds), 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minute:02d}:{second:02d}"
    return f"{minute}:{second:02d}"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def human_bitrate(bitrate: int) -> str:
    if bitrate <= 0:
        return "码率未知"
    return f"{bitrate / 8_000_000:.2f} MB/s"


def video_choice_title(video: VideoInfo) -> list[tuple[str, str]]:
    if video.is_dlog:
        label = "[D-Log M]" if video.dji_color_mode == "dlog_m" else "[D-Log]"
        prefix = ("class:dlog", f"{label} ")
    elif video.dji_color_mode == "normal":
        prefix = ("class:muted", "[普通]   ")
    elif video.is_dji:
        prefix = ("class:dji", "[DJI?]   ")
    else:
        prefix = ("class:muted", "[普通]   ")
    details = (
        f"{video.path.name}  {video.width}x{video.height} "
        f"{video.fps:.2f}fps  {human_bitrate(video.video_bitrate)}  "
        f"{human_duration(video.duration)}  {human_size(video.size)}"
    )
    return [prefix, ("", details)]


def default_video_selected(video: VideoInfo) -> bool:
    return video.is_dlog


def _ask_with_back(prompt: Any) -> Any:
    @prompt.application.key_bindings.add(Keys.Escape, eager=True)
    def go_back(event: Any) -> None:
        event.app.exit(result=BACK)

    return prompt.ask()


def ask_checkbox(message: str, choices: Sequence[Any]) -> list[Any] | object | None:
    prompt = questionary.checkbox(
        message,
        choices=choices,
        instruction="（↑↓ 移动，空格选中/取消，A 全选/全不选，Enter 确认，Esc 返回）",
        style=PROMPT_STYLE,
    )
    return _ask_with_back(prompt)


def ask_select(message: str, choices: Sequence[Any], default: Any = None) -> Any:
    prompt = questionary.select(
        message,
        choices=choices,
        default=default,
        instruction="（↑↓ 移动，Enter 确认，Esc 返回）",
        style=PROMPT_STYLE,
    )
    return _ask_with_back(prompt)


def select_videos(
    videos: Sequence[VideoInfo],
    current: Sequence[VideoInfo] | None = None,
) -> list[VideoInfo] | object | None:
    current_paths = {str(video.path) for video in current} if current is not None else None
    choices = [
        questionary.Choice(
            title=video_choice_title(video),
            value=str(video.path),
            checked=(
                str(video.path) in current_paths
                if current_paths is not None
                else default_video_selected(video)
            ),
        )
        for video in videos
    ]
    selected = ask_checkbox("选择要批量转换的视频", choices)
    if selected is None or selected is BACK:
        return selected
    selected_set = set(selected)
    return [video for video in videos if str(video.path) in selected_set]


def _normalize_device(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower()).replace("dji", "")


def suggested_device(devices: Sequence[str], videos: Sequence[VideoInfo]) -> str:
    hints = " ".join(video.device_hint for video in videos if video.device_hint)
    if not hints:
        return devices[0]
    normalized_hint = _normalize_device(hints)
    return max(
        devices,
        key=lambda device: SequenceMatcher(None, _normalize_device(device), normalized_hint).ratio(),
    )


def select_lut(
    luts: Sequence[LutOption],
    videos: Sequence[VideoInfo],
    current: LutOption | None = None,
) -> LutOption | object | None:
    devices = sorted({lut.device for lut in luts})
    preferred_device = (
        current.device
        if current is not None and current.device in devices
        else suggested_device(devices, videos)
    )
    while True:
        device = ask_select("选择拍摄设备", devices, default=preferred_device)
        if device is None or device is BACK:
            return device
        preferred_device = device
        candidates = [lut for lut in luts if lut.device == device]
        if len(candidates) == 1:
            lut = candidates[0]
            print(f"使用 LUT：{lut.profile} / {lut.look}")
            return lut
        choices = [
            questionary.Choice(f"{lut.profile} / {lut.look}", value=str(lut.path))
            for lut in candidates
        ]
        default_lut = (
            current if current is not None and current in candidates else candidates[0]
        )
        selected = ask_select("选择 LUT 风格", choices, default=str(default_lut.path))
        if selected is BACK:
            continue
        if selected is None:
            return None
        return next((lut for lut in candidates if str(lut.path) == selected), None)


def encoder_names(ffmpeg: str) -> set[str]:
    result = run_capture([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "无法读取 FFmpeg 编码器列表")
    return {
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.match(r"\s*[VASD\.]{6}\s+(\S+)", line))
    }


def probe_hardware_encoder(ffmpeg: str, spec: EncoderSpec) -> tuple[bool, str]:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=black:s=1920x1080:d=0.1:r=30",
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        spec.name,
        "-pix_fmt",
        spec.pixel_format,
    ]
    if spec.name == "hevc_videotoolbox":
        command += ["-profile:v", "main10", "-b:v", "10M"]
    command += ["-f", "null", "-"]
    try:
        result = run_capture(command, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "自检超时"
    if result.returncode == 0:
        return True, ""
    errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    reason = errors[0] if errors else "硬件编码初始化失败"
    return False, reason[:160]


def detect_encoders(ffmpeg: str, codec: str) -> list[EncoderAvailability]:
    compiled = encoder_names(ffmpeg)
    candidates = [spec for spec in ENCODER_CANDIDATES if spec.codec == codec and spec.name in compiled]
    statuses = []
    for spec in candidates:
        if spec.hardware:
            available, reason = probe_hardware_encoder(ffmpeg, spec)
            statuses.append(EncoderAvailability(spec, available, reason))
        else:
            statuses.append(EncoderAvailability(spec, True))
    return statuses


def select_encoder(
    statuses: Sequence[EncoderAvailability],
    current: EncoderSpec | None = None,
) -> EncoderSpec | object | None:
    available = [status.spec for status in statuses if status.available]
    unavailable = [status for status in statuses if not status.available]
    if unavailable:
        print("未通过硬件自检：")
        for status in unavailable:
            print(f"  - {status.spec.label}: {status.reason}")
    if not available:
        return None
    choices = []
    for spec in available:
        kind = "硬件，速度快" if spec.hardware else "软件，速度较慢但质量稳定"
        choices.append(questionary.Choice(f"{spec.label}（{kind}）", value=spec.name))
    default = current.name if current in available else available[0].name
    selected = ask_select("选择编码器 / 硬件加速", choices, default=default)
    if selected is None or selected is BACK:
        return selected
    return next((spec for spec in available if spec.name == selected), None)


def estimate_bitrate(video: VideoInfo, codec: str, quality: str) -> int:
    config = QUALITY_LEVELS[quality]
    bpp = float(config[f"{codec}_bpp"])
    fps = video.fps if video.fps > 0 else 30.0
    raw = video.width * video.height * fps * bpp
    return max(1_000_000, int(round(raw / 500_000) * 500_000))


def quality_description(videos: Sequence[VideoInfo], codec: str, quality: str) -> str:
    config = QUALITY_LEVELS[quality]
    bitrates = [estimate_bitrate(video, codec, quality) for video in videos]
    low, high = min(bitrates), max(bitrates)
    if low == high:
        rate = f"约 {low / 8_000_000:.2f} MB/s"
        per_hour = f"{low / 1_000_000 * 0.45:.1f} GB/h"
    else:
        rate = f"约 {low / 8_000_000:.2f}-{high / 8_000_000:.2f} MB/s"
        per_hour = f"{low / 1_000_000 * 0.45:.1f}-{high / 1_000_000 * 0.45:.1f} GB/h"
    return f"{config['label']} — {rate}，{per_hour}；{config['description']}"


def select_quality(
    videos: Sequence[VideoInfo],
    codec: str,
    current: str | None = None,
) -> str | object | None:
    choices = [
        questionary.Choice(quality_description(videos, codec, name), value=name)
        for name in QUALITY_LEVELS
    ]
    default = current if current in QUALITY_LEVELS else "balanced"
    return ask_select("选择输出质量（码率为内容无关的经验估算）", choices, default=default)


def encoder_arguments(spec: EncoderSpec, quality: str, bitrate: int) -> list[str]:
    config = QUALITY_LEVELS[quality]
    args = ["-c:v", spec.name, "-pix_fmt", spec.pixel_format]
    if spec.name == "libx265":
        args += ["-preset", "medium", "-crf", str(config["h265_crf"])]
    elif spec.name == "libsvtav1":
        args += ["-preset", "7", "-crf", str(config["av1_crf"])]
    elif spec.name == "libaom-av1":
        args += ["-cpu-used", "5", "-crf", str(config["av1_crf"]), "-b:v", "0"]
    else:
        maxrate = int(bitrate * 1.35)
        bufsize = int(bitrate * 2)
        args += ["-b:v", str(bitrate), "-maxrate", str(maxrate), "-bufsize", str(bufsize)]
        if spec.name == "hevc_videotoolbox":
            args += ["-profile:v", "main10"]
    if spec.codec == "h265":
        args += ["-tag:v", "hvc1"]
    else:
        args += ["-tag:v", "av01"]
    return args


def _filter_path(path: str | Path) -> str:
    value = str(path).replace("\\", "/").replace("'", "\\'").replace(":", "\\:")
    return f"'{value}'"


def lut_filter_value(lut: LutOption, pixel_format: str) -> str:
    official_lut = _filter_path(lut.path.name)
    return f"lut3d=file={official_lut}:interp=tetrahedral,format={pixel_format}"


def output_path_for(
    video: VideoInfo,
    input_dir: Path,
    output_dir: Path,
    codec: str,
) -> Path:
    try:
        relative_parent = video.path.resolve().relative_to(input_dir.resolve()).parent
    except ValueError:
        relative_parent = Path()
    return output_dir / relative_parent / f"{video.path.stem}_Rec709_{codec.upper()}.mp4"


def build_ffmpeg_command(
    ffmpeg: str,
    video: VideoInfo,
    output: Path,
    lut: LutOption,
    encoder: EncoderSpec,
    quality: str,
    overwrite: bool,
) -> list[str]:
    bitrate = estimate_bitrate(video, encoder.codec, quality)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(video.path.resolve()),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-vf",
        lut_filter_value(lut, encoder.pixel_format),
    ]
    command += encoder_arguments(encoder, quality, bitrate)
    command += [
        "-c:a",
        "copy",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
        "-metadata",
        f"comment=Converted to Rec.709 with {lut.path.name}",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    return command


def _format_eta(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "--:--"
    minutes, second = divmod(int(seconds), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{second:02d}" if hours else f"{minute:02d}:{second:02d}"


def _render_progress(index: int, total: int, name: str, ratio: float, speed: str, eta: float) -> None:
    width = 24
    completed = min(width, max(0, round(width * ratio)))
    bar = "#" * completed + "-" * (width - completed)
    print(
        f"\r[{index}/{total}] {name[:30]:30} [{bar}] {ratio * 100:5.1f}% "
        f"{speed:>7} ETA {_format_eta(eta)}",
        end="",
        flush=True,
    )


def transcode(
    command: Sequence[str],
    cwd: Path,
    video: VideoInfo,
    index: int,
    total: int,
) -> tuple[bool, str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    speed = "--x"
    output_seconds = 0.0
    errors: list[str] = []
    started = time.monotonic()
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if "=" not in line:
                if line:
                    errors.append(line)
                continue
            key, value = line.split("=", 1)
            if key in {"out_time_us", "out_time_ms"}:
                try:
                    output_seconds = int(value) / 1_000_000
                except ValueError:
                    pass
            elif key == "speed":
                speed = value
            elif key == "progress":
                ratio = min(1.0, output_seconds / video.duration) if video.duration > 0 else 0.0
                elapsed = time.monotonic() - started
                eta = elapsed * (1 - ratio) / ratio if ratio > 0 else math.inf
                _render_progress(index, total, video.path.name, ratio, speed, eta)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return_code = process.wait()
    if return_code == 0:
        _render_progress(index, total, video.path.name, 1.0, speed, 0)
        print()
        return True, ""
    print()
    return False, "\n".join(errors[-12:]) or f"FFmpeg 退出码 {return_code}"


def command_for_display(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def print_video_table(videos: Sequence[VideoInfo]) -> None:
    green = "\033[32;1m" if sys.stdout.isatty() else ""
    orange = "\033[33;1m" if sys.stdout.isatty() else ""
    reset = "\033[0m" if sys.stdout.isatty() else ""
    for video in videos:
        if video.is_dlog:
            label = "[D-Log M]" if video.dji_color_mode == "dlog_m" else "[D-Log]"
            marker = f"{green}{label}{reset}"
        elif video.dji_color_mode == "normal":
            marker = "[普通]"
        elif video.is_dji:
            marker = f"{orange}[DJI?]{reset}"
        else:
            marker = "[普通]"
        print(
            f"{marker:9} {video.path.name} | {video.width}x{video.height} "
            f"{video.fps:.2f}fps | {human_bitrate(video.video_bitrate)} | "
            f"{video.codec}/{video.pixel_format} | {human_duration(video.duration)}"
        )
        print(f"          判定：{video.detection_reason}")


def print_system_summary(platform_lut: str) -> None:
    system = platform.system()
    version = platform.mac_ver()[0] if system == "Darwin" else platform.release()
    system_name = "macOS" if system == "Darwin" else system
    print(f"系统：{system_name} {version} ({platform.machine()})")
    print(f"LUT：已自动选择 lut/{platform_lut}")
    if system == "Darwin" and platform.machine() == "arm64":
        print("硬件：检测到 Apple Silicon，芯片含 H.264/H.265 媒体引擎；仍会用 FFmpeg 做实际编码自检")
    elif system == "Windows":
        print("硬件：将实际检测 NVIDIA NVENC、Intel Quick Sync、AMD AMF 和 Media Foundation")


def ensure_dependency() -> bool:
    if questionary is not None:
        return True
    print("缺少交互界面依赖 questionary。请先运行：", file=sys.stderr)
    print("  conda activate dlog && python -m pip install -r requirements.txt", file=sys.stderr)
    return False


def resolve_executable(value: str, label: str) -> str:
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"找不到 {label}：{value}")
    return resolved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 DJI 官方 LUT 批量转换 D-Log 视频",
    )
    parser.add_argument("directory", nargs="?", help="待扫描的视频目录")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("-o", "--output", help="输出目录（默认：输入目录/output）")
    parser.add_argument("--lut-root", default=str(Path(__file__).resolve().parent / "lut"), help="LUT 根目录")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg 可执行文件")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe 可执行文件")
    parser.add_argument("--list", action="store_true", help="只分析并列出视频，不进入转换菜单")
    parser.add_argument("--dry-run", action="store_true", help="完成交互后只打印 FFmpeg 命令")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名输出文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.list and not ensure_dependency():
        return 2
    try:
        ffmpeg = resolve_executable(args.ffmpeg, "FFmpeg")
        ffprobe = resolve_executable(args.ffprobe, "FFprobe")
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    directory_value = args.directory
    if not directory_value:
        if args.list:
            print("错误：--list 模式必须提供目录", file=sys.stderr)
            return 2
        directory_value = questionary.path("输入视频目录", only_directories=True, style=PROMPT_STYLE).ask()
        if directory_value is None:
            return 130
    directory = Path(directory_value).expanduser().resolve()
    if not directory.is_dir():
        print(f"错误：不是有效目录：{directory}", file=sys.stderr)
        return 2
    output_dir = Path(args.output).expanduser().resolve() if args.output else directory / "output"

    try:
        luts, platform_lut = load_luts(Path(args.lut_root).expanduser().resolve())
    except (FileNotFoundError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print_system_summary(platform_lut)

    paths = discover_paths(directory, args.recursive, output_dir)
    if not paths:
        print(f"目录中没有支持的视频：{directory}")
        return 1
    videos, scan_errors = scan_videos(paths, ffprobe)
    for path, message in scan_errors:
        print(f"跳过无法分析的文件：{path.name}（{message}）", file=sys.stderr)
    if not videos:
        print("没有可转换的视频。")
        return 1
    print_video_table(videos)
    if args.list:
        return 0

    selected: list[VideoInfo] | None = None
    lut: LutOption | None = None
    codec: str | None = None
    encoder: EncoderSpec | None = None
    quality: str | None = None
    encoder_cache: dict[str, list[EncoderAvailability]] = {}
    step = 0
    try:
        while True:
            if step == 0:
                video_result = select_videos(videos, selected)
                if video_result is BACK or video_result is None:
                    return 130
                if not video_result:
                    print("没有选择视频，已退出。")
                    return 0
                selected = video_result
                step = 1
            elif step == 1:
                lut_result = select_lut(luts, selected, lut)
                if lut_result is BACK:
                    step = 0
                    continue
                if lut_result is None:
                    return 130
                lut = lut_result
                step = 2
            elif step == 2:
                codec_result = ask_select(
                    "选择视频编码",
                    [
                        questionary.Choice("H.265 / HEVC（兼容性较好）", value="h265"),
                        questionary.Choice("AV1（体积更小，编码更慢、兼容性较新）", value="av1"),
                    ],
                    default=codec or "h265",
                )
                if codec_result is BACK:
                    step = 1
                    continue
                if codec_result is None:
                    return 130
                if codec_result != codec:
                    encoder = None
                codec = codec_result
                step = 3
            elif step == 3:
                if codec not in encoder_cache:
                    print("正在检查 FFmpeg 编码器及硬件可用性...")
                    encoder_cache[codec] = detect_encoders(ffmpeg, codec)
                statuses = encoder_cache[codec]
                if not any(status.available for status in statuses):
                    print(f"当前 FFmpeg 没有可用的 {codec.upper()} 编码器。", file=sys.stderr)
                    step = 2
                    continue
                encoder_result = select_encoder(statuses, encoder)
                if encoder_result is BACK:
                    step = 2
                    continue
                if encoder_result is None:
                    return 130
                encoder = encoder_result
                step = 4
            else:
                quality_result = select_quality(selected, codec, quality)
                if quality_result is BACK:
                    step = 3
                    continue
                if quality_result is None:
                    return 130
                quality = quality_result
                break
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130

    assert selected is not None
    assert lut is not None
    assert codec is not None
    assert encoder is not None
    assert quality is not None

    print("\n转换配置：")
    print(f"  视频：{len(selected)} 个")
    print(f"  LUT：{lut.device} / {lut.profile} / {lut.look}")
    print("  色彩空间：SDR / Rec.709")
    print(f"  编码：{encoder.label}，10-bit {codec.upper()}")
    print(f"  质量：{quality_description(selected, codec, quality)}")
    print(f"  输出：{output_dir}")

    succeeded = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []
    for index, video in enumerate(selected, start=1):
        final_output = output_path_for(video, directory, output_dir, codec)
        if final_output.exists() and not args.overwrite:
            print(f"[{index}/{len(selected)}] 跳过已有文件：{final_output.name}")
            skipped += 1
            continue
        temporary = final_output.with_name(f".{final_output.stem}.part-{os.getpid()}.mp4")
        command = build_ffmpeg_command(
            ffmpeg,
            video,
            temporary,
            lut,
            encoder,
            quality,
            overwrite=True,
        )
        if args.dry_run:
            print(command_for_display(command))
            continue
        final_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            ok, message = transcode(command, lut.path.parent, video, index, len(selected))
        except KeyboardInterrupt:
            if temporary.exists():
                temporary.unlink()
            print("\n转换已取消，源文件未改动。")
            return 130
        if ok:
            os.replace(temporary, final_output)
            succeeded += 1
        else:
            if temporary.exists():
                temporary.unlink()
            failed.append((video.path, message))

    if args.dry_run:
        print("\nDry-run 完成，没有写入视频文件。")
        return 0
    print(f"\n完成：成功 {succeeded}，跳过 {skipped}，失败 {len(failed)}。")
    for path, message in failed:
        print(f"\n失败：{path.name}\n{message}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
