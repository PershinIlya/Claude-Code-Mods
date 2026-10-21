"""Charts for Details and the menu bar (ctempo/chart.py, menuart.chart, png lines)."""

import base64
import json
import struct
import unittest
import zlib

from ctempo import band, chart, commands, config, menuart, menubar, profile, statusline
from ctempo.png import Canvas

from .helpers import MON, H, D, TempHome, payload
from .test_band import NOW, STATES

R5 = int(MON + 16 * H + 30 * 60)  # Monday 16:30 UTC: the 5 hours from 11:30
RW = int(MON + 4 * D + 4 * H)  # Friday 04:00 UTC


def pixels(b64):
    """(width, height, [alpha per pixel]) of a PNG from Canvas.png."""
    data = base64.b64decode(b64)
    w, h = struct.unpack(">II", data[16:24])
    i = data.index(b"IDAT")
    n = struct.unpack(">I", data[i - 4:i])[0]
    raw = zlib.decompress(data[i + 4:i + 4 + n])
    stride = 1 + 4 * w
    return w, h, [raw[y * stride + 1 + 4 * x + 3] for y in range(h) for x in range(w)]


class Forecast(unittest.TestCase):
    def test_the_5_hours_go_on_at_their_rate_flat_at_100(self):
        self.assertEqual(chart._line(40.0, 10.0, 0, 2 * H), [[0, 40.0], [2 * H, 60.0]])
        # 40 + 30%/h reaches 100 in 2 hours, then stays there.
        self.assertEqual(chart._line(40.0, 30.0, 0, 3 * H), [[0, 40.0], [2 * H, 100.0], [3 * H, 100.0]])
        self.assertEqual(chart._line(100.0, 30.0, 0, H), [[0, 100.0], [H, 100.0]])
        self.assertEqual(chart._line(40.0, None, 0, H), [])
        self.assertEqual(chart._line(40.0, 10.0, H, H), [])

    def test_the_week_walks_the_profile(self):
        # Work only on Monday 9 to 17 (UTC): the forecast is flat outside it.
        w = [0.0] * profile.BINS
        for h in range(9, 17):
            w[h] = 1.0 / 8
        out = chart._walk(w, 20.0, 40.0, MON + 6 * H, MON + 20 * H)
        at = dict((t, u) for t, u in out)
        self.assertAlmostEqual(at[MON + 9 * H], 20.0)
        self.assertAlmostEqual(at[MON + 13 * H], 40.0)  # half of 40 points over half the hours
        self.assertAlmostEqual(at[MON + 17 * H], 60.0)
        self.assertAlmostEqual(out[-1][1], 60.0)
        self.assertEqual(out[-1][0], MON + 20 * H)
        # Capped at 100, then flat to the end.
        out = chart._walk(w, 90.0, 40.0, MON, MON + D)
        self.assertEqual(out[-1], [MON + D, 100.0])
        self.assertTrue(all(u <= 100.0 for _, u in out))
        self.assertEqual(chart._walk(w, 20.0, None, MON, MON + D), [])

    def test_thinning_keeps_the_ends_and_the_order(self):
        pts = [[MON + 60 * i, float(i)] for i in range(600)]
        out = chart._thin(pts, MON, MON + 600 * 60, 50)
        self.assertLessEqual(len(out), 52)
        self.assertEqual(out[0], pts[0])
        self.assertEqual(out[-1], pts[-1])
        self.assertEqual(out, sorted(out))
        self.assertEqual(chart._thin(pts[:10], MON, MON + 600, 50), pts[:10])


class Ticks(unittest.TestCase):
    def test_hours_for_the_5_hours(self):
        grid, labels = chart._ticks(config.FIVE_HOUR, R5 - 5 * H, R5)
        self.assertEqual(grid, [int(MON + h * H) for h in (12, 13, 14, 15, 16)])
        self.assertEqual([t for _, t in labels], ["12", "13", "14", "15", "16"])
        # A window opened on the hour labels its ends too.
        grid, labels = chart._ticks(config.FIVE_HOUR, MON + 12 * H, MON + 17 * H)
        self.assertEqual(grid, [int(MON + h * H) for h in (13, 14, 15, 16)])
        self.assertEqual([t for _, t in labels], ["12", "13", "14", "15", "16", "17"])

    def test_midnights_and_day_names_for_the_week(self):
        grid, labels = chart._ticks(config.SEVEN_DAY, RW - 7 * D, RW)
        self.assertEqual(grid, [int(MON + d * D) for d in range(-2, 4 + 1)])
        # Fri 04:00 to Sat 00:00 is a day's label; Fri 00:00 to 04:00 is too short for one.
        self.assertEqual([t for _, t in labels], ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])
        self.assertEqual(labels[3][0], int(MON + 12 * H))


