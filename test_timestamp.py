import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import timestamp


class TimestampTests(unittest.TestCase):
    def test_quicktime_creation_date_has_priority_over_export_time(self):
        probe = {
            "format": {
                "tags": {
                    "creation_time": "2026-08-31T09:56:10Z",
                    "com.apple.quicktime.creationdate": "2026-08-28T00:10:10Z",
                }
            }
        }
        capture = timestamp.capture_time_from_probe(Path("export.mov"), probe)

        self.assertEqual(capture.instant, timestamp.parse_datetime("2026-08-28T00:10:10Z"))
        self.assertIn("com.apple.quicktime.creationdate", capture.source)

    def test_output_name_uses_requested_timezone(self):
        capture = timestamp.CaptureTime(
            timestamp.parse_datetime("2026-08-30T06:04:10Z"), "test"
        )
        output = timestamp.output_path_for(
            capture,
            Path("output"),
            ZoneInfo("Asia/Shanghai"),
            set(),
        )
        self.assertEqual(output.name, "DJI_20260830140410.mp4")

    def test_duplicate_names_get_sequence_suffix(self):
        capture = timestamp.CaptureTime(
            timestamp.parse_datetime("2026-08-30T06:04:10Z"), "test"
        )
        reserved: set[Path] = set()
        first = timestamp.output_path_for(
            capture, Path("output"), ZoneInfo("Asia/Shanghai"), reserved
        )
        second = timestamp.output_path_for(
            capture, Path("output"), ZoneInfo("Asia/Shanghai"), reserved
        )
        self.assertEqual(first.name, "DJI_20260830140410.mp4")
        self.assertEqual(second.name, "DJI_20260830140410_01.mp4")

    def test_existing_output_is_never_reused(self):
        capture = timestamp.CaptureTime(
            timestamp.parse_datetime("2026-08-30T06:04:10Z"), "test"
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "DJI_20260830140410.mp4").write_bytes(b"existing")
            output = timestamp.output_path_for(
                capture, output_dir, ZoneInfo("Asia/Shanghai"), set()
            )

        self.assertEqual(output.name, "DJI_20260830140410_01.mp4")

    def test_publish_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / ".part.mp4"
            destination = root / "video.mp4"
            temporary.write_bytes(b"new")
            destination.write_bytes(b"existing")

            with self.assertRaises(FileExistsError):
                timestamp.publish_without_overwrite(temporary, destination)

            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(temporary.read_bytes(), b"new")

    def test_ffmpeg_command_stream_copies_into_mp4(self):
        capture = timestamp.CaptureTime(
            timestamp.parse_datetime("2026-08-30T06:04:10Z"), "test"
        )
        command = timestamp.ffmpeg_command(
            "ffmpeg", Path("input.mov"), Path("output.mp4"), capture, False
        )
        self.assertIn("copy", command)
        self.assertIn("mp4", command)
        self.assertIn("unofficial", command)
        self.assertIn("creation_time=2026-08-30T06:04:10Z", command)


if __name__ == "__main__":
    unittest.main()
