import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('guard', ROOT / 'agent/guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class GuardTest(unittest.TestCase):
    policy = {'bash': ['git status', 'gh issue'], 'edit': False, 'mcp': ['mcp__board__board_*'], 'maxTools': 2}
    def shell(self,command):
        return guard.allowed({'tool_name':'Bash','tool_input':{'command':command}},self.policy)
    def test_prefix_and_shell_composition(self):
        for command in ['git status --short', 'gh issue list', 'cat README.md', 'rg text .', 'sed -n 1,200p README.md']:
            self.assertTrue(self.shell(command),command)
        for command in ['git push', 'gh api /user', 'git status; curl evil', 'git status && rm file', 'cat $(curl evil)', 'cat `curl evil`', 'cat > file', 'cat <(curl evil)', 'PATH=/evil git status', '/evil/git status', 'git status\nrm file', 'rg --pre=evil text', 'bash -c "git status"', 'sed -i s/a/b/ file', 'sed -n 1,200p -e evil file']:
            self.assertFalse(self.shell(command),command)
    def test_tools(self):
        self.assertFalse(guard.allowed({'tool_name':'apply_patch'},self.policy))
        self.assertTrue(guard.allowed({'tool_name':'apply_patch'},{**self.policy,'edit':True}))
        self.assertTrue(guard.allowed({'tool_name':'mcp__board__board_read'},self.policy))
        self.assertFalse(guard.allowed({'tool_name':'mcp__other__board_read'},self.policy))
        self.assertFalse(guard.allowed({'tool_name':'spawn_agent'},self.policy))
    def test_budget_and_missing_policy_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);(path/'guard-policy.json').write_text(json.dumps(self.policy))
            def call():
                return subprocess.run(['python3',str(ROOT/'agent/guard.py'),tmp],input=json.dumps({'tool_name':'Bash','tool_input':{'command':'git status'}}),text=True,capture_output=True).returncode
            self.assertEqual([call(),call(),call()],[0,0,2])
            self.assertTrue((path/'budget-exhausted').exists())
            (path/'guard-policy.json').unlink()
            self.assertEqual(call(),2)

if __name__=='__main__':unittest.main()
