import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from evaluateandscore.server import DIMENSIONS, Survey, SurveyError, byte_range


class SurveyTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.survey = Survey(self.root)
        for name in ("raw/原声.wav", "test/模型一.wav", "test/模型二.wav"):
            (self.survey.datasets / name).write_bytes(b"sample")
        self.ip = "192.168.1.22"

    def answers(self, session):
        return {
            sample["id"]: dict.fromkeys(DIMENSIONS, 4)
            for question in session["questions"] for sample in question
        }

    def test_blind_draw_and_multiple_attempts_from_one_ip(self):
        self.survey = Survey(self.root, questions=3)
        for _ in range(2):
            session = self.survey.start(self.ip)
            self.assertNotIn(".wav", json.dumps(session))
            self.assertIsNotNone(session["reference_url"])
            answers = self.answers(session)
            # Concurrent duplicate submissions produce one receipt and one file.
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(
                    lambda _: self.survey.submit(session["id"], self.ip, answers), range(2)
                ))
            self.assertEqual(receipts[0], receipts[1])
            self.assertEqual(self.survey.status(session["id"], self.ip), receipts[0])
            result = json.loads((self.survey.results / (session["id"] + ".json")).read_text("utf-8"))
            self.assertEqual(result["participant_ip"], self.ip)
            self.assertEqual(result["reference_file"], "raw/原声.wav")
            self.assertEqual(len(result["questions"]), 3)
            for question in result["questions"]:
                self.assertCountEqual([s["file"] for s in question["samples"]],
                                      ["test/模型一.wav", "test/模型二.wav"])
                for sample in question["samples"]:
                    self.assertEqual(sample["source"], "clone")
                    self.assertEqual(sample["scores"], answers[sample["sample_id"]])
                    self.assertEqual(sample["feedback"], "")
                    self.assertEqual(sample["authenticity_mean"], 4)
        self.assertEqual(len(list(self.survey.results.glob("*.json"))), 2)

    def test_feedback_is_saved_per_sample_occurrence(self):
        survey = Survey(self.root, questions=2)
        session = survey.start(self.ip)
        answers = self.answers(session)
        comments = {}
        for number, question in enumerate(session["questions"], 1):
            sample_id = question[0]["id"]
            comments[sample_id] = f"第 {number} 题的听感\n句尾很自然 🎧"
            answers[sample_id]["feedback"] = comments[sample_id]
        survey.submit(session["id"], self.ip, answers)
        result = json.loads((survey.results / (session["id"] + ".json")).read_text("utf-8"))
        for question in result["questions"]:
            for sample in question["samples"]:
                self.assertEqual(sample["feedback"], comments.get(sample["sample_id"], ""))
                self.assertEqual(sample["scores"], dict.fromkeys(DIMENSIONS, 4))
                self.assertEqual(sample["authenticity_mean"], 4)

    def test_invalid_scores_and_wrong_ip_leave_no_result(self):
        session = self.survey.start(self.ip)
        key = session["id"]
        with self.assertRaises(SurveyError):
            self.survey.submit(key, "192.168.1.99", self.answers(session))
        with self.assertRaises(SurveyError):
            self.survey.submit(key, self.ip, {})
        for invalid in (0, 6, True, 2.5, "5"):
            scores = self.answers(session)
            next(iter(scores.values()))["timbre"] = invalid
            with self.assertRaises(SurveyError):
                self.survey.submit(key, self.ip, scores)
        for invalid_feedback in (5, None, ["反馈"], "\ud800"):
            answers = self.answers(session)
            next(iter(answers.values()))["feedback"] = invalid_feedback
            with self.assertRaises(SurveyError):
                self.survey.submit(key, self.ip, answers)
        self.assertFalse(list(self.survey.results.iterdir()))

    def test_small_or_empty_pool_can_be_updated_at_runtime(self):
        for group in self.survey.inventory().values():
            for path in group:
                path.unlink()
        self.assertEqual(self.survey.setup()["samples"], 0)
        with self.assertRaises(SurveyError):
            self.survey.start(self.ip)
        (self.survey.datasets / "test/单段.WAV").touch()
        session = self.survey.start(self.ip)
        self.assertIsNone(session["reference_url"])
        self.assertTrue(all(len(question) == 1 for question in session["questions"]))

    def test_auto_round_scales_with_pool_and_uses_distinct_pairs(self):
        self.assertEqual(self.survey.setup()["questions"], 1)
        for expected in range(3, 8):
            (self.survey.datasets / f"test/录音{expected}.wav").touch()
            self.assertEqual(self.survey.setup()["questions"], expected)
            session = self.survey.start(self.ip)
            files = self.survey.active(session["id"], self.ip).files
            pairs = [frozenset(files[s["id"]] for s in q) for q in session["questions"]]
            self.assertEqual(len(pairs), expected)
            self.assertEqual(len(set(pairs)), expected)
            self.assertTrue(all(len(pair) == 2 for pair in pairs))
            self.assertCountEqual(files.values(), self.survey.inventory()["test"] * 2)

    def test_reference_does_not_fill_empty_or_short_test_pool(self):
        for path in self.survey.inventory()["test"]:
            path.unlink()
        self.assertTrue(self.survey.setup()["has_reference"])
        self.assertEqual(self.survey.setup()["samples"], 0)
        with self.assertRaisesRegex(SurveyError, "datasets/test"):
            self.survey.start(self.ip)

        (self.survey.datasets / "test/唯一克隆.wav").touch()
        self.assertEqual(self.survey.setup()["samples"], 1)
        session = self.survey.start(self.ip)
        self.assertIsNotNone(session["reference_url"])
        self.assertTrue(all(len(question) == 1 for question in session["questions"]))

    def test_audio_ranges(self):
        self.assertEqual(byte_range("bytes=4-8", 10), (4, 8))
        self.assertEqual(byte_range("bytes=4-", 10), (4, 9))
        self.assertEqual(byte_range("bytes=-2", 10), (8, 9))
        self.assertIsNone(byte_range("bytes=10-", 10))
        self.assertIsNone(byte_range("bytes=-0", 10))
