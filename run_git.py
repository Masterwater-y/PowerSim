import subprocess

def run_git(cmd):
    result = subprocess.run(cmd, shell=True, cwd='/home/yinhaolang/simulators/minesim', capture_output=True, text=True)
    print(f"CMD: {cmd}")
    print(f"STDOUT:\n{result.stdout}")
    print(f"STDERR:\n{result.stderr}")
    print(f"RETURN CODE: {result.returncode}\n")

run_git('git config --global user.email "bot@trae.ai"')
run_git('git config --global user.name "Trae Assistant"')
run_git('git init')
run_git('git add .')
run_git('git commit -m "feat: implement IntervalCore with LQ/SQ, STLF, RS limits and serialization instructions"')
