import subprocess

result = subprocess.run(['make', '-C', '/home/yinhaolang/simulators/minesim/build'], capture_output=True, text=True)
with open('/home/yinhaolang/simulators/minesim/make_error.txt', 'w') as f:
    f.write("RETURN CODE: " + str(result.returncode) + "\n")
    f.write("STDOUT:\n" + result.stdout + "\n")
    f.write("STDERR:\n" + result.stderr + "\n")
