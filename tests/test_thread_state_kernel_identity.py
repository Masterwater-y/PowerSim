import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
HEADER=ROOT/'gem5-fs/taogen/shared/thread_state_kernel_identity.hh'
SHA='da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174'


class KernelInitialTaskTest(unittest.TestCase):
    def test_static_boot_task_only_for_exact_kernel_and_address(self):
        self.assertTrue(HEADER.exists(),'exact-build static boot-task identity missing')
        code='''#include "thread_state_kernel_identity.hh"
#include <cassert>
int main() {
 using fastsim::known_linux54_boot_task_pid;
 assert(known_linux54_boot_task_pid("SHA",0xffffffff81e11780ULL).value()==0);
 assert(!known_linux54_boot_task_pid("different-kernel",0xffffffff81e11780ULL));
 assert(!known_linux54_boot_task_pid("SHA",0xffff888000001000ULL));
}'''.replace('SHA',SHA)
        with tempfile.TemporaryDirectory(dir=ROOT/'tmp') as directory:
            d=Path(directory);(d/'test.cc').write_text(code)
            subprocess.run(['/opt/gcc-11/bin/g++','-std=c++17','-I'+str(HEADER.parent),
                            str(d/'test.cc'),'-o',str(d/'test')],check=True,capture_output=True)
            subprocess.run([str(d/'test')],check=True)

    def test_exact_kernel_elf_symbol_and_initial_pid_bytes(self):
        from elftools.elf.elffile import ELFFile
        path=Path('/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0')
        if not path.exists():self.skipTest('audited kernel not installed')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),SHA)
        with path.open('rb') as handle:
            elf=ELFFile(handle);symbol=elf.get_section_by_name('.symtab').get_symbol_by_name('init_task')[0]
            self.assertEqual(symbol['st_value'],0xffffffff81e11780)
            address=symbol['st_value']+0x758
            segments=[seg for seg in elf.iter_segments() if seg['p_type']=='PT_LOAD' and
                      seg['p_vaddr']<=address<seg['p_vaddr']+seg['p_filesz']]
            self.assertEqual(len(segments),1)
            offset=address-segments[0]['p_vaddr']
            self.assertEqual(segments[0].data()[offset:offset+4],b'\0'*4)


if __name__=='__main__':unittest.main()
