#!/usr/bin/env python3
"""Restore capture timestamps from DJI exports and remux them to MP4."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
QUICKTIME_DATE_KEYS = (
    "com.apple.quicktime.creationdate",
    "creationdate",
    "date",
)


@dataclass(frozen=True)
class CaptureTime:
    instant: datetime
    source: str


def run_capture(command: Sequence[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
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


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _all_tags(probe: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result = [("容器", probe.get("format", {}).get("tags", {}))]
    result.extend(
        (f"轨道 {stream.get('index', index)}", stream.get("tags", {}))
        for index, stream in enumerate(probe.get("streams", []))
    )
    return result


def capture_time_from_probe(path: Path, probe: dict[str, Any]) -> CaptureTime:
    tag_groups = _all_tags(probe)
    for wanted_key in QUICKTIME_DATE_KEYS:
        for group_name, tags in tag_groups:
            for key, value in tags.items():
                if str(key).lower() != wanted_key or not value:
                    continue
                try:
                    return CaptureTime(parse_datetime(str(value)), f"{group_name} {key}")
                except ValueError:
                    continue

    filename_match = re.search(r"(?:^|_)DJI_(\d{14})(?:_|\.|$)", path.name, re.IGNORECASE)
    if filename_match:
        parsed = datetime.strptime(filename_match.group(1), "%Y%m%d%H%M%S").astimezone()
        return CaptureTime(parsed, "DJI 文件名")

    for group_name, tags in tag_groups:
        for key, value in tags.items():
            if str(key).lower() != "creation_time" or not value:
                continue
            try:
                return CaptureTime(parse_datetime(str(value)), f"{group_name} {key}（备用）")
            except ValueError:
                continue
    raise ValueError("没有找到可用的拍摄时间元数据")


def probe_capture_time(path: Path, ffprobe: str) -> CaptureTime:
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
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise ValueError(detail[-1] if detail else "FFprobe 无法读取")
    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("FFprobe 返回了无效 JSON") from exc
    return capture_time_from_probe(path, probe)


def discover_videos(directory: Path, output_dir: Path, recursive: bool) -> list[Path]:
    iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
    output_resolved = output_dir.resolve()
    videos: list[Path] = []
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            if output_resolved in path.resolve().parents:
                continue
        except OSError:
            pass
        videos.append(path)
    return sorted(videos, key=lambda item: item.name.casefold())


def local_capture_time(capture: CaptureTime, target_zone: ZoneInfo | None) -> datetime:
    return capture.instant.astimezone(target_zone) if target_zone else capture.instant.astimezone()


def output_path_for(
    capture: CaptureTime,
    output_dir: Path,
    target_zone: ZoneInfo | None,
    reserved: set[Path],
) -> Path:
    timestamp = local_capture_time(capture, target_zone).strftime("%Y%m%d%H%M%S")
    base = output_dir / f"DJI_{timestamp}.mp4"
    if base not in reserved and not base.exists():
        reserved.add(base)
        return base
    for sequence in range(1, 10_000):
        candidate = output_dir / f"DJI_{timestamp}_{sequence:02d}.mp4"
        if candidate not in reserved and not candidate.exists():
            reserved.add(candidate)
            return candidate
    raise ValueError(f"同一秒的重名文件过多：{timestamp}")


def ffmpeg_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    capture: CaptureTime,
    overwrite: bool,
) -> list[str]:
    utc_value = capture.instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(source),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-c",
        "copy",
        "-strict",
        "unofficial",
        "-metadata",
        f"creation_time={utc_value}",
        "-metadata:s:v",
        f"creation_time={utc_value}",
        "-metadata:s:a",
        f"creation_time={utc_value}",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(destination),
    ]


def _set_windows_file_times(path: Path, instant: datetime) -> None:
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    set_file_time = ctypes.windll.kernel32.SetFileTime
    set_file_time.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    set_file_time.restype = wintypes.BOOL

    handle = create_file(str(path), 0x0100, 0x00000007, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "无法打开输出文件以设置时间")
    try:
        intervals = int((instant.timestamp() + 11_644_473_600) * 10_000_000)
        file_time = wintypes.FILETIME(intervals & 0xFFFFFFFF, intervals >> 32)
        if not set_file_time(handle, file_time, None, file_time):
            raise OSError(ctypes.get_last_error(), "无法设置 Windows 文件时间")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def restore_filesystem_timestamp(path: Path, capture: CaptureTime) -> str | None:
    epoch = capture.instant.timestamp()
    os.utime(path, (epoch, epoch))
    system = platform.system()
    if system == "Darwin":
        set_file = shutil.which("SetFile")
        if not set_file:
            return "未找到 SetFile，仅恢复了修改时间"
        local_value = capture.instant.astimezone().strftime("%m/%d/%Y %H:%M:%S")
        result = run_capture([set_file, "-d", local_value, "-m", local_value, str(path)])
        if result.returncode != 0:
            return result.stderr.strip() or "SetFile 无法恢复创建时间"
    elif system == "Windows":
        try:
            _set_windows_file_times(path, capture.instant)
        except OSError as exc:
            return str(exc)
    return None


def publish_without_overwrite(temporary: Path, destination: Path) -> None:
    """Atomically publish a same-filesystem temporary file without replacing data."""
    os.link(temporary, destination)
    temporary.unlink()


def resolve_executable(value: str, label: str) -> str:
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"找不到 {label}：{value}")
    return resolved


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="恢复 DJI 导出视频的拍摄时间并无损封装为 MP4")
    parser.add_argument("directory", help="待处理的视频目录")
    parser.add_argument("-o", "--output", help="输出目录（默认：输入目录/output）")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("--timezone", help="文件名使用的 IANA 时区，例如 Asia/Shanghai（默认：系统时区）")
    parser.add_argument("--dry-run", action="store_true", help="只显示将执行的操作")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg 可执行文件")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe 可执行文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(f"错误：不是有效目录：{directory}", file=sys.stderr)
        return 2
    output_dir = Path(args.output).expanduser().resolve() if args.output else directory / "output"
    try:
        ffmpeg = resolve_executable(args.ffmpeg, "FFmpeg")
        ffprobe = resolve_executable(args.ffprobe, "FFprobe")
        target_zone = ZoneInfo(args.timezone) if args.timezone else None
    except (FileNotFoundError, ZoneInfoNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    videos = discover_videos(directory, output_dir, args.recursive)
    if not videos:
        print(f"目录中没有可处理的视频：{directory}")
        return 1

    reserved: set[Path] = set()
    jobs: list[tuple[Path, Path, CaptureTime]] = []
    failed: list[tuple[Path, str]] = []
    for source in videos:
        try:
            capture = probe_capture_time(source, ffprobe)
            destination = output_path_for(capture, output_dir, target_zone, reserved)
            jobs.append((source, destination, capture))
            local_time = local_capture_time(capture, target_zone)
            print(
                f"{source.name}\n"
                f"  拍摄时间：{local_time.isoformat(timespec='seconds')}（{capture.source}）\n"
                f"  输出文件：{destination.name}"
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            failed.append((source, str(exc)))

    if args.dry_run:
        print(f"\nDry-run：将处理 {len(jobs)} 个视频，没有写入文件。")
        for source, message in failed:
            print(f"跳过 {source.name}：{message}", file=sys.stderr)
        return 1 if failed and not jobs else 0

    output_dir.mkdir(parents=True, exist_ok=True)
    succeeded = 0
    for index, (source, destination, capture) in enumerate(jobs, start=1):
        temporary = destination.with_name(f".{destination.stem}.part-{os.getpid()}.mp4")
        command = ffmpeg_command(ffmpeg, source, temporary, capture, overwrite=True)
        print(f"[{index}/{len(jobs)}] 正在无损封装：{destination.name}")
        try:
            result = run_capture(command, timeout=3600)
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()
                raise ValueError(detail[-1] if detail else f"FFmpeg 退出码 {result.returncode}")
            publish_without_overwrite(temporary, destination)
            warning = restore_filesystem_timestamp(destination, capture)
            if warning:
                print(f"  时间提示：{warning}")
            succeeded += 1
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if temporary.exists():
                temporary.unlink()
            failed.append((source, str(exc)))

    print(f"\n完成：成功 {succeeded}，失败 {len(failed)}。")
    for source, message in failed:
        print(f"失败 {source.name}：{message}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
