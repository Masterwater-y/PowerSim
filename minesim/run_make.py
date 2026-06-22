import subprocess
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parent
cmd = ['make', '-C', str(repo_root)] + sys.argv[1:]
result = subprocess.run(cmd, capture_output=True, text=True)

with open(repo_root / 'make_error.txt', 'w') as f:
    f.write("CMD: " + " ".join(cmd) + "\n")
    f.write("RETURN CODE: " + str(result.returncode) + "\n")
    f.write("STDOUT:\n" + result.stdout + "\n")
    f.write("STDERR:\n" + result.stderr + "\n")

print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
