import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(HERE.name.replace("-", "_"), HERE / "tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

class CommonTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "tool.py"), *args], cwd=HERE,
                              capture_output=True, text=True, timeout=10)
    def test_cli_pass(self):
        result = self.cli("examples/" + PASS, *ARGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.assertEqual(result.stderr, "")
    def test_cli_fail(self):
        result = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "fail")
    def test_cli_malformed(self):
        result = self.cli("examples/" + MALFORMED, *ARGS)
        self.assertEqual(result.returncode, MALFORMED_CODE, result.stdout + result.stderr)
        self.assertIn(json.loads(result.stdout)["status"], ("fail", "error"))
    def test_cli_missing_file(self):
        result = self.cli("nonexistent-input", *ARGS)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "unreadable_utf8_input")
    def test_cli_invalid_arguments_are_redacted(self):
        result = self.cli("--unrecognized-secret-value")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "invalid_arguments")
        self.assertNotIn("unrecognized-secret-value", result.stdout + result.stderr)
    def test_cli_help(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
    def test_cli_stability(self):
        a = self.cli("examples/" + FAIL, *ARGS)
        b = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(a.stdout, b.stdout)
    def test_file_limit_and_utf8(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"x" * (tool.MAX_BYTES + 1))
            with self.assertRaises(tool.InputError):
                tool.read_text(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                tool.read_text(path)
    def test_json_parser_safety(self):
        for text in ('{"x":1,"x":2}', '[NaN]', '[' * 130 + '0' + ']' * 130):
            with self.subTest(text=text[:20]), self.assertRaises(tool.InputError):
                tool.parse_json(text)
    def test_report_cap_preserves_count(self):
        report = tool.Report("test")
        for i in range(101):
            report.add("sample", record=i)
        result = report.finish()
        self.assertEqual(len(result["findings"]), 100)
        self.assertEqual(result["summary"]["finding_count"], 101)
        self.assertTrue(result["findings_truncated"])

    def test_adversarial_cli_and_read_only_corpus(self):
        samples = ['', 'null', 'true', '0', '[]', '{}', '[null]', '[true]', '[1]',
                   '["NaN"]', '[{}]', '[[]]', '{"x":1,"x":2}',
                   '[1e99999999999999999999999999]', '[NaN]', '[Infinity]',
                   '"\\ud800"', '["\\ud800"]', '{', '[', '["a","b"]',
                   '[1e-99999999999999999999999999]']
        before = {path: path.read_bytes() for path in (HERE / 'examples').iterdir()}
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / 'input.txt'
            for sample in samples:
                with self.subTest(sample=sample):
                    path.write_text(sample, encoding='utf-8')
                    result = self.cli(str(path), *ARGS)
                    self.assertIn(result.returncode, (0, 1, 2), result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertIn(json.loads(result.stdout)['status'], ('pass','fail','error'))
                    self.assertEqual(path.read_text(encoding='utf-8'), sample)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

PASS = 'pass.json'
FAIL = 'fail.json'
MALFORMED = 'malformed.json'
MALFORMED_CODE = 2
ARGS = ['--start', '2026-01-01T00:00:00Z', '--end', '2026-12-31T23:59:59Z']

class AuditTests(unittest.TestCase):
    def run_audit(self, values, **kwargs):
        return tool.audit(values,start=kwargs.pop('start','2026-01-01T00:00:00Z'),end=kwargs.pop('end','2026-12-31T23:59:59Z'),**kwargs)
    def test_all_unit_candidates(self):
        result=self.run_audit([1767225600,1767225600000,1767225600000000])
        self.assertEqual([r['candidate_units'] for r in result['records']],[['seconds'],['milliseconds'],['microseconds']])
    def test_epoch_zero_ambiguous(self):
        result=self.run_audit([0],start='1969-12-31T00:00:00Z',end='1970-01-02T00:00:00Z')
        self.assertEqual(result['summary']['ambiguous_records'],1)
        self.assertEqual(result['status'],'fail')
    def test_negative_epoch(self):
        result=self.run_audit([-86400],start='1969-12-31T00:00:00Z',end='1969-12-31T00:00:00Z')
        self.assertEqual(result['records'][0]['candidate_units'],['seconds'])
    def test_inclusive_microsecond_endpoint(self):
        result=self.run_audit([1767225600000001],start='2026-01-01T00:00:00.000001Z',end='2026-01-01T00:00:00.000001Z')
        self.assertEqual(result['status'],'pass')
    def test_timezone_equivalence(self):
        self.assertEqual(tool.boundary('2026-01-01T01:00:00+01:00'),tool.boundary('2026-01-01T00:00:00Z'))
    def test_expected_unit_does_not_correct(self):
        result=self.run_audit([1767225600],expected='milliseconds')
        self.assertEqual(result['findings'][0]['code'],'expected_unit_outside_window')
        self.assertEqual(result['records'][0]['candidate_units'],['seconds'])
    def test_out_of_window(self): self.assertEqual(self.run_audit([0])['findings'][0]['code'],'no_unit_in_window')
    def test_fractional_boolean_huge(self):
        for value in (1.5,True,'1.5',10**30,'９'):
            with self.assertRaises(tool.InputError): self.run_audit([value])
    def test_bound_errors(self):
        for start,end in [('2026-01-01','2027-01-01Z'),('bad','bad'),('2027-01-01T00:00:00Z','2026-01-01T00:00:00Z')]:
            with self.assertRaises(tool.InputError): self.run_audit([0],start=start,end=end)
    def test_redacted_values(self): self.assertNotIn('1767225600',json.dumps(self.run_audit([1767225600])))
    def test_rows_cap(self):
        result=self.run_audit([1767225600]*101)
        self.assertTrue(result['records_truncated'])
        self.assertEqual(result['summary']['candidate_counts']['seconds'],101)

if __name__ == '__main__': unittest.main()
