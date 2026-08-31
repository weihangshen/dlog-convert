import tempfile
import unittest
from pathlib import Path

import convert


class DLogConverterTests(unittest.TestCase):
    def test_checkbox_uses_green_check_and_large_empty_circle(self):
        self.assertEqual(convert.questionary_common.INDICATOR_SELECTED, "✓")
        self.assertEqual(convert.questionary_common.INDICATOR_UNSELECTED, "◯")
        styles = dict(convert.PROMPT_STYLE.style_rules)
        self.assertIn("fg:#00a86b", styles["selected"])

    def test_parse_standard_and_vivid_luts(self):
        standard = convert.parse_lut_filename(
            Path("DJI OSMO Pocket 4 D-Log to Rec.709 V1.0.cube")
        )
        vivid = convert.parse_lut_filename(
            Path("DJI OSMO Pocket 4 D-Log to Rec.709 vivid V1.0.cube")
        )
        flip = convert.parse_lut_filename(Path("DJI Flip D-Log M to Rec.709 V1.cube"))

        self.assertEqual(standard.device, "DJI OSMO Pocket 4")
        self.assertEqual(standard.look, "标准")
        self.assertEqual(vivid.look, "Vivid")
        self.assertEqual(flip.profile, "D-Log M")

    def test_djmd_packet_distinguishes_dlog_from_normal(self):
        dlog_packet = bytes.fromhex("120f0a071a05766964656f12041a020802")
        normal_packet = bytes.fromhex("120d0a071a05766964656f12021a00")

        self.assertEqual(convert._dji_color_mode_from_djmd_packet(dlog_packet), "dlog")
        self.assertEqual(convert._dji_color_mode_from_djmd_packet(normal_packet), "normal")

    def test_ffprobe_hex_packet_parser(self):
        dump = "\n00000000: 120f 0a07 1a05 7669 6465 6f12 041a 0208  ......video.....\n00000010: 02                                       .\n"
        self.assertEqual(
            convert.parse_ffprobe_hex_data(dump),
            bytes.fromhex("120f0a071a05766964656f12041a020802"),
        )

    def test_djmd_color_mode_overrides_ambiguous_d_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DJI_20260828204320_0227_D.MP4"
            path.touch()
            probe = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 3840,
                        "height": 2160,
                        "pix_fmt": "yuv420p10le",
                        "avg_frame_rate": "60000/1001",
                        "duration": "11.2",
                    },
                    {"codec_type": "data", "codec_tag_string": "djmd"},
                ],
                "format": {
                    "duration": "11.2",
                    "size": "1000",
                    "tags": {"encoder": "DJI OsmoPocket4"},
                },
            }
            dlog_video = convert.video_info_from_probe(path, probe, "dlog")
            normal_video = convert.video_info_from_probe(path, probe, "normal")

        self.assertTrue(dlog_video.is_dji)
        self.assertTrue(dlog_video.is_dlog)
        self.assertFalse(normal_video.is_dlog)
        self.assertAlmostEqual(dlog_video.fps, 59.94005994)
        self.assertEqual(dlog_video.device_hint, "DJI OsmoPocket4")
        self.assertEqual(normal_video.detection_reason, "DJI djmd 色彩模式为普通")
        self.assertEqual(convert.video_choice_title(normal_video)[0][1], "[普通]   ")

    def test_normal_dji_is_not_assumed_to_be_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DJI_001_N.MP4"
            path.touch()
            probe = {
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                "format": {"size": "0", "tags": {"encoder": "DJI Camera"}},
            }
            video = convert.video_info_from_probe(path, probe)

        self.assertTrue(video.is_dji)
        self.assertFalse(video.is_dlog)

    def test_quality_estimate_scales_with_fps(self):
        base = dict(
            path=Path("video.mp4"),
            duration=60,
            width=3840,
            height=2160,
            codec="hevc",
            pixel_format="yuv420p10le",
            video_bitrate=0,
            size=0,
            is_dji=True,
            is_dlog=True,
            detection_reason="test",
            device_hint="DJI",
        )
        thirty = convert.VideoInfo(fps=30, **base)
        sixty = convert.VideoInfo(fps=60, **base)
        self.assertAlmostEqual(
            convert.estimate_bitrate(sixty, "h265", "balanced"),
            convert.estimate_bitrate(thirty, "h265", "balanced") * 2,
            delta=500_000,
        )

    def test_source_bitrate_display(self):
        self.assertEqual(convert.human_bitrate(120_174_343), "15.02 MB/s")
        self.assertEqual(convert.human_bitrate(0), "码率未知")

    def test_default_selection_only_selects_dlog(self):
        base = dict(
            path=Path("video.mp4"), width=1920, height=1080, fps=30, codec="hevc",
            pixel_format="yuv420p10le", video_bitrate=0, size=0, is_dji=True,
            detection_reason="test", device_hint="DJI",
        )
        short_dlog = convert.VideoInfo(duration=1, is_dlog=True, **base)
        long_normal = convert.VideoInfo(
            duration=600, is_dlog=False, dji_color_mode="normal", **base
        )

        self.assertTrue(convert.default_video_selected(short_dlog))
        self.assertFalse(convert.default_video_selected(long_normal))

    def test_video_selection_is_sorted_by_filename(self):
        base = dict(
            duration=20, width=1920, height=1080, fps=30, codec="hevc",
            pixel_format="yuv420p10le", video_bitrate=0, is_dji=True, is_dlog=True,
            detection_reason="test", device_hint="DJI",
        )
        beta = convert.VideoInfo(path=Path("Beta.mp4"), size=5_000, **base)
        alpha = convert.VideoInfo(path=Path("alpha.mp4"), size=1_000, **base)
        gamma = convert.VideoInfo(path=Path("gamma.mp4"), size=3_000, **base)

        ordered = convert.sort_videos_for_selection([beta, gamma, alpha])
        self.assertEqual(
            [video.path.name for video in ordered],
            ["alpha.mp4", "Beta.mp4", "gamma.mp4"],
        )

    def test_quality_menu_has_ten_levels_and_byte_rate_units(self):
        self.assertEqual(len(convert.QUALITY_LEVELS), 10)
        video = convert.VideoInfo(
            Path("video.mp4"), 60, 3840, 2160, 60, "hevc", "yuv420p10le",
            120_000_000, 0, True, True, "test", "DJI",
        )
        description = convert.quality_description([video], "h265", "balanced")
        self.assertIn("MB/s", description)
        self.assertIn("GB/h", description)
        self.assertNotIn("Mb/s", description)

    def test_command_maps_only_primary_video_and_audio(self):
        video = convert.VideoInfo(
            Path("/input/DJI_D.MP4"),
            10,
            3840,
            2160,
            60,
            "hevc",
            "yuv420p10le",
            0,
            0,
            True,
            True,
            "test",
            "DJI",
        )
        lut = convert.LutOption(Path("/lut/Test LUT.cube"), "DJI Test", "D-Log", "标准")
        encoder = convert.EncoderSpec("libx265", "CPU", "h265", False, "yuv420p10le")
        command = convert.build_ffmpeg_command(
            "ffmpeg", video, Path("/output/test.mp4"), lut, encoder, "balanced", True
        )
        command_text = " ".join(command)

        self.assertIn("0:v:0", command)
        self.assertIn("0:a?", command)
        self.assertNotIn("0:d", command_text)
        self.assertIn("lut3d=file='Test LUT.cube'", command_text)
        self.assertIn("yuv420p10le", command)
        self.assertIn("bt709", command)
        self.assertEqual(
            convert.output_path_for(video, Path("/input"), Path("/output"), "h265").name,
            "DJI_D_Rec709_H265.mp4",
        )


if __name__ == "__main__":
    unittest.main()
