"""Golden output guards for budget-limited generic view selection."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from leancontext.compact import compact
from leancontext.store import Store


class GenericCompactBudgetTests(unittest.TestCase):
    def test_existing_view_format_and_recovery(self):
        cases = (
            ('dense', ''.join(f'ERROR: test {i} failed\n' for i in range(100)) + 'fatal: last error\n',
             512, 'af07155d27606c7c5d1f5359fcb79d3a1f0a982ff6d7a191c9fb58ed99bccb86', 99),
            ('mixed', ''.join(f'warning: unit {i}\n' if i%7 == 0 else f'progress {i}\n' for i in range(120)) + '3 failed\nfatal: last cause\n',
             1200, 'a868e49bd442605f36dce51acd1483a2a3842939f47814206723e7563642af95', 106),
            ('unicode', ('\x1b[31mERROR: 異常😀\x1b[0m\r\n' + 'x'*800 + '\u2028\n')*12 + '9 passed\n',
             901, '853d16fb8d1f2fb32ed8e89c836595ba1fd728b18b94823cf8994976b14eae3e', 26),
            ('long', 'x'*5000 + '\nfatal: next\n' + 'y'*2000,
             512, 'b111f7c1ff18cc7d9682ce83bca971fea81ae5053765727d2c3ce6e51203584d', 1),
            ('plain', 'progress only\n'*90,
             700, '743f106ea7a20af9b171d3b8a165b41d24e32f05cb3c2ae0033da6d9cc13a271', 84),
            ('small', 'OK\n',
             512, 'a12b7cb43c9d9134b5bb1b35e9096b66775d9e92e7611d1cc92b02edd6782a87', 0),
        )
        with TemporaryDirectory() as root:
            with TemporaryDirectory() as state:
                store = Store(Path(root), state)
                for name, text, budget, expected, omitted in cases:
                    with self.subTest(case=name):
                        view = compact(text, store=store, max_chars=budget, strategy='generic')
                        self.assertEqual(sha256(view.text.encode()).hexdigest(), expected)
                        self.assertEqual(view.omitted_lines, omitted)
                        self.assertEqual(view.original_chars, len(text))
                        self.assertEqual(view.emitted_chars, len(view.text))
                        self.assertLessEqual(len(view.text), budget)
                        if view.changed:
                            self.assertEqual(store.read_bytes(view.ref), text.encode())
                        else:
                            self.assertEqual(view.text, text)
                            self.assertIsNone(view.ref)


if __name__ == '__main__':
    unittest.main()
