# -*- coding: utf-8 -*-
"""Tests without API calls:  venv\\Scripts\\python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lyricvideo as lv  # noqa: E402

CFG = lv.deep_merge(lv.DEFAULT_CONFIG, {})


def W(text, start, end, loss=0.1):
    return {"text": text, "start": start, "end": end, "loss": loss}


SONGTEXT = """[Intro]

Don't you wait-for me, it's 1999
Rock-n-Roll forever – yeah!

[Chorus]
Grüße aus München, 24/7
Ooh…
"""

# Response in the format of the ElevenLabs API (words with text/start/end/loss, characters, loss).
# Deliberately with special cases: whitespace entries, punctuation attached to words, "Rock-n-Roll"
# split into three parts and a separate entry just for the dash.
# The German line checks umlauts and other non-ASCII letters.
API_RESPONSE = {
    "characters": [],
    "words": [
        W("Don't", 1.00, 1.30), W(" ", 1.30, 1.31), W("you", 1.31, 1.50), W(" ", 1.5, 1.5),
        W("wait-for", 1.50, 2.00), W("me,", 2.00, 2.30), W("it's", 2.40, 2.60), W("1999", 2.60, 3.50),
        W("Rock", 4.00, 4.20), W("n", 4.20, 4.30), W("Roll", 4.30, 4.60), W("forever", 4.60, 5.40),
        W("–", 5.40, 5.40), W("yeah!", 5.50, 6.00, loss=0.9),
        W("Grüße", 7.00, 7.40), W("aus", 7.40, 7.60), W("München,", 7.60, 8.20), W("24/7", 8.20, 9.00),
        W("Ooh…", 9.10, 9.30),
    ],
    "loss": 0.15,
}


class TestMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.txt = Path(self.tmp.name) / "song.txt"
        self.txt.write_text(SONGTEXT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_lyrics_skips_empty_and_brackets(self):
        lines = lv.parse_lyrics(self.txt)
        self.assertEqual([l.text for l in lines], [
            "Don't you wait-for me, it's 1999", "Rock-n-Roll forever – yeah!", "Grüße aus München, 24/7", "Ooh…"])
        self.assertEqual([l.source_line for l in lines], [3, 4, 7, 8])

    def test_word_counts(self):
        self.assertEqual(len(lv.line_tokens("Don't you wait-for me, it's 1999")), 6)
        self.assertEqual(len(lv.line_tokens("Rock-n-Roll forever – yeah!")), 3)  # the dash does not count
        self.assertEqual(lv.norm("Don't"), "dont")
        self.assertEqual(lv.norm("München,"), "münchen")
        self.assertEqual(lv.norm("München"), "münchen")  # decomposed umlaut

    def test_mapping_and_times(self):
        lines = lv.parse_lyrics(self.txt)
        groups = lv.map_words_to_lines(lines, API_RESPONSE["words"])
        # line 2: "Rock-n-Roll" comes as three API words, the dash is ignored
        self.assertEqual([len(g) for g in groups], [6, 5, 4, 1])
        cues, notes = lv.build_cues(lines, groups, CFG)
        self.assertEqual([(c.start, c.end) for c in cues], [(1.0, 3.5), (4.0, 6.0), (7.0, 9.0), (9.1, 9.3)])
        self.assertEqual(cues[1].text, "Rock-n-Roll forever – yeah!")
        self.assertAlmostEqual(cues[1].loss, (0.1 * 4 + 0.9) / 5)
        self.assertEqual(notes, [])

    def test_line_count_unchanged_when_split_differently(self):
        # lines split differently -> the same response still fits (reuse without API)
        self.txt.write_text("Don't you wait-for me,\nit's 1999 Rock-n-Roll\nforever – yeah! Grüße aus München, 24/7 Ooh…",
                            encoding="utf-8")
        lines = lv.parse_lyrics(self.txt)
        groups = lv.map_words_to_lines(lines, API_RESPONSE["words"])
        self.assertEqual([len(g) for g in groups], [4, 5, 7])

    def test_mismatch_missing_word(self):
        words = [w for w in API_RESPONSE["words"] if w["text"] != "forever"]
        with self.assertRaises(lv.AlignmentMismatch) as cm:
            lv.map_words_to_lines(lv.parse_lyrics(self.txt), words)
        msg = str(cm.exception)
        self.assertIn("lyrics line 4", msg)
        self.assertIn("Your lyrics: 14 words, response: 15 words", msg)

    def test_mismatch_extra_words(self):
        words = API_RESPONSE["words"] + [W("encore", 10, 11)]
        with self.assertRaises(lv.AlignmentMismatch) as cm:
            lv.map_words_to_lines(lv.parse_lyrics(self.txt), words)
        self.assertIn("more words", str(cm.exception))
        self.assertIn("encore", str(cm.exception))

    def test_mismatch_different_word(self):
        words = [dict(w) for w in API_RESPONSE["words"]]
        words[0]["text"] = "Do"
        with self.assertRaises(lv.AlignmentMismatch) as cm:
            lv.map_words_to_lines(lv.parse_lyrics(self.txt), words)
        self.assertIn("lyrics line 3", str(cm.exception))

    def test_stretched_first_word_over_intro(self):
        lines = [lv.LyricLine("Once I sang", 1)]
        groups = [[W("Once", 0.0, 13.9), W("I", 14.02, 14.1), W("sang", 14.28, 14.76)]]
        cues, notes = lv.build_cues(lines, groups, CFG)
        self.assertAlmostEqual(cues[0].start, 13.9 - 0.48)  # 4 letters * 0.12 s
        self.assertIn("Once", notes[0])

    def test_overlap_is_trimmed(self):
        cues = [lv.Cue(1.0, 3.2, "a", 1), lv.Cue(3.0, 4.0, "b", 2)]
        notes = lv.fix_overlaps(cues)
        self.assertEqual(cues[0].end, 3.0)
        self.assertEqual(len(notes), 1)


class TestProject(unittest.TestCase):
    def test_safe_project_name(self):
        self.assertEqual(lv.safe_project_name('Midnight: "Drive"?'), "Midnight Drive")
        self.assertEqual(lv.safe_project_name("  Song 1. "), "Song 1")
        self.assertEqual(lv.safe_project_name("con"), "_con")

    def test_pick_file_any_name(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for n in ("My Image.JPG", "whatever.mp3", "text.txt", ".gitkeep", "notes.docx"):
                (d / n).write_bytes(b"x")
            self.assertEqual(lv.pick_file(d, "image").name, "My Image.JPG")
            self.assertEqual(lv.pick_file(d, "audio").name, "whatever.mp3")
            self.assertEqual(lv.pick_file(d, "text").name, "text.txt")

    def test_pick_file_several_asks(self):
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.png").write_bytes(b"x")
            (d / "b.png").write_bytes(b"x")
            old = sys.stdin
            try:
                sys.stdin = io.StringIO("2\n")
                self.assertEqual(lv.pick_file(d, "image").name, "b.png")
            finally:
                sys.stdin = old

    def test_pick_file_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(lv.LyricError):
                lv.pick_file(Path(d), "audio")


class TestSrtAss(unittest.TestCase):
    def test_srt_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.srt"
            cues = [lv.Cue(1.0, 2.5, "Hello world", 1), lv.Cue(3661.123, 3662.0, "Line one\nLine two", 2)]
            lv.write_srt(p, cues)
            back = lv.parse_srt(p)
            self.assertEqual([(c.start, c.end, c.text) for c in back],
                             [(1.0, 2.5, "Hello world"), (3661.123, 3662.0, "Line one\nLine two")])
            self.assertIn("01:01:01,123 --> 01:01:02,000", p.read_text(encoding="utf-8"))

    def test_ass_color_is_bgr(self):
        self.assertEqual(lv.ass_color("#F5E6C8"), "&H00C8E6F5")
        self.assertEqual(lv.ass_color("#FF0000"), "&H000000FF")  # red
        self.assertEqual(lv.ass_color("#0000FF", 0x80), "&H80FF0000")  # blue, semi-transparent

    def test_karaoke_tags(self):
        style = lv.validate_style(dict(lv.BUILTIN_STYLE, background={"dim_percent": 0}), "t")
        cue = lv.Cue(4.0, 6.0, "Rock-n-Roll forever – yeah!", 1)
        words = lv.match_cues_to_words([cue], API_RESPONSE["words"])[0]
        self.assertEqual(len(words), 5)  # Rock, n, Roll, forever, yeah!
        txt = lv.karaoke_text(cue, cue.start, cue.end, words, style)
        self.assertEqual(txt, "{\\k60}Rock-n-Roll {\\k90}forever – {\\k50}yeah!")

    def test_karaoke_follows_shifted_line(self):
        style = lv.validate_style(dict(lv.BUILTIN_STYLE, background={"dim_percent": 0}), "t")
        cue = lv.Cue(4.5, 6.5, "Rock-n-Roll forever – yeah!", 1)  # moved by 0.5 s in the SRT
        words = lv.match_cues_to_words([cue], API_RESPONSE["words"])[0]
        self.assertEqual(lv.karaoke_text(cue, cue.start, cue.end, words, style),
                         "{\\k60}Rock-n-Roll {\\k90}forever – {\\k50}yeah!")

    def test_reveal_word_by_word(self):
        style = lv.validate_style(dict(lv.BUILTIN_STYLE, background={"dim_percent": 0}, word_fade_ms=120), "t")
        cue = lv.Cue(4.0, 6.0, "Rock-n-Roll forever – yeah!", 1)
        words = lv.match_cues_to_words([cue], API_RESPONSE["words"])[0]
        self.assertEqual(lv.reveal_text(cue, cue.start, cue.end, words, style),
                         "{\\alpha&HFF&\\t(0,120,\\alpha&H00&)}Rock-n-Roll "
                         "{\\alpha&HFF&\\t(600,720,\\alpha&H00&)}forever – "
                         "{\\alpha&HFF&\\t(1500,1620,\\alpha&H00&)}yeah!")

    def test_reveal_falls_back_without_alignment(self):
        style = lv.validate_style(dict(lv.BUILTIN_STYLE, background={"dim_percent": 0}, highlight="reveal"), "t")
        _, used = lv.build_ass([lv.Cue(1, 2, "Hello", 1)], style, 1920, 1080, None)
        self.assertEqual(used, "none")

    def test_hold_extends_until_next_line(self):
        cues = [lv.Cue(1.0, 2.0, "a", 1), lv.Cue(2.5, 3.0, "b", 2), lv.Cue(6.0, 7.0, "c", 3)]
        lv.hold_cues(cues, 1.5)
        self.assertEqual([c.end for c in cues], [2.5, 4.5, 8.5])  # never past the start of the next line

    def test_reveal_keeps_real_times_when_only_start_moved(self):
        style = lv.validate_style(dict(lv.BUILTIN_STYLE, background={"dim_percent": 0}, word_fade_ms=0), "t")
        words = [W("Once", 0.0, 13.9), W("I", 14.0, 14.1), W("sang", 14.5, 15.0)]
        cue = lv.Cue(13.5, 15.0, "Once I sang", 1, srt_end=15.0)  # only the start corrected
        self.assertEqual(lv.reveal_text(cue, cue.start, cue.end, words, style),
                         "{\\alpha&H00&}Once {\\alpha&HFF&\\t(500,501,\\alpha&H00&)}I "
                         "{\\alpha&HFF&\\t(1000,1001,\\alpha&H00&)}sang")

    def test_repeated_line_picks_nearest(self):
        words = [W("la", 1, 2), W("la", 2, 3), W("x", 3, 4), W("la", 10, 11), W("la", 11, 12)]
        res = lv.match_cues_to_words([lv.Cue(10.2, 12, "La la", 1)], words)[0]
        self.assertEqual(res[0]["start"], 10)

    def test_style_fallback_and_override(self):
        style, source = lv.resolve_style("grimdark", {"size": 50})
        self.assertEqual(style["size"], 50)
        self.assertEqual(source["size"], "command line")
        self.assertEqual(style["box_color"], "#000000")  # from standard
        self.assertEqual(source["box_color"], "standard")
        self.assertEqual(style["position"], "middle")


if __name__ == "__main__":
    unittest.main()
