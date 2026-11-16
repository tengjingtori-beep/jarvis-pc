# -*- coding: utf-8 -*-
"""jarvis.py (v2) の単体テスト。GUI・音・マイクは使わず、純粋ロジックだけを確かめる。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import jarvis  # noqa: E402


class TestStripThink(unittest.TestCase):
    def test_removes_think_block(self):
        self.assertEqual(jarvis.strip_think("<think>考え中</think>答え"), "答え")

    def test_removes_unclosed_think(self):
        self.assertEqual(jarvis.strip_think("答え<think>考え中…"), "答え")

    def test_removes_eos(self):
        self.assertEqual(jarvis.strip_think("答え</s>"), "答え")

    def test_empty(self):
        self.assertEqual(jarvis.strip_think(""), "")
        self.assertEqual(jarvis.strip_think(None), "")


class TestParseActionV1(unittest.TestCase):
    def test_create_file_multiline_body(self):
        act = jarvis.parse_action("ファイル作成: メモ.txt\n一行目\n二行目")
        self.assertEqual(act["type"], "create_file")
        self.assertEqual(act["name"], "メモ.txt")
        self.assertEqual(act["body"], "一行目\n二行目")

    def test_fullwidth_colon(self):
        act = jarvis.parse_action("開く： メモ.txt")
        self.assertEqual(act["type"], "open")
        self.assertEqual(act["name"], "メモ.txt")

    def test_action_after_chatter_line(self):
        act = jarvis.parse_action("はい、作りますね。\nファイル作成: a.txt\n中身です")
        self.assertEqual(act["type"], "create_file")
        self.assertEqual(act["name"], "a.txt")

    def test_command(self):
        act = jarvis.parse_action("コマンド: dir /b")
        self.assertEqual(act["type"], "command")
        self.assertEqual(act["cmd"], "dir /b")

    def test_circle_and_line(self):
        act = jarvis.parse_action("丸: 25% 75%")
        self.assertEqual((act["type"], act["x"], act["y"]), ("circle", 25.0, 75.0))
        act = jarvis.parse_action("線: 10 20 30 40")
        self.assertEqual(act["type"], "line")
        self.assertEqual((act["x1"], act["y1"], act["x2"], act["y2"]),
                         (10.0, 20.0, 30.0, 40.0))

    def test_done_and_text(self):
        act = jarvis.parse_action("おわり: メモを作りました")
        self.assertEqual(act["type"], "done")
        self.assertEqual(act["summary"], "メモを作りました")
        act = jarvis.parse_action("今日はいい天気ですね。")
        self.assertEqual(act["type"], "text")

    def test_think_content_is_not_an_action(self):
        act = jarvis.parse_action("<think>ファイル作成: x.txt</think>おわり: 完了です")
        self.assertEqual(act["type"], "done")


class TestParseActionPC(unittest.TestCase):
    def test_click_family(self):
        act = jarvis.parse_action("クリック: 50 50")
        self.assertEqual(act["type"], "click")
        self.assertEqual((act["x"], act["y"]), (50.0, 50.0))
        self.assertEqual(jarvis.parse_action("ダブルクリック: 10 20")["type"], "double_click")
        self.assertEqual(jarvis.parse_action("右クリック: 10 20")["type"], "right_click")

    def test_move(self):
        act = jarvis.parse_action("移動: 30 40")
        self.assertEqual(act["type"], "move")
        self.assertEqual((act["x"], act["y"]), (30.0, 40.0))

    def test_drag(self):
        act = jarvis.parse_action("ドラッグ: 10 20 80 90")
        self.assertEqual(act["type"], "drag")
        self.assertEqual((act["x1"], act["y1"], act["x2"], act["y2"]),
                         (10.0, 20.0, 80.0, 90.0))

    def test_key(self):
        act = jarvis.parse_action("キー: ctrl+c")
        self.assertEqual(act["type"], "key")
        self.assertEqual(act["spec"], "ctrl+c")

    def test_type_text(self):
        act = jarvis.parse_action("入力: こんにちは")
        self.assertEqual(act["type"], "type_text")
        self.assertEqual(act["text"], "こんにちは")

    def test_scroll(self):
        self.assertEqual(jarvis.parse_action("スクロール: -3")["amount"], -3)
        self.assertEqual(jarvis.parse_action("スクロール: 5")["amount"], 5)
        self.assertEqual(jarvis.parse_action("スクロール: 99999")["amount"], 1200)

    def test_wait_clamped(self):
        self.assertEqual(jarvis.parse_action("待つ: 3")["seconds"], 3.0)
        self.assertEqual(jarvis.parse_action("待つ: 99")["seconds"], 10.0)

    def test_window_actions(self):
        for line, kind in (("前面: メモ帳", "front"), ("閉じる: メモ帳", "close_window"),
                           ("最大化: メモ帳", "maximize"), ("最小化: メモ帳", "minimize")):
            act = jarvis.parse_action(line)
            self.assertEqual(act["type"], kind, line)
            self.assertEqual(act["title"], "メモ帳", line)

    def test_click_name(self):
        act = jarvis.parse_action("名前をクリック: 送信")
        self.assertEqual(act["type"], "click_name")
        self.assertEqual(act["name"], "送信")

    def test_brightness_clamped(self):
        self.assertEqual(jarvis.parse_action("明るさ: 70")["level"], 70)
        self.assertEqual(jarvis.parse_action("明るさ: 150")["level"], 100)
        self.assertEqual(jarvis.parse_action("明るさ: -5")["level"], 0)

    def test_screenshot(self):
        act = jarvis.parse_action("スクショ: 今の画面")
        self.assertEqual(act["type"], "screenshot")
        self.assertEqual(act["name"], "今の画面")
        act = jarvis.parse_action("スクショ:")
        self.assertEqual(act["name"], "スクショ.png")

    def test_see_and_see_click_order(self):
        act = jarvis.parse_action("見て: この画面を説明して")
        self.assertEqual(act["type"], "see")
        self.assertEqual(act["question"], "この画面を説明して")
        act = jarvis.parse_action("見てクリック: 送信ボタン")
        self.assertEqual(act["type"], "see_click")
        self.assertEqual(act["name"], "送信ボタン")

    def test_page(self):
        act = jarvis.parse_action("ページ: 要約して")
        self.assertEqual(act["type"], "page")
        self.assertEqual(act["question"], "要約して")


class TestKeySpec(unittest.TestCase):
    def test_single_keys(self):
        self.assertEqual(jarvis.key_spec_to_vk("enter"), [0x0D])
        self.assertEqual(jarvis.key_spec_to_vk("esc"), [0x1B])
        self.assertEqual(jarvis.key_spec_to_vk("win"), [0x5B])

    def test_combos(self):
        self.assertEqual(jarvis.key_spec_to_vk("ctrl+c"), [0x11, 0x43])
        self.assertEqual(jarvis.key_spec_to_vk("alt+f4"), [0x12, 0x73])
        self.assertEqual(jarvis.key_spec_to_vk("ctrl+shift+esc"), [0x11, 0x10, 0x1B])

    def test_media_keys(self):
        self.assertEqual(jarvis.key_spec_to_vk("volume_up"), [0xAF])
        self.assertEqual(jarvis.key_spec_to_vk("volume_down"), [0xAE])
        self.assertEqual(jarvis.key_spec_to_vk("mute"), [0xAD])
        self.assertEqual(jarvis.key_spec_to_vk("next"), [0xB0])
        self.assertEqual(jarvis.key_spec_to_vk("prev"), [0xB1])
        self.assertEqual(jarvis.key_spec_to_vk("play_pause"), [0xB3])

    def test_unknown_and_empty(self):
        self.assertIsNone(jarvis.key_spec_to_vk("banana"))
        self.assertIsNone(jarvis.key_spec_to_vk(""))
        self.assertIsNone(jarvis.key_spec_to_vk(None))
        self.assertIsNone(jarvis.key_spec_to_vk("ctrl+banana"))


class TestShoppingGuard(unittest.TestCase):
    def test_japanese(self):
        for s in ["これを購入する", "注文を確定", "支払い方法", "決済する",
                  "カートに入れる", "チェックアウト", "レジに進む", "本を買う"]:
            self.assertTrue(jarvis.needs_shopping_confirm(s), s)

    def test_english(self):
        for s in ["buy now", "Checkout", "PAY", "place your Order"]:
            self.assertTrue(jarvis.needs_shopping_confirm(s), s)

    def test_safe(self):
        for s in ["メモを作る", "設定を開く", "dir /b", ""]:
            self.assertFalse(jarvis.needs_shopping_confirm(s), s)
        self.assertFalse(jarvis.needs_shopping_confirm())
        self.assertFalse(jarvis.needs_shopping_confirm(None))


class TestDestructiveGuard(unittest.TestCase):
    def test_positives(self):
        for s in ["shutdown /s /t 0", "shutdown -r", "Restart-Computer", "Stop-Computer",
                  "logoff", "format D:", "del /s /q C:\\temp", "rd /s /q folder",
                  "rmdir /s x", "Remove-Item -Recurse C:\\x", "rm -rf /", "rm -fr /"]:
            self.assertTrue(jarvis.needs_destructive_confirm(s), s)

    def test_negatives(self):
        for s in ["dir /b", "dir /s /b", "echo hello", "del memo.txt",
                  "format", "メモを作る", "アプリ: notepad", ""]:
            self.assertFalse(jarvis.needs_destructive_confirm(s), s)
        self.assertFalse(jarvis.needs_destructive_confirm())
        self.assertFalse(jarvis.needs_destructive_confirm(None))


class TestSarashinaPrompt(unittest.TestCase):
    def test_system_and_user(self):
        p = jarvis.build_sarashina_prompt("SYS", [("user", "こんにちは")])
        self.assertEqual(p, "<|system|>SYS</s><|user|>こんにちは</s><|assistant|>")

    def test_turns(self):
        p = jarvis.build_sarashina_prompt(
            "SYS", [("user", "やあ"), ("assistant", "はい"), ("user", "元気？")])
        self.assertEqual(
            p, "<|system|>SYS</s><|user|>やあ</s><|assistant|>はい</s>"
               "<|user|>元気？</s><|assistant|>")


class TestPathsAndConfig(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(jarvis.sanitize_relpath("../evil.txt"), "evil.txt")
        self.assertEqual(jarvis.sanitize_relpath("C:\\temp\\x.txt"), "temp/x.txt")
        self.assertEqual(jarvis.sanitize_relpath(""), "無題.txt")

    def test_config_defaults_v2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = jarvis.load_config(os.path.join(d, "config.json"))
            self.assertEqual(cfg["speaker_id"], 11)
            self.assertEqual(cfg["mmproj_path"], "")
            self.assertTrue(cfg["listen_on"])
            self.assertFalse(cfg["autostart"])

    def test_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            cfg = jarvis.load_config(path)
            cfg["mmproj_path"] = "C:/models/mmproj.gguf"
            cfg["listen_on"] = False
            self.assertTrue(jarvis.save_config(path, cfg))
            again = jarvis.load_config(path)
            self.assertEqual(again["mmproj_path"], "C:/models/mmproj.gguf")
            self.assertFalse(again["listen_on"])


class TestSanitizeForSpeech(unittest.TestCase):
    def test_plain_text_passes(self):
        self.assertEqual(jarvis.sanitize_for_speech("こんにちは。元気ですか。"),
                         "こんにちは。元気ですか。")

    def test_think_removed(self):
        self.assertEqual(jarvis.sanitize_for_speech("<think>考え</think>答えです。"),
                         "答えです。")

    def test_code_fence_removed(self):
        out = jarvis.sanitize_for_speech("答えです。\n```python\nprint(1)\n```\n以上です。")
        self.assertNotIn("print", out)
        self.assertIn("答えです", out)
        self.assertIn("以上です", out)

    def test_inline_code_removed(self):
        out = jarvis.sanitize_for_speech("`dir` を実行しました。")
        self.assertNotIn("dir", out)
        self.assertIn("実行しました", out)

    def test_action_lines_removed(self):
        out = jarvis.sanitize_for_speech("やります。\nコマンド: dir /b\n終わりました。")
        self.assertNotIn("dir", out)
        self.assertNotIn("コマンド", out)
        self.assertIn("やります", out)

    def test_done_summary_kept(self):
        out = jarvis.sanitize_for_speech("おわり: メモを作りました")
        self.assertIn("メモを作りました", out)

    def test_url_removed(self):
        out = jarvis.sanitize_for_speech("詳しくは https://example.com/x を見てください。")
        self.assertNotIn("example.com", out)
        self.assertIn("見てください", out)

    def test_path_and_filename_removed(self):
        out = jarvis.sanitize_for_speech("C:\\Users\\tora\\memo.txt に保存しました。")
        self.assertNotIn("C:", out)
        self.assertNotIn("memo.txt", out)
        self.assertIn("保存しました", out)

    def test_markdown_and_emoji_removed(self):
        out = jarvis.sanitize_for_speech("## 見出し\n**大事**です🎉")
        self.assertNotIn("#", out)
        self.assertNotIn("*", out)
        self.assertNotIn("🎉", out)
        self.assertIn("大事です", out)

    def test_sentence_cap(self):
        out = jarvis.sanitize_for_speech("一です。二です。三です。四です。五です。")
        self.assertIn("一です", out)
        self.assertIn("三です", out)
        self.assertNotIn("四です", out)

    def test_char_cap(self):
        out = jarvis.sanitize_for_speech("あ" * 300 + "。")
        self.assertLessEqual(len(out), 180)

    def test_nothing_speakable(self):
        self.assertEqual(jarvis.sanitize_for_speech("コマンド: dir"), "")
        self.assertEqual(jarvis.sanitize_for_speech(""), "")
        self.assertEqual(jarvis.sanitize_for_speech(None), "")
        self.assertEqual(jarvis.sanitize_for_speech("```\nonly code\n```"), "")


class TestListenStateMachine(unittest.TestCase):
    def test_message_flow(self):
        m = jarvis.ListenStateMachine()
        act = m.feed("メモを作って")
        self.assertEqual(act["action"], "message")
        self.assertIn("メモを作って", act["text"])
        self.assertEqual(m.mode, jarvis.ListenStateMachine.LISTENING)

    def test_wake_prefix_stripped(self):
        m = jarvis.ListenStateMachine()
        act = m.feed("ジャービス、メモを作って")
        self.assertEqual(act["action"], "message")
        self.assertNotIn("ジャービス", act["text"])
        self.assertIn("メモを作って", act["text"])

    def test_wake_word_alone_ignored(self):
        m = jarvis.ListenStateMachine()
        self.assertEqual(m.feed("ジャービス")["action"], "ignore")

    def test_too_short_and_empty(self):
        m = jarvis.ListenStateMachine()
        self.assertEqual(m.feed("あ")["action"], "ignore")
        self.assertEqual(m.feed("")["action"], "ignore")
        self.assertEqual(m.feed("　")["action"], "ignore")

    def test_quiet_phrases(self):
        for phrase in ["静かにして", "聞き耳を止めて", "聞くのをやめて", "ミュート"]:
            m = jarvis.ListenStateMachine()
            act = m.feed(phrase)
            self.assertEqual(act["action"], "quiet", phrase)
            self.assertEqual(m.mode, jarvis.ListenStateMachine.QUIET, phrase)

    def test_quiet_ignores_random_speech(self):
        m = jarvis.ListenStateMachine()
        m.feed("静かにして")
        act = m.feed("メモを作って")
        self.assertEqual(act["action"], "ignore")
        self.assertEqual(act.get("reason"), "quiet")
        self.assertEqual(m.mode, jarvis.ListenStateMachine.QUIET)

    def test_resume_phrases(self):
        for phrase in ["聞いて", "聞き耳を始めて", "聞いてる？"]:
            m = jarvis.ListenStateMachine()
            m.feed("静かにして")
            act = m.feed(phrase)
            self.assertEqual(act["action"], "resume", phrase)
            self.assertEqual(m.mode, jarvis.ListenStateMachine.LISTENING, phrase)

    def test_task_stop_does_not_mute(self):
        m = jarvis.ListenStateMachine()
        act = m.feed("止めて")
        self.assertEqual(act["action"], "stop_task")
        self.assertEqual(m.mode, jarvis.ListenStateMachine.LISTENING)
        # 止めた後も聞き耳は続いている
        self.assertEqual(m.feed("メモを作って")["action"], "message")

    def test_stop_variants(self):
        for phrase in ["ストップ", "とめて"]:
            m = jarvis.ListenStateMachine()
            self.assertEqual(m.feed(phrase)["action"], "stop_task", phrase)

    def test_echo_guard(self):
        m = jarvis.ListenStateMachine()
        m.set_speaking(True)
        self.assertEqual(m.state(), jarvis.ListenStateMachine.SPEAKING_MUTED)
        self.assertEqual(m.feed("メモを作って")["action"], "ignore")
        # 読み上げ中の「止めて」も自分の声の可能性があるので捨てる
        self.assertEqual(m.feed("止めて")["action"], "ignore")
        m.set_speaking(False, now=100.0)
        # 終了後1秒の余韻ガード
        self.assertEqual(m.feed("メモを作って", now=100.5)["action"], "ignore")
        self.assertEqual(m.state(now=100.5), jarvis.ListenStateMachine.SPEAKING_MUTED)
        # 余韻が過ぎれば聞く
        self.assertEqual(m.feed("メモを作って", now=101.5)["action"], "message")

    def test_full_off_and_start(self):
        m = jarvis.ListenStateMachine()
        m.full_stop()
        self.assertEqual(m.state(), jarvis.ListenStateMachine.FULL_OFF)
        self.assertEqual(m.feed("メモを作って")["action"], "ignore")
        self.assertEqual(m.feed("聞いて")["action"], "ignore")  # 完全停止は声で戻らない
        m.full_start()
        self.assertEqual(m.feed("メモを作って")["action"], "message")


class TestVisionHelpers(unittest.TestCase):
    def test_coords(self):
        self.assertEqual(jarvis.parse_vision_coords("512 384"), (51.2, 38.4))
        self.assertEqual(jarvis.parse_vision_coords("座標は 100, 900 です"), (10.0, 90.0))

    def test_coords_clamped(self):
        self.assertEqual(jarvis.parse_vision_coords("1200 -50"), (100.0, 0.0))

    def test_coords_fail(self):
        self.assertIsNone(jarvis.parse_vision_coords("見つかりませんでした"))
        self.assertIsNone(jarvis.parse_vision_coords("123"))
        self.assertIsNone(jarvis.parse_vision_coords(""))

    def test_messages_shape(self):
        msgs = jarvis.build_vision_messages("これは何", "QUJD")
        self.assertEqual(len(msgs), 1)
        content = msgs[0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "これは何"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(content[1]["image_url"]["url"].endswith("QUJD"))


class TestPageHelpers(unittest.TestCase):
    def test_cleanup_dedupe(self):
        self.assertEqual(jarvis.cleanup_page_text("a\n\n b \na\nc"), "a\nb\nc")

    def test_cleanup_truncate(self):
        out = jarvis.cleanup_page_text("x" * 100, max_chars=10)
        self.assertEqual(len(out), 10)

    def test_parse_helper_output(self):
        raw = '{"is_browser": true, "title": "T", "url": "https://x.jp", "text": "hello"}'
        page = jarvis.parse_page_helper_output(raw)
        self.assertTrue(page["is_browser"])
        self.assertEqual(page["title"], "T")
        self.assertEqual(page["url"], "https://x.jp")
        self.assertEqual(page["text"], "hello")

    def test_parse_helper_output_with_noise(self):
        raw = "警告: 何か\n{\"is_browser\": false, \"title\": \"\", \"url\": \"\", \"text\": \"\"}\n"
        page = jarvis.parse_page_helper_output(raw)
        self.assertIsNotNone(page)
        self.assertFalse(page["is_browser"])

    def test_parse_helper_output_bad(self):
        self.assertIsNone(jarvis.parse_page_helper_output("JSONではありません"))
        self.assertIsNone(jarvis.parse_page_helper_output(""))
        self.assertIsNone(jarvis.parse_page_helper_output(None))

    def test_is_browser_window(self):
        self.assertTrue(jarvis.is_browser_window("タイトル - Google Chrome", ""))
        self.assertTrue(jarvis.is_browser_window("", "Chrome_WidgetWin_1"))
        self.assertTrue(jarvis.is_browser_window("", "MozillaWindowClass"))
        self.assertTrue(jarvis.is_browser_window("何か - Microsoft Edge", ""))
        self.assertFalse(jarvis.is_browser_window("メモ帳", "Notepad"))
        self.assertFalse(jarvis.is_browser_window("", ""))

    def test_build_page_question(self):
        q = jarvis.build_page_question("要約して", "題名X", "https://x.jp", "本文Y")
        self.assertIn("題名X", q)
        self.assertIn("https://x.jp", q)
        self.assertIn("本文Y", q)
        self.assertIn("要約して", q)


class TestCreationRequest(unittest.TestCase):
    def test_positives(self):
        for s in ["ゲームを作って", "メモを作って", "アプリを作ってほしい",
                  "文書を作る", "ページを作って", "プログラムを作って"]:
            self.assertTrue(jarvis.is_creation_request(s), s)

    def test_negatives(self):
        for s in ["今日の天気は？", "メモを開いて", "音量を上げて", ""]:
            self.assertFalse(jarvis.is_creation_request(s), s)


class TestNoConsoleWindows(unittest.TestCase):
    def test_create_no_window_value(self):
        self.assertEqual(jarvis.CREATE_NO_WINDOW, 0x08000000)
        self.assertTrue(callable(jarvis.run_hidden))
        self.assertTrue(callable(jarvis.hidden_popen))

    def test_uia_helper_has_page_mode(self):
        self.assertIn('"page"', jarvis.UIA_HELPER_PS1)
        self.assertIn("GetForegroundWindow", jarvis.UIA_HELPER_PS1)


if __name__ == "__main__":
    unittest.main()
