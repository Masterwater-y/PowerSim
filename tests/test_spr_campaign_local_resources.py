import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CampaignLocalResourceTest(unittest.TestCase):
    def test_local_resources_are_forwarded_to_all_cases(self):
        result = subprocess.run([
            sys.executable, str(ROOT / 'tools/run_spr_retired_cache_campaign.py'),
            '--output', str(ROOT / 'tmp/spr-local-resource-plan-only'),
            '--kernel', '/tmp/test-kernel', '--disk', '/tmp/test-disk',
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(len(plan['commands']), 4)
        for row in plan['commands'].values():
            command = row['capture']
            self.assertEqual(command[command.index('--kernel') + 1], '/tmp/test-kernel')
            self.assertEqual(command[command.index('--disk') + 1], '/tmp/test-disk')


if __name__ == '__main__':
    unittest.main()