class Snapshots(TempHome):
    def v(self, **kw):
        v = dict(window=config.FIVE_HOUR, present=True, window_id=7, used=30.0, rate=12.0, verdict="on_pace")
        v.update(kw)
        return v

    def log(self, window=config.FIVE_HOUR):
        return (self.st.meta_get("forecasts:" + window) or {}).get("log", [])

    def test_one_every_15_minutes_for_the_5_hours(self):
        self.assertTrue(chart.snapshot(self.st, self.v(), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(used=31.0), NOW + 14 * 60))
        self.assertTrue(chart.snapshot(self.st, self.v(used=33.0), NOW + 15 * 60))
        self.assertEqual(self.log(), [[int(NOW), 30.0, 12.0], [int(NOW + 15 * 60), 33.0, 12.0]])

    def test_a_new_window_starts_over_and_the_last_ones_are_kept(self):
        for k in range(chart.KEEP + 5):
            chart.snapshot(self.st, self.v(), NOW + k * 900)
        self.assertEqual(len(self.log()), chart.KEEP)
        self.assertEqual(self.log()[-1][0], int(NOW + (chart.KEEP + 4) * 900))
        chart.snapshot(self.st, self.v(window_id=8), NOW + 100 * 900)
        self.assertEqual(len(self.log()), 1)

    def test_nothing_to_keep_without_a_forecast(self):
        self.assertFalse(chart.snapshot(self.st, self.v(verdict="maxed"), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(verdict="unknown"), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(rate=None), NOW))
        self.assertFalse(chart.snapshot(self.st, {"window": config.FIVE_HOUR, "present": False}, NOW))
        # The week keeps its percent per share, one every 2 hours.
        wk = self.v(window=config.SEVEN_DAY, rate=None, _path_k=55.0)
        self.assertTrue(chart.snapshot(self.st, wk, NOW))
        self.assertFalse(chart.snapshot(self.st, wk, NOW + 2 * H - 60))
        self.assertTrue(chart.snapshot(self.st, wk, NOW + 2 * H))
        self.assertEqual(self.log(config.SEVEN_DAY)[0], [int(NOW), 30.0, 55.0])


