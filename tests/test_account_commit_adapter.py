"""Exercise the actual account commit handler without starting worker background services."""
import ast
from pathlib import Path
import subprocess
import tempfile
import unittest


class CommitAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'GameFusion.git'; self.path.mkdir()
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.path),*args],stderr=subprocess.DEVNULL).decode().strip()
        git('init');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        (self.path/'README.md').write_text('Fixture\n');git('add','README.md');git('commit','-m','Fixture commit')
        self.sha=git('rev-parse','HEAD')
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='process_tasks')
        handler=next(n for n in ast.walk(function) if isinstance(n,ast.If) and "params.get('account_api_read')" in ast.unparse(n.test))
        self.handler=compile(ast.Module(body=handler.body,type_ignores=[]),'worker-commit-handler','exec')

    def execute(self,paths,expected=None):
        result={}
        namespace=dict(REPOSITORIES=paths,repo_name='GameFusion.git',params=dict(commit_sha=self.sha,repo_path=expected),
                       action='get_commit_info',task_result=result)
        exec(self.handler,namespace)
        return result

    def test_legacy_registration_without_path_reads_unique_commit(self):
        r=self.execute([str(self.path)])
        self.assertEqual(r['result']['commit_evidence']['sha'],self.sha)
        self.assertEqual(r['result']['commit_evidence']['source'],'git-object')

    def test_ambiguous_names_are_rejected(self):
        other=Path(self.folder.name)/'other'/'GameFusion.git';other.mkdir(parents=True)
        self.assertIn('error',self.execute([str(self.path),str(other)]))

    def test_supplied_mismatched_path_is_rejected(self):
        self.assertIn('error',self.execute([str(self.path)],str(Path(self.folder.name)/'unregistered')))

    def test_explicit_registered_path_disambiguates(self):
        other=Path(self.folder.name)/'other'/'GameFusion.git';other.mkdir(parents=True)
        r=self.execute([str(self.path),str(other)],str(self.path))
        self.assertEqual(r['result']['commit_evidence']['sha'],self.sha)


if __name__=='__main__':unittest.main()
