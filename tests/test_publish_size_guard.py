"""发布前超限文件护栏：远端 push 会拒绝超限 blob，必须在本地提前失败。"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('workflow_runner',ROOT/'run_build.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PublishSizeGuardTests(unittest.TestCase):
    def git(self,root,*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.PIPE,text=True).strip()

    def repository(self,temporary):
        root=Path(temporary)
        self.git(root,'init','--quiet','--initial-branch=main')
        self.git(root,'config','user.name','errbin')
        self.git(root,'config','user.email','errbin@users.noreply.github.com')
        return root

    def test_oversized_staged_file_fails_before_push(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=self.repository(temporary)
            oversized=root/'state/cache/icons/apple.json'
            oversized.parent.mkdir(parents=True)
            oversized.write_bytes(b'x'*(2048+1))
            self.git(root,'add','--all')
            with self.assertRaisesRegex(ValueError,'state/cache/icons/apple.json'):
                runner.check_published_file_sizes(root,max_bytes=2048)

    def test_limit_reports_every_offending_path_with_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=self.repository(temporary)
            for name in ('a.bin','b.bin'):
                (root/name).write_bytes(b'x'*4096)
            (root/'small.bin').write_bytes(b'x'*8)
            self.git(root,'add','--all')
            with self.assertRaisesRegex(ValueError,'a.bin') as caught:
                runner.check_published_file_sizes(root,max_bytes=1024)
            self.assertIn('b.bin',str(caught.exception))
            self.assertNotIn('small.bin',str(caught.exception))

    def test_files_at_the_limit_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=self.repository(temporary)
            (root/'exact.bin').write_bytes(b'x'*1024)
            self.git(root,'add','--all')
            runner.check_published_file_sizes(root,max_bytes=1024)

    def test_guard_runs_before_push_is_attempted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=self.repository(temporary)
            (root/'huge.bin').write_bytes(b'x'*(4096+1))
            attempted=[]
            original=runner.execute
            def record(args,**kwargs):
                attempted.append(list(args))
                return original(args,**kwargs)
            with patch.object(runner,'execute',record), \
                 patch.object(runner,'PUBLISHED_FILE_MAX_BYTES',4096):
                with self.assertRaisesRegex(ValueError,'huge.bin'):
                    runner.push_repository(root,'git@example.invalid:repo.git','key','main',message='m')
            self.assertFalse(any('push' in args for args in attempted))


if __name__=='__main__':
    unittest.main()