class Build(TempHome):
    def work(self, until, every=600, step=1.0, start=11 * H + 40 * 60):
        """5-hour readings from 11:40 rising `step` a reading, the week fixed."""
        self.known("s1")
        t, u = MON + start, 0.0
        while t <= until:
            u += step
            self.feed(t, five=(int(u), R5), week=(30, RW))
            t += every
        return int(u)

    def views(self, now):
        v5, vw, _ = commands.views(self.st, self.cfg, now)
        return v5, vw

    def test_used_and_the_forecast_to_the_reset(self):
        now = MON + 14 * H + 40 * 60
        self.work(now)
        v5, vw = self.views(now)
        c = chart.build(self.st, v5, now)
        self.assertEqual((c["t0"], c["t1"], c["now"]), (R5 - 5 * 3600, R5, int(now)))
        self.assertEqual(c["actual"][0], [int(MON + 11 * H + 30 * 60), 0.0])  # the window's start at 0
        self.assertEqual(c["actual"][-1], [int(now), v5["used"]])
        self.assertEqual(c["actual"], sorted(c["actual"]))
        self.assertEqual(c["forecast"][0], [int(now), v5["used"]])
        self.assertEqual(c["forecast"][-1][0], R5)
        self.assertAlmostEqual(c["forecast"][-1][1], v5["projected"], places=1)
        self.assertIsNone(c["fan"])  # a fitted rate, no early estimate
        self.assertEqual(c["ghosts"], [])  # no snapshot yet
        self.assertEqual([t for _, t in c["labels"]], ["12", "13", "14", "15", "16"])
        w = chart.build(self.st, vw, now)
        self.assertEqual(w["t1"], RW)
        self.assertAlmostEqual(w["forecast"][-1][1], vw["projected"], places=0)
        json.dumps(c)  # plain data for the band's JSON

    def test_an_early_estimate_has_a_fan(self):
        now = MON + 11 * H + 52 * 60
        self.work(now, step=2.0)  # 2 readings: fewer than rate_min_samples
        v5, _ = self.views(now)
        self.assertEqual(v5["rate_source"], "interpolated")
        c = chart.build(self.st, v5, now)
        lo, hi = c["fan"]
        self.assertLess(lo, c["forecast"][-1][1])
        self.assertGreater(hi, c["forecast"][-1][1])
        self.assertGreaterEqual(lo, v5["used"])
        self.assertLessEqual(hi, 100.0)

    def test_an_hour_old_forecast_is_a_ghost(self):
        t, now = MON + 13 * H, MON + 14 * H + 40 * 60
        self.work(t)
        for v in self.views(t):
            chart.snapshot(self.st, v, t)  # 1 h 40 min ago: too far from an hour, not drawn
        for k in range(1, 7):
            self.work(t + k * 900, start=t + (k - 1) * 900 + 600)  # one snapshot per 15 min
        t2 = MON + 13 * H + 40 * 60
        v5, _ = self.views(t2)
        log = self.st.meta_get("forecasts:" + config.FIVE_HOUR)["log"]
        log.append([int(t2), round(v5["used"], 2), round(v5["rate"], 6)])
        self.st.meta_set("forecasts:" + config.FIVE_HOUR, {"wid": v5["window_id"], "log": log})
        v5, _ = self.views(now)
        c = chart.build(self.st, v5, now)
        self.assertEqual(len(c["ghosts"]), 1)
        self.assertEqual(c["ghosts"][0][0], [int(t2), round(log[-1][1], 2)])
        self.assertEqual(c["ghosts"][0][-1][0], R5)

    def test_a_maxed_week_flattens_the_5_hours(self):
        now = MON + 14 * H + 40 * 60
        self.work(now)
        v5, vw = self.views(now)
        vw = dict(vw, verdict="maxed")
        chart.attach(self.st, v5, vw, now)
        f = v5["chart"]["forecast"]
        self.assertEqual(f, [[int(now), round(v5["used"], 2)], [R5, round(v5["used"], 2)]])
        self.assertIsNone(v5["chart"]["fan"])

    def test_a_broken_chart_is_none_not_the_band(self):
        v5 = {"window": config.FIVE_HOUR, "present": True}  # no reset_ts
        vw = {"window": config.SEVEN_DAY, "present": False}
        chart.attach(self.st, v5, vw, NOW)
        self.assertIsNone(v5["chart"])
        self.assertIsNone(vw["chart"])

    def test_the_status_line_keeps_snapshots_and_the_band_has_charts(self):
        self.known("s1")
        opts = statusline.parse_args(["--source", "mod", "--format", "json"])
        t = MON + 11 * H + 40 * 60
        for k in range(12):
            raw = json.dumps(payload((2 * k + 1, R5), (30, RW))).encode()
            v5, vw, _ = statusline.compute(raw, self.cfg, opts, t + k * 600)
        log = self.st.meta_get("forecasts:" + config.FIVE_HOUR)["log"]
        self.assertGreaterEqual(len(log), 2)
        self.assertTrue(all(b[0] - a[0] >= 900 for a, b in zip(log, log[1:])))
        ws = band.windows(v5, vw, t + 11 * 600)
        self.assertTrue(all(isinstance(w["chart"], dict) and w["chart"]["actual"] for w in ws), ws)
        # The terminal line draws no chart.
        opts = statusline.parse_args(["--source", "test"])
        v5, _, _ = statusline.compute(json.dumps(payload((25, R5), (30, RW))).encode(), self.cfg, opts, t + 12 * 600)
        self.assertNotIn("chart", v5)

    def test_now_json_has_no_private_keys(self):
        self.known("s1")
        self.feed(MON + 12 * H, five=(10, R5), week=(30, RW))
        _, vw, _ = commands.views(self.st, self.cfg, MON + 13 * H)
        self.assertIn("_weights", vw)  # the chart's
        self.assertFalse([k for k in commands.public(vw) if k.startswith("_")])
        self.assertEqual(commands.public({"a": 1, "_path_k": 2.0}), {"a": 1})


class Png(unittest.TestCase):
    def test_a_line_is_drawn_where_it_goes(self):
        c = Canvas(40, 10)
        c.polyline([(5, 5), (35, 5)], 2.0, (0, 0, 0, 1.0))
        a = [p[3] for p in c.px]
        self.assertEqual(a[5 * 40 + 20], 1.0)
        self.assertEqual(a[4 * 40 + 20], 1.0)
        self.assertEqual(a[1 * 40 + 20], 0.0)
        self.assertEqual(a[5 * 40 + 39], 0.0)

    def test_a_dashed_line_has_gaps_and_joints_are_not_darker(self):
        c = Canvas(40, 10)
        c.polyline([(0, 5), (40, 5)], 2.0, (0, 0, 0, 1.0), dash=(4, 4))
        row = [c.px[5 * 40 + x][3] for x in range(40)]
        self.assertEqual(row[1], 1.0)
        self.assertEqual(row[6], 0.0)
        c = Canvas(40, 10)
        c.polyline([(0, 5), (20, 5), (40, 5)], 2.0, (0, 0, 0, 0.5))
        self.assertAlmostEqual(c.px[5 * 40 + 20][3], 0.5)

    def test_fill_between_two_lines(self):
        c = Canvas(10, 10)
        c.fill_between(0, 10, lambda x: 2.5, lambda x: 8.0, (0, 0, 0, 1.0))
        col = [c.px[y * 10 + 4][3] for y in range(10)]
        self.assertEqual(col, [0.0, 0.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0])

    def test_a_ring(self):
        c = Canvas(20, 20)
        c.ring(10, 10, 6, 2.0, (0, 0, 0, 1.0))
        self.assertEqual(c.px[10 * 20 + 10][3], 0.0)  # the middle is open
        self.assertGreater(c.px[10 * 20 + 16][3], 0.9)


