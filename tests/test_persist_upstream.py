"""Verify incremental upstream persistence without rebuilding the live site."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('workflow_runner',ROOT/'run_build.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class IncrementalSyncTests(unittest.TestCase):
    def test_initial_unchanged_add_modify_and_delete_are_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target'
            (source/'nested').mkdir(parents=True)
            (source/'one.txt').write_bytes(b'one')
            (source/'nested/two.txt').write_bytes(b'two')
            first=runner.sync_tree(source,target)
            self.assertEqual(first['copied_files'],2)
            self.assertEqual(first['copied_bytes'],6)
            state={path:((target/path).stat().st_ino,(target/path).stat().st_mtime_ns) for path in ['one.txt','nested/two.txt']}
            second=runner.sync_tree(source,target)
            self.assertEqual(sum(second.values()),0)
            self.assertEqual(state,{path:((target/path).stat().st_ino,(target/path).stat().st_mtime_ns) for path in state})
            (source/'three.txt').write_bytes(b'three')
            added=runner.sync_tree(source,target)
            self.assertEqual((added['copied_files'],added['copied_bytes']),(1,5))
            (source/'one.txt').write_bytes(b'ONE')
            stamp=(source/'one.txt').stat().st_mtime_ns+1_000_000_000
            os.utime(source/'one.txt',ns=((source/'one.txt').stat().st_atime_ns,stamp))
            modified=runner.sync_tree(source,target)
            self.assertEqual((modified['copied_files'],modified['copied_bytes']),(1,3))
            self.assertEqual((target/'one.txt').read_bytes(),b'ONE')
            (source/'three.txt').unlink()
            (target/'orphan.txt').write_bytes(b'orphan')
            deleted=runner.sync_tree(source,target)
            self.assertEqual(deleted['deleted_files'],2)
            self.assertFalse((target/'three.txt').exists())
            self.assertFalse((target/'orphan.txt').exists())

    def test_permissions_are_repaired_without_rewriting_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target'
            source.mkdir();(source/'script.sh').write_bytes(b'#!/bin/sh\n')
            (source/'script.sh').chmod(0o755)
            runner.sync_tree(source,target)
            (target/'script.sh').chmod(0o600)
            repaired=runner.sync_tree(source,target)
            self.assertEqual((repaired['copied_files'],repaired['metadata_files']),(0,1))
            self.assertEqual((target/'script.sh').stat().st_mode & 0o777,0o755)
            self.assertEqual(sum(runner.sync_tree(source,target).values()),0)

    def test_symlinks_are_rejected_or_replaced_without_following(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target';outside=root/'outside'
            source.mkdir();target.mkdir();outside.write_bytes(b'outside')
            (source/'safe.txt').write_bytes(b'inside')
            (target/'safe.txt').symlink_to(outside)
            runner.sync_tree(source,target)
            self.assertTrue((target/'safe.txt').is_file())
            self.assertFalse((target/'safe.txt').is_symlink())
            self.assertEqual(outside.read_bytes(),b'outside')
            (source/'link.txt').symlink_to(source/'safe.txt')
            with self.assertRaisesRegex(ValueError,'symlink'):
                runner.sync_tree(source,target)

    def test_git_metadata_under_target_root_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target'
            source.mkdir();(source/'keep.txt').write_text('keep')
            (target/'.git').mkdir(parents=True);(target/'.git/config').write_text('local')
            runner.sync_tree(source,target)
            self.assertEqual((target/'.git/config').read_text(),'local')


class PersistUpstreamTests(unittest.TestCase):
    def git(self,root,*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.PIPE,text=True).strip()

    def repository(self,root):
        remote=root/'upstream.git';remote.mkdir()
        self.git(remote,'init','--quiet','--bare','--initial-branch=main')
        self.git(remote,'symbolic-ref','HEAD','refs/heads/main')
        seed=root/'seed';seed.mkdir()
        self.git(seed,'init','--quiet','--initial-branch=main')
        self.git(seed,'config','user.name','errbin')
        self.git(seed,'config','user.email','errbin@users.noreply.github.com')
        (seed/'README.md').write_text('Upstream snapshot.\n')
        self.git(seed,'add','README.md')
        self.git(seed,'commit','--quiet','-m','Initialize upstream snapshot')
        self.git(seed,'push','--quiet',remote.as_uri(),'HEAD:refs/heads/main')
        return remote

    def test_second_persist_is_zero_copy_and_creates_no_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);remote=self.repository(root);job=root/'job';job.mkdir()
            self.git(root,'clone','--quiet',remote.as_uri(),str(job/'upstream'))
            source=job/'runtime/sources/Author/Repo/main/rule.txt';source.parent.mkdir(parents=True);source.write_text('rule\n')
            captured=job/'captured-upstream/raw/Author/Repo/main/rule.txt';captured.parent.mkdir(parents=True);captured.write_text('rule\n')
            manifest=job/'captured-upstream/manifests/provenance.json';manifest.parent.mkdir(parents=True);manifest.write_text('{"version":1}\n')
            cache=job/'runtime/cache/icons/apple.json';cache.parent.mkdir(parents=True);cache.write_text('{"version":2}\n')
            data=job/'runtime/data/source-dates.json';data.parent.mkdir(parents=True);data.write_text('{}\n')
            icon=job/'runtime/web/assets/icons/Author/Repo/icon.png';icon.parent.mkdir(parents=True);icon.write_bytes(b'png')
            archive=types.ModuleType('tools.egern_hub.raw_archive')
            def snapshot(source_root,archive_root,*,stats=None):
                runner.sync_tree(source_root,archive_root/'raw',stats=stats)
            archive.archive_snapshot=snapshot
            packages={'tools':types.ModuleType('tools'),'tools.egern_hub':types.ModuleType('tools.egern_hub'),
                      'tools.egern_hub.raw_archive':archive}
            original=runner.sync_tree;observed=[]
            def recording(source_root,target,**kwargs):
                current={};kwargs['stats']=current
                result=original(source_root,target,**kwargs)
                observed.append(dict(current))
                return result
            path_before=list(sys.path)
            environment={'UPSTREAM_REPOSITORY':remote.as_uri(),'UPSTREAM_WRITE_KEY':'local-test-key'}
            try:
                with patch.object(runner,'job_root',return_value=job), \
                        patch.object(runner,'sync_tree',recording), \
                        patch.dict(sys.modules,packages), \
                        patch.dict(os.environ,environment):
                    runner.persist_upstream()
                    first=self.git(remote,'rev-parse','main')
                    observed.clear()
                    runner.persist_upstream()
                    second=self.git(remote,'rev-parse','main')
            finally:
                sys.path[:]=path_before
            self.assertEqual(first,second)
            self.assertEqual(sum(row['copied_files'] for row in observed),0)
            self.assertEqual(sum(row['metadata_files'] for row in observed),0)
            self.assertEqual(sum(row['deleted_files'] for row in observed),0)
            self.assertEqual(self.git(remote,'show','main:state/cache/icons/apple.json'),'{"version":2}')
            self.assertEqual(self.git(remote,'show','main:raw/Author/Repo/main/rule.txt'),'rule')


if __name__=='__main__':
    unittest.main()
