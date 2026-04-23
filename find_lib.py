import subprocess
import sys

try:
    result = subprocess.run('find /data00/home/yinhaolang/pkgs/dynamorio -name "libdynamorio.so"', shell=True, capture_output=True, text=True)
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)
except Exception as e:
    print("Error:", e)
