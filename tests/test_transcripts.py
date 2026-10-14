"""Bootstrap from transcripts: counts only, never content."""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from ctempo import commands, profile, transcripts

from .helpers import MON, H, D, TempHome

SECRET = "TOP-SECRET-client-figures-4242"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def assistant(ts, mid, out=100, content=SECRET):
    return json.dumps({
        "parentUuid": "p", "type": "assistant", "timestamp": iso(ts), "requestId": "req_" + mid,
        "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": content}],
                    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 1000, "output_tokens": out}},
    })


class Bootstrap(TempHome):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ctempo-tr-")
        proj = os.path.join(self.root, "-Users-me-work")
        os.makedirs(os.path.join(proj, "sess", "subagents"))
        self.now = MON + 28 * D
        lines = []
        # Four weeks of Tuesdays 10:00-12:00, three messages an hour.
        for wk in range(4):
            for h in (10, 11):
                for k in range(3):
                    ts = MON + wk * 7 * D + D + h * H + k * 600
                    lines.append(assistant(ts, "msg_%d_%d_%d" % (wk, h, k)))
        # A message streamed over two lines (same id, growing usage).
        lines.append(assistant(MON + D + 10 * H + 50, "msg_stream", out=10))
        lines.append(assistant(MON + D + 10 * H + 55, "msg_stream", out=500))
        lines.append(json.dumps({"type": "user", "timestamp": iso(MON + D + 10 * H), "message": {"content": SECRET}}))
        lines.append('{"type":"assistant", "usage": broken json ' + SECRET)
        with open(os.path.join(proj, "sess.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(proj, "sess", "subagents", "agent-1.jsonl"), "w") as f:
            f.write(assistant(MON + 8 * D + 11 * H, "msg_sub") + "\n")
        old = os.path.join(proj, "old.jsonl")
        with open(old, "w") as f:
            f.write(assistant(MON - 200 * D, "msg_old") + "\n")
        os.utime(old, (MON - 200 * D, MON - 200 * D))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def test_prior_from_transcripts_and_no_content_leaks(self):
        buf = io.StringIO()
        args = SimpleNamespace(days=45, root=self.root, dry_run=False)
        orig = time.time
        time.time = lambda: self.now
        try:
            with redirect_stdout(buf):
                rc = commands.cmd_bootstrap(args, self.cfg)
        finally:
            time.time = orig
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn(SECRET, out)
        self.assertIn("Assistant messages counted: 26", out)  # 24 + 1 stream + 1 subagent
        self.assertIn("1 skipped", out)
        self.st.close()
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name), "rb") as f:
                self.assertNotIn(SECRET.encode(), f.read(), name)
        from ctempo.store import Store

        self.st = Store(os.path.join(self.dir, "tempo.db"))
        prof = profile.load(self.st)
        self.assertEqual(len(prof.prior), 168)
        tue10, tue11, tue3 = prof.prior[24 + 10], prof.prior[24 + 11], prof.prior[24 + 3]
        # 28 days covered: trusted fully (14 days or more).
        self.assertEqual(prof.prior_meta["trust"], 1.0)
        self.assertGreater(tue10, 0.25)
        self.assertGreater(tue11, 0.25)
        self.assertLess(tue3, 0.001)
        self.assertEqual(prof.prior_meta["messages"], 26)

    def test_hostile_lines_and_a_fifo_do_not_stop_the_scan(self):
        proj = os.path.join(self.root, "-Users-me-work")
        with open(os.path.join(proj, "weird.jsonl"), "w") as f:
            base = json.loads(assistant(MON + D + 10 * H, "x"))
            for bad in ({"id": ["a"]}, {"id": {"k": 1}}):
                d = json.loads(json.dumps(base))
                d["message"].update(bad)
                f.write(json.dumps(d) + "\n")
            d = json.loads(json.dumps(base))
            d["message"]["usage"]["input_tokens"] = 10 ** 400
            d["message"]["id"] = "msg_huge"
            f.write(json.dumps(d) + "\n")
            f.write('{"type":"assistant","usage":' + "[" * 100000 + "]" * 100000 + "}\n")
        os.mkfifo(os.path.join(proj, "pipe.jsonl"))
        t = time.time()
        events, stats = transcripts.scan(self.root, MON - D, self.now)
        self.assertLess(time.time() - t, 5)
        self.assertGreaterEqual(stats["bad_lines"], 2)
        self.assertEqual(stats["messages"], 27)  # 26 good + msg_huge (its huge count ignored)

    def test_cost_weight_and_stream_dedupe(self):
        events, stats = transcripts.scan(self.root, MON - D, self.now)
        self.assertEqual(stats["duplicates"], 1)
        weights = sorted(w for _, w in events)
        # 10 input + 0.1 * 1000 cache reads + 5 * 100 output = 610; the stream keeps out=500.
        self.assertAlmostEqual(weights[0], 610.0)
        self.assertAlmostEqual(weights[-1], 10 + 100 + 2500)
        self.assertEqual(stats["bad_lines"], 1)


class ShortHistory(unittest.TestCase):
    def test_an_hour_of_transcripts_stays_near_uniform(self):
        now = MON + 10 * H
        events = [(MON + 9 * H + k * 60, 100.0) for k in range(30)]
        prior, meta = transcripts.build_prior(events, now - 45 * D, now, 14)
        self.assertLess(meta["trust"], 0.01)
        self.assertAlmostEqual(sum(prior), 1.0)
        self.assertLess(max(prior) / min(prior), 2.0)

    def test_a_stray_old_message_does_not_buy_trust(self):
        now = MON + 3 * D
        events = [(MON - 30 * D, 50.0)]  # one small message a month ago
        events += [(MON + d * D + h * H + k * 600, 100.0) for d in (0, 1) for h in range(9, 18) for k in range(6)]
        prior, meta = transcripts.build_prior(events, now - 45 * D, now, 14)
        self.assertLessEqual(meta["trust"], 0.5)  # two active days of four
        self.assertEqual(meta["active_days"], 3)


if __name__ == "__main__":
    unittest.main()