def a_chart(window=config.FIVE_HOUR):
    if window == config.FIVE_HOUR:
        t0, t1 = R5 - 5 * 3600, R5
    else:
        t0, t1 = RW - 7 * 86400, RW
    now = t0 + (t1 - t0) * 0.6
    grid, labels = chart._ticks(window, t0, t1)
    return {"t0": t0, "t1": t1, "now": int(now), "actual": [[t0, 0.0], [int(now) - 600, 50.0], [int(now), 55.0]],
            "forecast": [[int(now), 55.0], [t1, 90.0]], "fan": [80.0, 100.0], "ghosts": [[[t0 + 3600, 20.0], [t1, 70.0]]],
            "grid": grid, "labels": labels}


class MenuChart(TempHome):
    def test_a_chart_picture_of_the_size_asked(self):
        for dark in (False, True):
            w, h, alpha = pixels(menuart.chart(a_chart(), 300, 40, dark, "error"))
            self.assertEqual((w, h), (600, 80))
            self.assertTrue(any(alpha))

    def test_labels_sit_under_their_grid_lines(self):
        c = a_chart()
        text = menuart.labels(c, 300, 6.02)
        self.assertLessEqual(len(text) * 6.02, 300)
        for t, label in c["labels"]:
            col = text.index(label)
            middle = (col + len(label) / 2.0) * 6.02
            self.assertLess(abs(middle - menuart._x(c, t, 300)), 6.02, (label, text))
        week = menuart.labels(a_chart(config.SEVEN_DAY), 300, 6.02)
        self.assertEqual(week.split(), ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])

    def test_labels_that_do_not_fit_are_left_out(self):
        # 13 a minute after 12 has no room; 16 at the right edge ends there.
        c = dict(a_chart(), labels=[[R5 - 3 * 3600, "12"], [R5 - 3 * 3600 + 60, "13"], [R5, "16"]])
        text = menuart.labels(c, 100, 6.02)
        self.assertEqual(text.split(), ["12", "16"])
        self.assertEqual(len(text), int(100 // 6.02))

    def menu(self, cfg=None):
        v5, vw = STATES["on pace"]
        ws = band.windows(dict(v5, chart=a_chart()), dict(vw, chart=a_chart(config.SEVEN_DAY)), NOW)
        return menubar.lines(ws, NOW - 60, NOW, cfg or self.cfg)

    def test_the_menu_has_a_chart_under_each_bar(self):
        out = self.menu()
        bars = [i for i, line in enumerate(out) if line.startswith(("5h ", "Wk "))]
        self.assertEqual(len(bars), 2)
        for i in bars:
            width = int(out[i].split(" width=")[1].split()[0]) + 0
            self.assertRegex(out[i + 1], r"^ \| image=\S+,\S+ width=\d+ height=%d tooltip=What was used" % menubar.CHART_H)
            self.assertNotIn("color=", out[i + 1])  # SwiftBar drops a picture with a color
            self.assertTrue(out[i + 2].endswith(" | font=Menlo size=10 trim=false color=" + menubar.DIM), out[i + 2])
            self.assertGreater(int(out[i + 1].split(" width=")[1].split()[0]), width)  # under the bar and its label
        self.assertEqual(out[bars[1] + 2].split(" | ")[0].split(), ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])

    def test_charts_can_be_turned_off(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["menubar"]["charts"] = False
        self.assertFalse([line for line in self.menu(cfg) if line.startswith(" | image=")])
        v5, vw = STATES["on pace"]
        out = menubar.lines(band.windows(v5, vw, NOW), NOW - 60, NOW, self.cfg)  # no chart in the views
        self.assertFalse([line for line in out if line.startswith(" | image=")])

    def test_the_forecast_takes_the_paces_color(self):
        v5, _ = STATES["runs out"]
        w = band.windows(dict(v5, chart=a_chart()), STATES["runs out"][1], NOW)[0]
        self.assertEqual(menubar.tone(w), "error")
        red = menuart.chart(a_chart(), 300, 40, False, "error")
        plain = menuart.chart(a_chart(), 300, 40, False, None)
        self.assertNotEqual(red, plain)


if __name__ == "__main__":
    unittest.main()
