import subprocess

def run_git(cmd):
    result = subprocess.run(cmd, shell=True, cwd='/home/yinhaolang/simulators/minesim', capture_output=True, text=True)
    print(f"CMD: {cmd}")
    print(f"STDOUT:\n{result.stdout}")
    print(f"STDERR:\n{result.stderr}")
    print(f"RETURN CODE: {result.returncode}\n")

run_git('git checkout -b feature/pipeline-modeling')
run_git('GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=no" git push -u origin feature/pipeline-modeling')
