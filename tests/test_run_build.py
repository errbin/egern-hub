"""Check live build diagnostics without publishing private connection details."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('workflow_runner',ROOT/'run_build.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class BuildLogTests(unittest.TestCase):
    def environment(self):
        return dict(os.environ,BUILD_REPOSITORY='git@github.com:demo/private-engine.git',
                    UPSTREAM_REPOSITORY='https://github.com/demo/private-inputs.git',
                    SITE_REPOSITORY='https://github.com/demo/private-output.git',
                    EXAMPLE_TOKEN='sensitive-example-value')

    def test_redacts_private_repositories_but_keeps_public_sources_and_errors(self):
        clean=runner.LogSanitizer(self.environment()).clean
        text='\n'.join([
            'git@github.com:demo/private-engine.git: Permission denied (publickey)',
            'https%3A%2F%2Fgithub.com%2Fdemo%2Fprivate-inputs.git HTTP 403',
            'https://github.com/demo/private%2Dengine.git',
            '/tmp/private-output/build.py:42 ValueError: failed',
            'token=sensitive-example-value',
            'https://github.com/Example/public-scripts/blob/main/天气.js',
            'https://raw.githubusercontent.com/Example/scripts/main/tool%20name.js?pattern=%5Efoo',
            'SyntaxError: Unexpected token at 广告过滤.js:12',
        ])
        output=clean(text)
        for hidden in ['private-engine','private-inputs','private-output','sensitive-example-value']:
            self.assertNotIn(hidden,output)
        for visible in ['Permission denied (publickey)','HTTP 403','ValueError: failed',
                        'https://github.com/Example/public-scripts/blob/main/天气.js',
                        'https://raw.githubusercontent.com/Example/scripts/main/tool%20name.js?pattern=%5Efoo',
                        'SyntaxError: Unexpected token at 广告过滤.js:12']:
            self.assertIn(visible,output)

    def test_masks_credentials_and_blocks_upstream_workflow_commands(self):
        sanitizer=runner.LogSanitizer({})
        self.assertNotIn('ghp_exampleSecretValue',sanitizer.clean('token ghp_exampleSecretValue'))
        self.assertNotIn('user:password',sanitizer.clean('https://user:password@example.com/file'))
        self.assertNotIn('::error::',sanitizer.clean('::error::untrusted text'))
        sanitizer.clean('-----BEGIN OPENSSH PRIVATE KEY-----')
        self.assertNotIn('key-body',sanitizer.clean('key-body'))
        sanitizer.clean('-----END OPENSSH PRIVATE KEY-----')
        self.assertEqual(sanitizer.clean('ordinary diagnostic'),'ordinary diagnostic')
        known_key='-----BEGIN OPENSSH PRIVATE KEY-----\nkey-body\nabcd\n-----END OPENSSH PRIVATE KEY-----'
        known=runner.LogSanitizer({'BUILD_READ_KEY':known_key})
        for line in known_key.splitlines():
            self.assertNotIn(line,known.clean(line))

    def test_streams_diagnostics_before_child_exits_and_preserves_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            signal=root/'observed'
            class Sink(io.StringIO):
                def write(self,value):
                    if 'RuntimeError: 脚本.js:17 network failed' in value:
                        signal.touch()
                    return super().write(value)
            output=Sink()
            child='''import pathlib,sys,time
signal=pathlib.Path(sys.argv[1])
print("RuntimeError: 脚本.js:17 network failed",flush=True)
print("private-engine: HTTP 403",flush=True)
deadline=time.monotonic()+5
while not signal.exists() and time.monotonic()<deadline:time.sleep(0.01)
sys.exit(7 if signal.exists() else 8)
'''
            with contextlib.redirect_stdout(output):
                result=runner.run_with_error_logs([sys.executable,'-u','-c',child,str(signal)],
                    cwd=root,env=self.environment(),log_path=root/'build.log')
            self.assertEqual(result,7)
            self.assertIn('RuntimeError: 脚本.js:17 network failed',output.getvalue())
            self.assertIn('HTTP 403',output.getvalue())
            self.assertNotIn('private-engine',output.getvalue())

    def test_formats_progress_and_retains_detailed_verification_errors(self):
        event={'kind':'verification','stage':'case_completed','id':'adapted-1234',
               'url':'https://raw.githubusercontent.com/Example/scripts/main/ad-filter.js',
               'result':{'name':'response body','status':'converted_error',
                         'converted_error':'TypeError: missing body','differences':['$.body']}}
        message=runner.public_progress('@@EGERN_PROGRESS '+json.dumps(event))
        self.assertIn('ad-filter.js',message)
        self.assertIn('TypeError: missing body',message)
        self.assertIn('$.body',message)

    def test_formats_source_reference_and_actual_read_method(self):
        url='https://raw.githubusercontent.com/Example/scripts/'+'a'*40+'/folder/demo.js'
        for method,label in [('cache','local cache'),('archive','repository snapshot'),('download','network download')]:
            with self.subTest(method=method):
                event={'kind':'source','stage':'reading_repository_file','id':'folder/demo.js',
                       'url':url,'method':method}
                message=runner.public_progress('@@EGERN_PROGRESS '+json.dumps(event))
                self.assertIn(url,message)
                self.assertIn(label,message)
                self.assertIn('folder/demo.js',message)

    def test_failed_setup_command_prints_sanitized_error(self):
        result=subprocess.CompletedProcess(['git'],128,b'',
            b'fatal: private-engine: Permission denied (publickey)\n')
        output=io.StringIO()
        with patch.dict(os.environ,self.environment()),patch.object(runner.subprocess,'run',return_value=result):
            with contextlib.redirect_stdout(output),self.assertRaisesRegex(RuntimeError,'128'):
                runner.execute(['git'])
        self.assertIn('Permission denied (publickey)',output.getvalue())
        self.assertNotIn('private-engine',output.getvalue())

    def test_successful_build_is_silent_but_private_log_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            lines=['Building: Generate static site',
                   'test_failed_conversion (tests.Check) ... ok',
                   '@@EGERN_PROGRESS '+json.dumps({'kind':'verification','stage':'case_completed',
                       'id':'demo.js','result':{'status':'passed','original_error':None}})]
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                result=runner.run_with_error_logs([sys.executable,'-c','print('+repr('\n'.join(lines))+')'],
                    cwd=root,env=self.environment(),log_path=root/'build.log')
            self.assertEqual(result,0)
            self.assertEqual(output.getvalue(),'')
            self.assertEqual((root/'build.log').read_text(),'\n'.join(lines)+'\n')

    def test_only_failed_events_and_traceback_details_are_shown(self):
        output=io.StringIO()
        diagnostics=runner.ErrorLogFilter(runner.LogSanitizer(self.environment()))
        with contextlib.redirect_stdout(output):
            for status in ['passed','inconclusive','converted_error','mismatch']:
                diagnostics.write('@@EGERN_PROGRESS '+json.dumps({'kind':'verification',
                    'stage':'case_completed','id':status+'.js','result':{'status':status}}))
            diagnostics.write('@@EGERN_PROGRESS '+json.dumps({'kind':'script',
                'stage':'conversion_failed','id':'demo.js','reason':'unsupported completion flow'}))
            diagnostics.write('@@EGERN_PROGRESS '+json.dumps({'kind':'script',
                'stage':'probe_generation_skipped','id':'slow.js','reason':'probe analysis timed out'}))
            for line in ['Traceback (most recent call last):',
                         '  File "/tmp/private-engine/build.py", line 42, in build',
                         '    parse_script()', 'ValueError: invalid rule', 'ordinary progress']:
                diagnostics.write(line)
        text=output.getvalue()
        for visible in ['converted_error.js','mismatch.js','demo.js','unsupported completion flow',
                        'Verification probes skipped: slow.js: probe analysis timed out',
                        'line 42','parse_script()','ValueError: invalid rule']:
            self.assertIn(visible,text)
        for hidden in ['passed.js','inconclusive.js','private-engine','ordinary progress']:
            self.assertNotIn(hidden,text)

    def test_unclassified_command_failure_keeps_sanitized_diagnostics(self):
        result=subprocess.CompletedProcess(['git'],128,b'',b'private-engine: malformed response\n')
        output=io.StringIO()
        with patch.dict(os.environ,self.environment()),patch.object(runner.subprocess,'run',return_value=result):
            with contextlib.redirect_stdout(output),self.assertRaises(RuntimeError):
                runner.execute(['git'])
        self.assertIn('malformed response',output.getvalue())
        self.assertNotIn('private-engine',output.getvalue())

    def test_preserves_complete_multiline_unittest_failures_and_failed_totals(self):
        output=io.StringIO()
        child='''import unittest
print("ordinary progress",flush=True)
class Checks(unittest.TestCase):
    def test_difference(self):
        self.assertEqual({"unexpected-entry"},set())
    def test_multiline(self):
        self.fail("first line\\nsecond line without indentation\\nprivate-inputs: expected snapshot differs")
unittest.main(argv=["checks"],verbosity=2)
'''
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(output):
            root=Path(temporary)
            result=runner.run_with_error_logs([sys.executable,'-u','-c',child],
                cwd=root,env=self.environment(),log_path=root/'tests.log')
        self.assertEqual(result,1)
        text=output.getvalue()
        for visible in ["'unexpected-entry'",'second line without indentation',
                        'expected snapshot differs','Ran 2 tests in ',
                        'FAILED (failures=2)']:
            self.assertIn(visible,text)
        for hidden in ['private-engine','private-inputs','ordinary progress']:
            self.assertNotIn(hidden,text)

    def test_successful_unittest_totals_remain_silent(self):
        output=io.StringIO()
        diagnostics=runner.ErrorLogFilter(runner.LogSanitizer(self.environment()))
        with contextlib.redirect_stdout(output):
            for line in ['Ran 10 tests in 0.1s', '', 'OK', 'ordinary progress']:
                diagnostics.write(line)
        self.assertEqual(output.getvalue(),'')


if __name__=='__main__':
    unittest.main()
