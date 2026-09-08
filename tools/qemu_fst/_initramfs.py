"""Build the minimal QEMU-FST initramfs."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


INITRAMFS_NAME = "initramfs.cpio.gz"

INIT_SOURCE = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/reboot.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define VALUE_SIZE 256
#define PATH_SIZE 4096
#define VECTOR_BYTES (64 * 1024)
#define VECTOR_ITEMS 256

static void fail(const char *message)
{
    fprintf(stderr, "[qemu-fst-init] ERROR: %s: %s\n", message,
            strerror(errno));
    sync();
    reboot(LINUX_REBOOT_CMD_POWER_OFF);
    _exit(2);
}

static int command_line_value(const char *name, char *value, size_t value_size)
{
    FILE *file = fopen("/proc/cmdline", "r");
    char command_line[4096];
    if (!file)
        return -1;
    if (!fgets(command_line, sizeof(command_line), file)) {
        fclose(file);
        return -1;
    }
    fclose(file);
    const size_t name_size = strlen(name);
    for (char *cursor = command_line; *cursor;) {
        while (*cursor == ' ')
            ++cursor;
        if (!strncmp(cursor, name, name_size) && cursor[name_size] == '=') {
            cursor += name_size + 1;
            const size_t length = strcspn(cursor, " \n");
            if (!length || length >= value_size)
                return -1;
            memcpy(value, cursor, length);
            value[length] = '\0';
            return 0;
        }
        cursor += strcspn(cursor, " ");
    }
    return -1;
}

static char **read_vector(const char *path, char *storage, size_t storage_size,
                          char **items, size_t item_count)
{
    int file = open(path, O_RDONLY | O_CLOEXEC);
    if (file < 0)
        fail(path);
    ssize_t size = read(file, storage, storage_size - 1);
    if (size < 0)
        fail(path);
    if (read(file, storage + size, 1) != 0) {
        errno = E2BIG;
        fail(path);
    }
    close(file);
    storage[size] = '\0';
    size_t count = 0;
    for (char *cursor = storage; cursor < storage + size;) {
        if (count + 1 >= item_count) {
            errno = E2BIG;
            fail(path);
        }
        items[count++] = cursor;
        cursor += strlen(cursor) + 1;
    }
    items[count] = NULL;
    return items;
}

int main(void)
{
    mkdir("/proc", 0555);
    mkdir("/sys", 0555);
    mkdir("/dev", 0755);
    mkdir("/mnt", 0755);
    mkdir("/mnt/workloads", 0555);
    mkdir("/work", 0755);
    if (mount("proc", "/proc", "proc", 0, "") != 0)
        fail("mount proc");
    if (mount("sysfs", "/sys", "sysfs", 0, "") != 0)
        fail("mount sysfs");
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, "mode=0755") != 0)
        fail("mount devtmpfs");
    if (mount("tmpfs", "/work", "tmpfs", 0, "mode=0755,size=2G") != 0)
        fail("mount tmpfs");

    char workload[VALUE_SIZE];
    if (command_line_value("QEMU_FST_WORKLOAD", workload, sizeof(workload)) ||
        strchr(workload, '/') || strstr(workload, "..")) {
        errno = EINVAL;
        fail("QEMU_FST_WORKLOAD");
    }
    for (int attempt = 0; attempt < 100; ++attempt) {
        if (mount("/dev/vda", "/mnt/workloads", "ext4",
                  MS_NOSUID | MS_NODEV, "") == 0)
            break;
        if (attempt == 99)
            fail("mount workload disk");
        usleep(100000);
    }

    char case_root[PATH_SIZE];
    char program[PATH_SIZE];
    char descriptor[PATH_SIZE];
    snprintf(case_root, sizeof(case_root), "/mnt/workloads/cases/%s",
             workload);

    char argv_storage[VECTOR_BYTES];
    char env_storage[VECTOR_BYTES];
    char *argv[VECTOR_ITEMS];
    char *envp[VECTOR_ITEMS];
    snprintf(descriptor, sizeof(descriptor), "%s/argv", case_root);
    read_vector(descriptor, argv_storage, sizeof(argv_storage), argv,
                VECTOR_ITEMS);
    snprintf(descriptor, sizeof(descriptor), "%s/env", case_root);
    read_vector(descriptor, env_storage, sizeof(env_storage), envp,
                VECTOR_ITEMS);

    fprintf(stderr, "[qemu-fst-init] workload=%s uid=1000 gid=1000\n",
            workload);

    pid_t child = fork();
    if (child < 0)
        fail("fork");
    if (!child) {
        snprintf(program, sizeof(program), "%s/program", case_root);
        fprintf(stderr, "[qemu-fst-init] exec=%s cwd=%s\n", program,
                case_root);
        fflush(stderr);
        if (chdir(case_root) != 0 || setgroups(0, NULL) != 0 ||
            setgid(1000) != 0 || setuid(1000) != 0)
            fail("drop privileges");
        execve(program, argv, envp);
        fail(program);
    }
    int status = 0;
    if (waitpid(child, &status, 0) < 0)
        fail("waitpid");
    const int result = WIFEXITED(status) ? WEXITSTATUS(status) : 128;
    fprintf(stderr, "[qemu-fst-init] workload=%s status=%d\n", workload,
            result);
    sync();
    reboot(LINUX_REBOOT_CMD_POWER_OFF);
    while (1)
        pause();
}
"""


def build_initramfs(staging: Path) -> Path:
    root = staging / "initramfs-root"
    root.mkdir()
    source = root / "init.c"
    source.write_text(INIT_SOURCE, encoding="utf-8")
    subprocess.run(
        ["gcc", "-O2", "-static", "-o", str(root / "init"), str(source)],
        check=True,
    )
    source.unlink()
    output = staging / INITRAMFS_NAME
    with output.open("wb") as archive:
        cpio = subprocess.Popen(
            ["cpio", "--null", "-o", "--format=newc"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        gzip = subprocess.Popen(
            ["gzip", "-9"], stdin=cpio.stdout, stdout=archive
        )
        assert cpio.stdin is not None
        cpio.stdin.write(b".\0./init\0")
        cpio.stdin.close()
        assert cpio.stdout is not None
        cpio.stdout.close()
        if cpio.wait() or gzip.wait():
            raise RuntimeError("failed to build QEMU-FST initramfs")
    shutil.rmtree(root)
    return output
