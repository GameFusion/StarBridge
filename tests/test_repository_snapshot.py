import sys,tempfile,unittest,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repository_snapshot import file_snapshot, readme_snapshot

class SnapshotTests(unittest.TestCase):
    def test_bare_tree_uses_requested_branch_and_handles_literal_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';bare=root/'repo.git'
            def git(*args):return subprocess.check_output(['git',*map(str,args)],stderr=subprocess.DEVNULL,text=True).strip()
            git('init','-b','main',work)
            git('-C',work,'config','user.name','Test');git('-C',work,'config','user.email','test@example.invalid')
            (work/'README.md').write_text('Main branch readme')
            (work/'a tab\tand newline\n.txt').write_text('literal path')
            git('-C',work,'add','.');git('-C',work,'commit','-m','Initial')
            git('-C',work,'checkout','-b','feature');(work/'README.md').write_text('Feature readme');git('-C',work,'commit','-am','Feature')
            git('clone','--bare',work,bare)
            files,size=file_snapshot(bare,'main')
            self.assertEqual({f['name'] for f in files},{'README.md','a tab\tand newline\n.txt'})
            self.assertEqual(size,len('Main branch readme')+len('literal path'))
            self.assertTrue(all(f['last_change']['message'] == 'Initial' for f in files))
            self.assertEqual(files[0]['latest_sha'],git('-C',bare,'rev-parse','main'))
            self.assertEqual(readme_snapshot(bare,'main'),'Main branch readme')
            self.assertEqual(readme_snapshot(bare,'feature'),'Feature readme')

if __name__=='__main__':unittest.main()
