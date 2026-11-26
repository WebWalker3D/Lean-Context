"""Source-command classification is independent of the evaluator host."""
import unittest

from leancontext.diagnostics import source_command


class WindowsSourceCommandTests(unittest.TestCase):
    def test_windows_rg_executable(self):
        self.assertTrue(source_command({'argv': [r'C:\Tools\rg.exe', 'error', '.']}))

    def test_windows_git_executable(self):
        self.assertTrue(source_command({'argv': [r'C:\Program Files\Git\cmd\GIT.EXE', '-C', r'C:\dev', 'diff']}))

    def test_bare_exe(self):
        self.assertTrue(source_command({'argv': ['rg.exe', 'error']}))
        self.assertTrue(source_command({'argv': ['git.exe', 'show', 'HEAD']}))

    def test_non_source_git(self):
        self.assertFalse(source_command({'argv': [r'C:\Tools\git.exe', 'commit', '-m', 'show']}))

    def test_posix_source(self):
        self.assertTrue(source_command({'argv': ['/usr/bin/git', '-C', '/tmp', 'show', 'HEAD']}))
        self.assertTrue(source_command({'argv': ['/usr/bin/grep', 'error']}))
        self.assertFalse(source_command({'argv': ['/usr/bin/git', 'status']}))

    def test_shell_wrapper_not_inferred(self):
        self.assertFalse(source_command({'argv': ['pwsh.exe', '-Command', 'Get-Content x']}))

    def test_invalid_argv(self):
        self.assertFalse(source_command({'argv': ['git', None]}))
