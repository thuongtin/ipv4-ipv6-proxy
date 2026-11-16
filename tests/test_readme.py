"""Keep localized READMEs aligned with the Vietnamese source."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
README_NAMES = ('README.md', 'README.zh-CN.md', 'README.ko.md', 'README.ja.md', 'README.en.md')
NAVIGATION = '[Tiếng Việt](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | [English](README.en.md)'


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.documents = {name: (ROOT / name).read_text(encoding='utf-8') for name in README_NAMES}
        self.source = self.documents['README.md']

    def test_all_languages_have_navigation_and_four_sections(self):
        for name, text in self.documents.items():
            with self.subTest(name=name):
                self.assertEqual(text.count(NAVIGATION), 1)
                self.assertEqual(len(re.findall(r'^## ', text, re.MULTILINE)), 4)
                self.assertNotIn('\u2014', text)

    def test_shell_commands_are_identical(self):
        expected = re.findall(r'```sh\n(.*?)```', self.source, re.DOTALL)
        self.assertEqual(len(expected), 2)
        for name, text in self.documents.items():
            with self.subTest(name=name):
                self.assertEqual(re.findall(r'```sh\n(.*?)```', text, re.DOTALL), expected)
                self.assertEqual(text.count('```'), 4)

    def test_link_targets_and_affiliate_are_preserved(self):
        expected = re.findall(r'\]\(([^)]+)\)', self.source)
        for name, text in self.documents.items():
            with self.subTest(name=name):
                targets = re.findall(r'\]\(([^)]+)\)', text)
                self.assertEqual(targets, expected)
                self.assertEqual(text.count('https://www.vultr.com/?ref=8809561'), 1)
                for target in targets:
                    if not target.startswith(('https://', 'http://', '#')):
                        self.assertTrue((ROOT / target.split('#', 1)[0]).is_file(), target)

    def test_technical_constants_are_preserved(self):
        inline = lambda text: re.findall(r'(?<!`)`([^`\n]+)`(?!`)', text)
        expected = inline(self.source)
        for name, text in self.documents.items():
            with self.subTest(name=name):
                self.assertEqual(inline(text), expected)
                for value in ('Alpine Linux', '3proxy 1.0.3', 'OpenRC', '/64', '10000', '11999', '80/443', 'Gift Code', 'PayPal'):
                    self.assertIn(value, text)

    def test_offer_verification_date_is_preserved(self):
        dates = {'README.md': '16/11/2026', 'README.zh-CN.md': '2026 年 11 月 16 日',
                 'README.ko.md': '2026년 11월 16일', 'README.ja.md': '2026 年 11 月 16 日',
                 'README.en.md': '16 November 2026'}
        for name, date in dates.items():
            with self.subTest(name=name):
                self.assertIn(date, self.documents[name])


if __name__ == '__main__':
    unittest.main()
