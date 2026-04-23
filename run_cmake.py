import subprocess

def run_cmd(cmd):
    print(f"Running: {cmd}")
    subprocess.run(cmd, shell=True, cwd='/home/yinhaolang/simulators/minesim/build', check=True)

try:
    run_cmd('cmake ..')
    run_cmd('make -j4')
except subprocess.CalledProcessError as e:
    print(f"Build failed with error: {e}")
