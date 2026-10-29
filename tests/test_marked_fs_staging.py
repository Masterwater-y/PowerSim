import ast
import base64
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT/'scripts/run_gem5_sync_probe.py').read_text())
namespace = {}
safe_imports = [node for node in tree.body if isinstance(node, ast.Import)
                and all(alias.name in ('base64', 'os', 'shlex') for alias in node.names)]
exec(compile(ast.Module(body=safe_imports+[node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name == '_b64_heredoc'], type_ignores=[]), '<staging>', 'exec'), namespace)


class MarkedStagingTest(unittest.TestCase):
    def test_encoded_temporary_stays_on_destination_filesystem(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            root = Path(directory)
            original, staged = root/'input', root/'output with spaces'
            original.write_bytes(bytes(range(256))*4096)
            script = namespace['_b64_heredoc'](str(original), str(staged))
            self.assertLess(len(script),original.stat().st_size//4,
                            'readfile script duplicates the entire uncompressed input on the small guest root disk')
            self.assertFalse('/tmp/stage.b64' in script, 'large staging escapes destination tmpfs')
            subprocess.run(['/bin/sh'], input=script, text=True, check=True)
            self.assertEqual(staged.read_bytes(), original.read_bytes())
            self.assertFalse((root/'.fastsim-stage.b64').exists())
            self.assertFalse((root/'.fastsim-stage.gz').exists())


if __name__ == '__main__':
    unittest.main()
