"""Unit tests for the solver and CSV parser. Run: python -m unittest -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import analyze, build_groups, unsupported_conclusions
from app.csv_import import CSVError, parse_csv


def rows_from_triplets(triplets):
    return list(triplets)


class SolverTests(unittest.TestCase):
    def setUp(self):
        # C1: G1={a,b} G2={c}
        # C2: G1={a,d} G2={e}
        # C3: G1={f,g} G2={h}
        # C4: G1={c,e}
        # C5: G1={i,j} G2={k,l}
        self.rows = [
            ("C1", "G1", "a"), ("C1", "G1", "b"), ("C1", "G2", "c"),
            ("C2", "G1", "a"), ("C2", "G1", "d"), ("C2", "G2", "e"),
            ("C3", "G1", "f"), ("C3", "G1", "g"), ("C3", "G2", "h"),
            ("C4", "G1", "c"), ("C4", "G1", "e"),
            ("C5", "G1", "i"), ("C5", "G1", "j"),
            ("C5", "G2", "k"), ("C5", "G2", "l"),
        ]

    def test_already_unsupported_all_groups_broken(self):
        marks = {d: "normal" for d in "abcdefghijkl"}
        marks["a"] = "retracted"   # breaks C1/G1 and C2/G1
        marks["c"] = "retracted"   # breaks C1/G2 -> C1 unsupported;
        # C4's only group G1={c,e} contains c -> C4 unsupported too
        groups = build_groups(self.rows)
        retracted = {d for d, m in marks.items() if m == "retracted"}
        self.assertEqual(unsupported_conclusions(groups, retracted),
                         ["C1", "C4"])

    def test_one_complete_group_keeps_conclusion_alive(self):
        marks = {d: "normal" for d in "abcdefghijkl"}
        marks["a"] = "retracted"
        groups = build_groups(self.rows)
        retracted = {"a"}
        # C2 still has G2={e} intact
        self.assertEqual(unsupported_conclusions(groups, retracted), [])

    def test_killable_and_optimal_selection(self):
        marks = {d: "normal" for d in "abcdefghijkl"}
        # Existing retraction breaks C1/G1; warning c breaks C1/G2 if retracted
        marks["a"] = "retracted"
        for d in ("c", "e", "h"):
            marks[d] = "warning"
        result = analyze(self.rows, marks, k=2)

        # C1 already unsupported (G1 via a, G2 would need c — wait: G2 only
        # breaks when c is actually retracted; a is not in G2).
        self.assertEqual(result["already_unsupported"], [])
        # Killable: C1 needs {c} (G1 already broken by a);
        #   C2 needs {e} (G1 already broken by a, intact G2={e});
        #   C3 groups contain no warnings -> not killable;
        #   C4's single group {c,e} -> {c,e}; C5 has no warnings.
        self.assertEqual(set(result["killable_conclusions"]),
                         {"C1", "C2", "C4"})
        # k=2: {c, e} kills C1, C2, C4 = 3; any other pair kills at most 2.
        self.assertEqual(result["affected_count"], 3)
        self.assertEqual(set(result["selected_dois"]), {"c", "e"})
        self.assertEqual(set(result["affected_conclusions"]),
                         {"C1", "C2", "C4"})

    def test_k_zero_selects_nothing(self):
        marks = {d: "normal" for d in "abcdefghijkl"}
        marks["c"] = "warning"
        result = analyze(self.rows, marks, k=0)
        self.assertEqual(result["selected_dois"], [])
        self.assertEqual(result["affected_count"], 0)

    def test_intact_group_without_warning_is_not_killable(self):
        marks = {d: "normal" for d in "abcdefghijkl"}
        # C5 groups contain i,j,k,l all normal -> cannot be killed
        result = analyze(self.rows, marks, k=5)
        self.assertNotIn("C5", result["killable_conclusions"])
        self.assertNotIn("C5", result["affected_conclusions"])

    def test_deterministic_tie_break_lexicographic(self):
        # Two disjoint single-group conclusions, each killed by one of two
        # warnings; k=1 -> equal gain, smaller DOI wins.
        rows = [("X", "G1", "x1"), ("Y", "G1", "y1")]
        marks = {"x1": "warning", "y1": "warning"}
        r1 = analyze(rows, marks, k=1)
        self.assertEqual(r1["affected_count"], 1)
        self.assertEqual(r1["selected_dois"], ["x1"])

    def test_k_larger_than_referenced_warnings(self):
        rows = [("X", "G1", "x1"), ("X", "G2", "x2")]
        marks = {"x1": "warning", "x2": "warning"}
        result = analyze(rows, marks, k=10)
        self.assertEqual(result["k_used"], 2)
        self.assertEqual(result["affected_count"], 1)

    def test_invalid_k(self):
        with self.assertRaises(ValueError):
            analyze([], {}, -1)


class CsvParserTests(unittest.TestCase):
    def test_chinese_headers(self):
        raw = "结论号,引文组号,DOI\nC1,G1,10.1/A\nC1,G1,10.1/a\n".encode()
        recs = parse_csv(raw)
        # duplicate after DOI lowercasing is deduped
        self.assertEqual(recs, [("C1", "G1", "10.1/a")])

    def test_english_headers(self):
        raw = b"conclusion_id,group_id,doi\nC1,G1,10.1/x\n"
        self.assertEqual(parse_csv(raw), [("C1", "G1", "10.1/x")])

    def test_missing_column(self):
        with self.assertRaises(CSVError):
            parse_csv(b"conclusion_id,group_id\nC1,G1\n")

    def test_empty_cell(self):
        with self.assertRaises(CSVError):
            parse_csv(b"conclusion_id,group_id,doi\nC1,,10.1/x\n")

    def test_empty_file(self):
        with self.assertRaises(CSVError):
            parse_csv(b"")

    def test_duplicate_rows_removed(self):
        raw = b"conclusion_id,group_id,doi\nC1,G1,10.1/x\nC1,G1,10.1/x\n"
        self.assertEqual(len(parse_csv(raw)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
