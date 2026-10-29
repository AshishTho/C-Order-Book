"""Black-box reconstruction checks against a deliberately simple Python oracle."""
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

BINARY = str(Path(sys.argv.pop(1)).resolve())
HEADER = "seq,type,order_ref,side,price,qty\n"
SEED_BOOK = "1,A,1,B,100,20\n2,A,2,S,102,30\n"


class ReplayTests(unittest.TestCase):
    def invoke(self, contents, *options):
        with tempfile.TemporaryDirectory() as temp:
            src, dst = Path(temp) / "events.csv", Path(temp) / "bbo.csv"
            src.write_bytes(contents.encode("utf-8"))
            result = subprocess.run([BINARY, "--input", str(src), "--output", str(dst), *options],
                                    text=True, capture_output=True, timeout=20)
            data = dst.read_text() if dst.exists() else None
            self.assertFalse(Path(str(dst) + ".partial").exists())
            return result, data

    def test_hand_calculated_lifecycle(self):
        result, data = self.invoke(HEADER + SEED_BOOK +
                                  "3,E,1,-,0,5\n4,X,2,-,0,7\n5,D,1,-,0,0\n6,A,3,B,101,9\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data, "seq,bid_price,ask_price,bid_qty,ask_qty\n"
                              "2,100,102,20,30\n3,100,102,15,30\n4,100,102,15,23\n6,101,102,9,23\n")
        self.assertEqual(json.loads(result.stdout)["events"], 6)

    def test_rejects_corrupt_events_without_publishing_partial_output(self):
        for event in ["3,A,1,B,99,1", "3,A,3,B,102,1", "3,E,9,-,0,1",
                      "3,E,1,-,0,21", "3,X,1,-,0,-1", "3,D,1,-,0,1",
                      "3,A,3,Z,99,1", "3,A,3,B,65536,1", "3,A,3,B,99,0",
                      "3,A,0,B,99,1", "3,Z,1,-,0,1", "4,D,1,-,0,0",
                      "2,D,1,-,0,0", "3,E,1,B,0,1", "3,E,1,-,100,1",
                      "3,E,1,-,0,1,extra", "3,E,1,-,0,1junk",
                      "3,A,3,B,99,9223372036854775808"]:
            with self.subTest(event=event):
                result, data = self.invoke(HEADER + SEED_BOOK + event + "\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("line 4:", result.stderr)
                self.assertIsNone(data)

    def test_empty_or_one_sided_session_is_not_research_data(self):
        for contents in [HEADER, HEADER + "1,A,1,B,100,20\n", "wrong\n", ""]:
            result, data = self.invoke(contents)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(data)

    def test_ctrl_z_cannot_hide_corrupt_tail_on_windows(self):
        result, data = self.invoke(HEADER + SEED_BOOK + "\x1a3,invalid\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(data)

    def test_output_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            src, dst = Path(temp) / "in.csv", Path(temp) / "out.csv"
            src.write_text(HEADER + SEED_BOOK)
            dst.write_text("earlier result")
            result = subprocess.run([BINARY, "--input", str(src), "--output", str(dst)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(dst.read_text(), "earlier result")

    def test_integer_endpoints_are_lossless(self):
        maximum = (1 << 63) - 1
        sequence = (1 << 64) - 1
        base = maximum - 65535
        result, data = self.invoke(
            HEADER + f"{sequence - 2},A,{sequence},B,{base},{maximum}\n"
            f"{sequence - 1},A,{sequence - 1},S,{maximum},{maximum}\n"
            f"{sequence},E,{sequence},-,0,{maximum - 1}\n",
            "--base-tick", str(base))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data, "seq,bid_price,ask_price,bid_qty,ask_qty\n"
                         f"{sequence - 1},{base},{maximum},{maximum},{maximum}\n"
                         f"{sequence},{base},{maximum},1,{maximum}\n")
        self.assertEqual(json.loads(result.stdout)["events"], 3)

    def test_base_translation_preserves_depth_and_sequence(self):
        events = [(1, "A", 1, "B", 0, 9), (2, "A", 2, "S", 65535, 11),
                  (3, "A", 3, "B", 65534, 5), (4, "X", 3, "-", 0, 2),
                  (5, "D", 3, "-", 0, 0), (6, "A", 3, "B", 0, 4)]
        expected = [[2, 0, 65535, 9, 11], [3, 65534, 65535, 5, 11],
                    [4, 65534, 65535, 3, 11], [5, 0, 65535, 9, 11],
                    [6, 0, 65535, 13, 11]]
        for base in [0, 123456, (1 << 63) - 65536]:
            with self.subTest(base=base):
                rows = [f"{seq},{kind},{ref},{side},{price + base if kind == 'A' else 0},{qty}"
                        for seq, kind, ref, side, price, qty in events]
                result, data = self.invoke(HEADER + "\n".join(rows) + "\n", "--base-tick", str(base))
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = [[int(value) for value in row] for row in list(csv.reader(io.StringIO(data)))[1:]]
                for row in actual:
                    row[1] -= base
                    row[2] -= base
                self.assertEqual(actual, expected)

    def test_fully_reduced_reference_can_be_reused_on_opposite_side(self):
        result, data = self.invoke(HEADER + SEED_BOOK +
                                  "3,E,1,-,0,20\n4,D,2,-,0,0\n"
                                  "5,A,1,S,102,7\n6,A,2,B,100,9\n"
                                  "7,X,1,-,0,7\n8,A,1,S,101,4\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data, "seq,bid_price,ask_price,bid_qty,ask_qty\n"
                              "2,100,102,20,30\n6,100,102,9,7\n8,100,101,9,4\n")
        self.assertEqual(json.loads(result.stdout),
                         {"events": 8, "snapshots": 3, "live_orders": 2, "price_unit": "tick"})

    def test_invalid_integer_boundaries_do_not_publish(self):
        for event in ["18446744073709551616,D,1,-,0,0", "-1,D,1,-,0,0",
                      "3,A,18446744073709551616,B,99,1", "3,A,-1,B,99,1",
                      "3,A,3,B,9223372036854775808,1", "3,A,3,B,-9223372036854775809,1",
                      "3,A,3,B,99,-9223372036854775809", "3,E,1,-,0,+1",
                      "3,E,1,-,0, 1", "3,E,1,-,0,1 ", "3,E,1,-,0,1.0"]:
            with self.subTest(event=event):
                result, data = self.invoke(HEADER + SEED_BOOK + event + "\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("line 4:", result.stderr)
                self.assertIsNone(data)
        for base in [-1, (1 << 63) - 65535, 1 << 63]:
            with self.subTest(base=base):
                result, data = self.invoke(HEADER + SEED_BOOK, "--base-tick", str(base))
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(data)

    def test_line_endings_and_final_unterminated_row_are_equivalent(self):
        expected = None
        for ending in ["\n", "\r\n"]:
            for terminated in [True, False]:
                with self.subTest(ending=repr(ending), terminated=terminated):
                    contents = (HEADER + SEED_BOOK).replace("\n", ending)
                    if not terminated:
                        contents = contents[:-len(ending)]
                    result, data = self.invoke(contents)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if expected is None:
                        expected = data
                    self.assertEqual(data, expected)

    def test_randomized_depth_matches_independent_oracle(self):
        rng = random.Random(8021)
        orders = {}
        lines, expected = [], []
        next_ref = 0
        for seq in range(1, 3001):
            if not orders or rng.random() < .57:
                next_ref += 1
                side = rng.choice(["B", "S"])
                price = rng.randrange(90, 100) if side == "B" else rng.randrange(101, 111)
                qty = rng.randrange(1, 80)
                orders[next_ref] = [side, price, qty]
                lines.append(f"{seq},A,{next_ref},{side},{price},{qty}")
            else:
                ref = rng.choice(list(orders))
                if rng.random() < .25:
                    lines.append(f"{seq},D,{ref},-,0,0")
                    del orders[ref]
                else:
                    qty = rng.randrange(1, orders[ref][2] + 1)
                    lines.append(f"{seq},{rng.choice(['E','X'])},{ref},-,0,{qty}")
                    orders[ref][2] -= qty
                    if not orders[ref][2]:
                        del orders[ref]
            bids = [o for o in orders.values() if o[0] == "B"]
            asks = [o for o in orders.values() if o[0] == "S"]
            if bids and asks:
                bid, ask = max(o[1] for o in bids), min(o[1] for o in asks)
                expected.append([seq, bid, ask, sum(o[2] for o in bids if o[1] == bid),
                                 sum(o[2] for o in asks if o[1] == ask)])
        result, data = self.invoke(HEADER + "\n".join(lines) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = list(csv.reader(io.StringIO(data)))
        self.assertEqual([[int(v) for v in row] for row in rows[1:]], expected)


class ReplayRepeatabilityTests(unittest.TestCase):
    def test_exact_replay_is_byte_repeatable_on_same_host(self):
        events = HEADER + SEED_BOOK + "3,E,1,-,0,5\n4,X,2,-,0,7\n5,D,1,-,0,0\n6,A,3,B,101,9\n"
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "events.csv"
            src.write_text(events, encoding="utf-8")
            payload = src.read_bytes()
            outputs, digests = [], set()
            for run in range(3):
                dst = Path(temp) / f"bbo_{run}.csv"
                self.assertFalse(dst.exists())
                result = subprocess.run([BINARY, "--input", str(src), "--output", str(dst)],
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(Path(str(dst) + ".partial").exists())
                self.assertEqual(src.read_bytes(), payload)
                raw = dst.read_bytes()
                self.assertTrue(raw)
                summary = json.loads(result.stdout)
                self.assertEqual({k: summary[k] for k in ("events", "snapshots", "live_orders")},
                                 {"events": 6, "snapshots": 4, "live_orders": 2})
                outputs.append(raw)
                digests.add(hashlib.sha256(raw).hexdigest())
        self.assertEqual(outputs[1:], outputs[:1] * 2)
        self.assertEqual(len(digests), 1)
        rows = list(csv.reader(io.StringIO(outputs[0].decode("utf-8"))))
        self.assertEqual(rows[1:], [["2", "100", "102", "20", "30"], ["3", "100", "102", "15", "30"],
                                    ["4", "100", "102", "15", "23"], ["6", "101", "102", "9", "23"]])


if __name__ == "__main__":
    unittest.main()
