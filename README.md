# DJI D-Log 批量还原工具

交互式选择目录中的视频并识别 DJI D-Log 素材，使用 `lut/Mac` 或 `lut/Win` 中对应的官方 LUT 输出 10-bit SDR Rec.709 视频。

## 安装

需要系统中已有 `ffmpeg` 和 `ffprobe`。项目的 Python 依赖安装到 `dlog` Conda 环境：

```bash
conda activate dlog
python -m pip install -r requirements.txt
```

## 使用

```bash
conda activate dlog
python convert.py /path/to/videos
```

视频菜单按文件名升序排列，并显示分辨率、帧率、原视频码率、时长和大小。默认只勾选明确识别为 D-Log 的视频；使用方向键移动、空格选中或取消，按 A 全选/全不选，Enter 确认，Esc 返回上一个选项。码率统一显示为 `MB/s`，质量菜单同时显示预计的 `GB/h`。确认视频后依次选择设备、Standard/Vivid LUT 风格、H.265/AV1、当前机器实际可用的编码器和画质。

转换结果默认写入输入目录的 `output` 子目录。已有同名结果默认跳过；明确需要覆盖时使用 `--overwrite`。

常用选项：

```bash
# 只查看识别结果
python convert.py /path/to/videos --list

# 递归扫描，并指定输出目录
python convert.py /path/to/videos --recursive --output /path/to/output

# 完成交互但只显示 FFmpeg 命令
python convert.py /path/to/videos --dry-run
```

## D-Log 识别

支持 Pocket 4 的 D-Log（`djmd` 色彩模式值 `2`）和 DJI Flip 的 D-Log M（值 `19`）。Flip 的 D-Log M 显示绿色 `[D-Log M]` 并默认勾选。普通模式为空消息或值 `0`；未知模式不再直接判为普通。此规则已用 20 个 Flip 原片验证：12 个 D-Log M、8 个普通视频。`output` 文件名只用于本次核对人工标注，程序识别时不依赖输出目录、文件名、帧率或位深。

程序会读取 DJI `djmd` 数据轨首包里的色彩模式字段，并以此区分 D-Log 与普通视频。文件名中的 `_D` 不是色彩模式标记，不参与判断；仅当 `djmd` 无法读取时，才会将 FFprobe 发现的明确 D-Log 文本元数据作为备用依据。明确识别为 D-Log 的视频显示绿色 `[D-Log]` 并默认勾选，明确识别为普通模式的显示 `[普通]`；检测为 DJI 但无法确认色彩模式的素材才显示 `[DJI?]`。列表始终按文件名排序。

## 恢复导出视频时间

`timestamp.py` 会优先读取 DJI 导出视频中的 `com.apple.quicktime.creationdate` 原始拍摄时间，无损封装为 MP4，并以本地拍摄时间命名，例如 `DJI_20260830140410.mp4`。输出文件默认位于输入目录的 `output` 子目录，同时恢复容器创建时间、文件修改时间，以及 macOS/Windows 文件创建时间。

```bash
conda activate dlog
python timestamp.py /path/to/videos

# 递归扫描，或先预览识别结果
python timestamp.py /path/to/videos --recursive
python timestamp.py /path/to/videos --dry-run

# 明确指定文件名所用时区
python timestamp.py /path/to/videos --timezone Asia/Shanghai
```

如果多个视频的拍摄时间精确到同一秒，或者 `output` 中已经存在同名文件，后续文件会自动命名为 `_01`、`_02`。脚本始终生成新文件，不覆盖源视频或已有输出。

## 编码与硬件

- macOS：检测 Apple VideoToolbox。Apple Silicon 的 H.265 媒体引擎可用于硬件编码；当前 Apple 芯片不提供 AV1 硬件编码时会自动只列出软件 AV1。
- Windows：按 FFmpeg 编译能力和一次实际编码自检，检测 NVIDIA NVENC、Intel Quick Sync、AMD AMF 与 Media Foundation。
- 软件回退：H.265 使用 x265；AV1 优先使用 SVT-AV1。

质量菜单提供从极限压缩到归档级共 10 档。硬件编码按菜单中估算的目标码率控制；软件编码使用 CRF，菜单码率是依据分辨率和帧率给出的经验估算，实际码率会随画面复杂度变化。

工具只映射主视频轨、音频和常规元数据，不复制 DJI 的 `dbgi/djmd` 调试数据轨。源文件永远不会被修改。

## 测试

```bash
conda activate dlog
python -m unittest -v
python convert.py test --list
```
