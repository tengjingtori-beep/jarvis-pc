# -*- coding: utf-8 -*-
"""説明動画づくり（jarvis.py の動画セクション）の単体テスト。
GUI・モデル・音声合成は使わず、純粋ロジックと描画の形だけを確かめる。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import jarvis  # noqa: E402


SCRIPT_OK = """---場面---
タイトル: 朝の準備
絵: チェックリスト
スライド:
・顔を洗う
・朝ごはんを食べる
ナレーション: 朝はまず顔を洗いましょう。次に朝ごはんを食べます。
---場面---
タイトル: 出発の時間
絵: 時計
スライド:
・忘れ物を確かめる
ナレーション: 時計を見て、遅れないように出発しましょう。
"""


class TestParseVideoScript(unittest.TestCase):
    def test_well_formed(self):
        scenes = jarvis.parse_video_script(SCRIPT_OK)
        self.assertEqual(len(scenes), 2)
        s0 = scenes[0]
        self.assertEqual(s0["title"], "朝の準備")
        self.assertEqual(s0["motif_raw"], "チェックリスト")
        self.assertEqual(s0["bullets"], ["顔を洗う", "朝ごはんを食べる"])
        self.assertIn("顔を洗いましょう", s0["narration"])
        self.assertIn("朝ごはんを食べます", s0["narration"])
        self.assertEqual(scenes[1]["title"], "出発の時間")
        self.assertEqual(scenes[1]["bullets"], ["忘れ物を確かめる"])

    def test_fullwidth_colons_and_bullet_marks(self):
        text = ("---場面---\nタイトル： 題\n絵： ロボット\nスライド：\n"
                "- 一つ目\n* 二つ目\nナレーション： 本文です。\n")
        scenes = jarvis.parse_video_script(text)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["title"], "題")
        self.assertEqual(scenes[0]["bullets"], ["一つ目", "二つ目"])
        self.assertEqual(scenes[0]["narration"], "本文です。")

    def test_no_separators_gives_nothing(self):
        self.assertEqual(jarvis.parse_video_script("こんにちは。いい天気です。"), [])
        self.assertEqual(jarvis.parse_video_script(""), [])
        self.assertEqual(jarvis.parse_video_script(None), [])

    def test_think_is_ignored(self):
        text = "<think>---場面---\nタイトル: 偽物</think>\n" + SCRIPT_OK
        scenes = jarvis.parse_video_script(text)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0]["title"], "朝の準備")

    def test_multiline_narration_joined(self):
        text = ("---場面---\nタイトル: 題\nナレーション: 一行目です。\n二行目です。\n")
        scenes = jarvis.parse_video_script(text)
        self.assertEqual(scenes[0]["narration"], "一行目です。 二行目です。")

    def test_bullets_capped_at_four(self):
        body = "\n".join("・項目%d" % i for i in range(7))
        text = "---場面---\nタイトル: 題\nスライド:\n%s\nナレーション: 本文。\n" % body
        scenes = jarvis.parse_video_script(text)
        self.assertEqual(len(scenes[0]["bullets"]), 4)


class TestChooseMotif(unittest.TestCase):
    def test_explicit_japanese(self):
        self.assertEqual(jarvis.choose_motif("ロボット", ""), "robot")
        self.assertEqual(jarvis.choose_motif("チェックリスト", ""), "checklist")
        self.assertEqual(jarvis.choose_motif("時計", ""), "clock")
        self.assertEqual(jarvis.choose_motif("エンターキー", ""), "enter")

    def test_explicit_english(self):
        self.assertEqual(jarvis.choose_motif("robot", ""), "robot")
        self.assertEqual(jarvis.choose_motif("Gear", ""), "gear")

    def test_keyword_fallback(self):
        self.assertEqual(jarvis.choose_motif("", "やることを確かめる"), "checklist")
        self.assertEqual(jarvis.choose_motif("", "パソコンを開いて"), "laptop")
        self.assertEqual(jarvis.choose_motif("", "時計を見て出発"), "clock")
        self.assertEqual(jarvis.choose_motif("", "フォルダに整理する"), "folder")
        self.assertEqual(jarvis.choose_motif("宇宙", "時計を見て出発"), "clock")

    def test_default_is_document(self):
        self.assertEqual(jarvis.choose_motif("", "今日はいい天気です"), "document")
        self.assertEqual(jarvis.choose_motif("", ""), "document")
        self.assertEqual(jarvis.choose_motif(None, None), "document")


class TestNarrationText(unittest.TestCase):
    def test_split_sentences(self):
        out = jarvis.split_narration_sentences("一です。二です！三ですか？")
        self.assertEqual(out, ["一です。", "二です！", "三ですか？"])

    def test_split_empty(self):
        self.assertEqual(jarvis.split_narration_sentences(""), [])
        self.assertEqual(jarvis.split_narration_sentences("  "), [])

    def test_split_long_sentence_at_comma(self):
        text = "あ" * 30 + "、" + "い" * 40 + "。"
        out = jarvis.split_narration_sentences(text)
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].endswith("、"))
        self.assertTrue(out[1].endswith("。"))

    def test_split_no_punctuation(self):
        self.assertEqual(jarvis.split_narration_sentences("そのままの文"), ["そのままの文"])

    def test_clean_narration(self):
        out = jarvis.clean_narration_text(
            "**大事**です。詳しくは https://example.com/x を見て。\nコマンド: dir /b")
        self.assertNotIn("**", out)
        self.assertNotIn("example.com", out)
        self.assertNotIn("dir", out)
        self.assertIn("大事です", out)

    def test_clean_removes_code_fence(self):
        out = jarvis.clean_narration_text("前です。\n```\ncode\n```\n後です。")
        self.assertNotIn("code", out)
        self.assertIn("前です", out)
        self.assertIn("後です", out)

    def test_estimate_bounds(self):
        self.assertEqual(jarvis.estimate_narration_sec(""), 1.2)
        self.assertEqual(jarvis.estimate_narration_sec("あ" * 13), 2.0)
        self.assertEqual(jarvis.estimate_narration_sec("あ" * 650), 10.0)


class TestStagePlan(unittest.TestCase):
    def test_levels_and_pad(self):
        stages, total = jarvis.build_stage_plan([
            [("あ。", 2.0), ("い。", 3.0)],
            [("う。", None)],
        ])
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[0]["level"], 1)
        self.assertEqual(stages[0]["duration"], 2.0)
        self.assertEqual(stages[1]["level"], 3)  # 場面の最後は必ず全部入り
        self.assertAlmostEqual(stages[1]["duration"], 3.4)  # 場面末尾に +0.4
        self.assertEqual(stages[2]["scene"], 1)
        self.assertAlmostEqual(stages[2]["duration"], 1.6)  # 推定1.2 + 0.4
        self.assertAlmostEqual(total, 7.0)

    def test_short_scene_reaches_full_slide(self):
        stages, _ = jarvis.build_stage_plan([[("一文だけ。", 2.0)]])
        self.assertEqual(stages[0]["level"], 3)
        stages, _ = jarvis.build_stage_plan([[("一。", 1.0), ("二。", 1.0)]])
        self.assertEqual([s["level"] for s in stages], [1, 3])
        stages, _ = jarvis.build_stage_plan([[("一。", 1.0), ("二。", 1.0), ("三。", 1.0)]])
        self.assertEqual([s["level"] for s in stages], [1, 2, 3])

    def test_level_caps_at_three(self):
        stages, _total = jarvis.build_stage_plan(
            [[("文%d。" % i, 1.0) for i in range(5)]])
        self.assertEqual([s["level"] for s in stages], [1, 2, 3, 3, 3])

    def test_empty_scene_single_stage(self):
        stages, total = jarvis.build_stage_plan([[]])
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["level"], 3)
        self.assertEqual(stages[0]["caption"], "")
        self.assertEqual(stages[0]["duration"], 3.0)
        self.assertEqual(total, 3.0)

    def test_captions_carried(self):
        stages, _ = jarvis.build_stage_plan([[("一文目。", 1.5), ("二文目。", 1.5)]])
        self.assertEqual([s["caption"] for s in stages], ["一文目。", "二文目。"])
        self.assertEqual([s["sentence"] for s in stages], [0, 1])


class TestTemplatePlanning(unittest.TestCase):
    def test_seed_deterministic(self):
        self.assertEqual(jarvis.video_seed_for("朝の準備"), jarvis.video_seed_for("朝の準備"))
        self.assertNotEqual(jarvis.video_seed_for("朝の準備"), jarvis.video_seed_for("夜の勉強"))

    def test_scraps_deterministic_and_in_range(self):
        a = jarvis.plan_scraps(12345, 1080, 1920)
        b = jarvis.plan_scraps(12345, 1080, 1920)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 7)
        for sc in a:
            self.assertIn(sc["color"], jarvis.SCRAP_COLORS)
            self.assertGreater(sc["w"], 0)
            self.assertGreater(sc["h"], 0)

    def test_torn_points_deterministic(self):
        import random as _random
        p1 = jarvis.torn_rect_points(0, 0, 200, 100, _random.Random(42))
        p2 = jarvis.torn_rect_points(0, 0, 200, 100, _random.Random(42))
        self.assertEqual(p1, p2)
        self.assertGreaterEqual(len(p1), 20)

    def test_output_name(self):
        self.assertEqual(jarvis.video_output_name("朝の準備/やり方"), "video_朝の準備.mp4")
        self.assertEqual(jarvis.video_output_name(""), "video_説明.mp4")
        name = jarvis.video_output_name("あ" * 40)
        self.assertTrue(name.startswith("video_"))
        self.assertTrue(name.endswith(".mp4"))
        self.assertLessEqual(len(name), len("video_") + 20 + len(".mp4"))
        self.assertNotIn("/", jarvis.video_output_name("a/b"))
        self.assertNotIn(":", jarvis.video_output_name("a:b"))


class TestWrapAndWav(unittest.TestCase):
    def test_wrap_by_fake_width(self):
        width_of = lambda s: len(s) * 10  # noqa: E731
        self.assertEqual(jarvis.wrap_text_by_width("あいうえおかき", 30, width_of),
                         ["あいう", "えおか", "き"])

    def test_wrap_paragraphs(self):
        width_of = lambda s: len(s) * 10  # noqa: E731
        self.assertEqual(jarvis.wrap_text_by_width("ab\ncd", 100, width_of), ["ab", "cd"])

    def test_silent_wav_duration(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sil.wav")
            self.assertTrue(jarvis.write_silent_wav(path, 0.5, rate=8000))
            self.assertAlmostEqual(jarvis.wav_duration_sec(path), 0.5, places=2)

    def test_wav_duration_missing(self):
        self.assertEqual(jarvis.wav_duration_sec("/no/such/file.wav"), 0.0)


class TestFfmpegArgs(unittest.TestCase):
    def test_segment_args(self):
        args = jarvis.build_segment_args("ffmpeg", "a.png", "b.wav", 2.5, "seg.mp4")
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("-loop", args)
        self.assertIn("a.png", args)
        self.assertIn("b.wav", args)
        self.assertEqual(args[args.index("-t") + 1], "2.500")
        self.assertIn("libx264", args)
        self.assertEqual(args[-1], "seg.mp4")

    def test_concat_list_escapes(self):
        out = jarvis.build_concat_list(["C:\\work\\seg_000.mp4", "o'clock.mp4"])
        self.assertIn("file 'C:/work/seg_000.mp4'", out)
        self.assertIn("o'\\''clock.mp4", out)
        self.assertTrue(out.endswith("\n"))

    def test_concat_args(self):
        args = jarvis.build_concat_args("ffmpeg", "list.txt", "out.mp4")
        self.assertIn("concat", args)
        self.assertIn("list.txt", args)
        self.assertEqual(args[-1], "out.mp4")

    def test_xfade_filter_math(self):
        built = jarvis.build_xfade_filter([4.0, 4.0, 4.0], fade=0.25)
        self.assertIsNotNone(built)
        filt, total = built
        self.assertAlmostEqual(total, 11.5)
        self.assertIn("offset=3.750", filt)
        self.assertIn("offset=7.500", filt)
        self.assertIn("[vout]", filt)
        self.assertIn("acrossfade=d=0.250[aout]", filt)

    def test_xfade_filter_impossible(self):
        self.assertIsNone(jarvis.build_xfade_filter([1.0]))
        self.assertIsNone(jarvis.build_xfade_filter([0.1, 0.1]))
        self.assertIsNone(jarvis.build_xfade_filter([]))

    def test_xfade_args(self):
        args = jarvis.build_xfade_args("ffmpeg", ["a.mp4", "b.mp4"], [3.0, 3.0], "out.mp4")
        self.assertIsNotNone(args)
        self.assertIn("-filter_complex", args)
        self.assertEqual(args[-1], "out.mp4")
        self.assertIsNone(jarvis.build_xfade_args("ffmpeg", ["a.mp4"], [3.0], "out.mp4"))

    def test_ffmpeg_path_none_when_missing(self):
        with mock.patch.object(jarvis, "_imageio_ffmpeg", None), \
                mock.patch.object(jarvis.shutil, "which", lambda _x: None):
            self.assertIsNone(jarvis.ffmpeg_exe_path())


class TestVideoActionAndConfig(unittest.TestCase):
    def test_parse_action_video(self):
        act = jarvis.parse_action("動画: 朝の準備の仕方")
        self.assertEqual(act["type"], "make_video")
        self.assertEqual(act["theme"], "朝の準備の仕方")

    def test_video_line_not_spoken(self):
        self.assertEqual(jarvis.sanitize_for_speech("動画: 朝の準備"), "")

    def test_config_default_video_tate(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = jarvis.load_config(os.path.join(d, "config.json"))
            self.assertTrue(cfg["video_tate"])

    def test_script_prompt_shape(self):
        p = jarvis.build_video_script_prompt("宇宙の話")
        self.assertIn("宇宙の話", p)
        self.assertIn("タイトル", jarvis.VIDEO_SCRIPT_SYSTEM)
        self.assertIn("---場面---", jarvis.VIDEO_SCRIPT_SYSTEM)
        self.assertIn("ナレーション", jarvis.VIDEO_SCRIPT_SYSTEM)


class TestRenderFrames(unittest.TestCase):
    SCENE = {"title": "朝の準備", "bullets": ["顔を洗う", "朝ごはん"],
             "narration": "朝は顔を洗いましょう。", "motif": "checklist", "index": 0}

    def test_frame_sizes_and_levels(self):
        for level in (1, 2, 3):
            img = jarvis.render_video_frame(self.SCENE, level, "朝は顔を洗いましょう。",
                                            (270, 480), 999)
            self.assertEqual(img.size, (270, 480))
            self.assertEqual(img.mode, "RGB")

    def test_frame_landscape(self):
        img = jarvis.render_video_frame(self.SCENE, 3, "字幕です。", (640, 360), 5)
        self.assertEqual(img.size, (640, 360))

    def test_frame_deterministic(self):
        a = jarvis.render_video_frame(self.SCENE, 3, "文です。", (270, 480), 42)
        b = jarvis.render_video_frame(self.SCENE, 3, "文です。", (270, 480), 42)
        self.assertEqual(a.tobytes(), b.tobytes())

    def test_frame_seed_changes_picture(self):
        a = jarvis.render_video_frame(self.SCENE, 3, "文です。", (270, 480), 42)
        b = jarvis.render_video_frame(self.SCENE, 3, "文です。", (270, 480), 43)
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_all_motifs_render(self):
        for name in jarvis.MOTIF_NAMES:
            scene = dict(self.SCENE, motif=name)
            img = jarvis.render_video_frame(scene, 3, "テストです。", (270, 480), 7)
            self.assertEqual(img.size, (270, 480), name)

    def test_unknown_motif_falls_back(self):
        scene = dict(self.SCENE, motif="sonzai_shinai")
        img = jarvis.render_video_frame(scene, 3, "テストです。", (270, 480), 7)
        self.assertEqual(img.size, (270, 480))


if __name__ == "__main__":
    unittest.main()
