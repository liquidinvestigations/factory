#!/usr/bin/env python3

import sys
import os
import pty
from time import time, sleep
import json
import random
from pathlib import Path
import socket
import shutil
from tempfile import TemporaryDirectory
import subprocess
from contextlib import contextmanager
from argparse import ArgumentParser, REMAINDER
import shlex

"""
reference:
* http://kitchen.ci
* https://github.com/esmil/kitchen-qemu
* https://help.ubuntu.com/community/UEC/Images#Ubuntu_Cloud_Guest_images_on_12.04_LTS_.28Precise.29_and_beyond_using_NoCloud
* http://ubuntu-smoser.blogspot.ro/2013/02/using-ubuntu-cloud-images-without-cloud.html
"""

SSH_PRIVKEY = '''\
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACDPUAQxsWJNjIyRzGt9FLdeuv7OtWJNYnk592l4wJ57zwAAAJDgnRRK4J0U
SgAAAAtzc2gtZWQyNTUxOQAAACDPUAQxsWJNjIyRzGt9FLdeuv7OtWJNYnk592l4wJ57zw
AAAEBAAQzlJCFP03EyDr5D6ssyBshQ+1dvDYaZFXqkasWEs89QBDGxYk2MjJHMa30Ut166
/s61Yk1ieTn3aXjAnnvPAAAACW1nYXhAdHVmYQECAwQ=
-----END OPENSSH PRIVATE KEY-----
'''

SSH_PUBKEY = ('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM9QBDG'
              'xYk2MjJHMa30Ut166/s61Yk1ieTn3aXjAnnvP factory')


class Paths:

    def __init__(self, repo):
        self.repo = repo
        self.IMAGES = repo / 'images'
        self.VAR = repo / 'var'
        self.QEMU_HACKED_ARM = code_repo / 'qemu-hacked-arm'


code_repo = Path(__file__).resolve().parent
paths = Paths(code_repo)


def get_arch():
    return subprocess.check_output(['uname', '-m']).decode('latin1').strip()

def echo_run(cmd):
    print('+', *cmd)
    subprocess.run(cmd, check=True)

@contextmanager
def cd(path):
    prev = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(prev)

def kill_qemu_via_qmp(qmp_path):
    qmp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    qmp.connect(qmp_path)
    # https://wiki.qemu.org/Documentation/QMP
    qmp.sendall(b'{"execute": "qmp_capabilities"}\n')
    qmp.sendall(b'{"execute": "quit"}\n')
    qmp.close()


class PtyProcessError(RuntimeError):
    pass


@contextmanager
def pty_process(command):
    """
    Start subprocess in a new PTY. Helps with the interactive ssh password
    prompt.
    """

    (pid, fd) = pty.fork()
    if not pid:
        os.execv(command[0], command)

    try:
        yield fd

    finally:
        (_, exit_code) = os.waitpid(pid, 0)
        if exit_code != 0:
            raise PtyProcessError()


def pty_ssh(remote, port, password, command):
    ssh_args = [
        '/usr/bin/ssh', remote,
        '-p', str(port),
        '-o', 'NumberOfPasswordPrompts=1',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=known_hosts',
        command,
    ]

    with pty_process(ssh_args) as fd:
        while True:
            try:
                output = os.read(fd, 1024)
            except:
                return

            if b'password:' in output.lower().strip():
                os.write(fd, password.encode('utf8') + b'\n')

        while True:
            try:
                print(os.read(fd, 1024))
            except:
                return


DEFAULT_LOGIN = {
    'username': 'ubuntu',
    'password': 'ubuntu',
}


@contextmanager
def instance(platform, options):
    platform_home = paths.IMAGES / platform

    if not paths.VAR.is_dir():
        paths.VAR.mkdir()

    with TemporaryDirectory(prefix='vm-', dir=str(paths.VAR)) as var:
        vm = VM(platform_home, Path(var), options)
        vm.setup_var()

        with vm.boot():
            yield vm


class VM:

    def __init__(self, platform_home, var, options):
        self.platform_home = platform_home
        self.var = var
        self.options = options

        config_json = self.platform_home / 'config.json'
        if config_json.is_file():
            with config_json.open(encoding='utf8') as f:
                self.config = json.load(f)
        else:
            self.config = {}

        self.login = self.config.get('login', DEFAULT_LOGIN)
        self.remote = '{}@localhost'.format(self.login['username'])
        self.port = random.randint(1025, 65535)

        self.shares = []
        for i, s in enumerate(self.options.share):
            (path, mountpoint) = s.split(':')
            self.shares.append((i, Path(path).resolve(), mountpoint))

    def setup_var(self):
        with (self.var / 'id_ed25519').open('w', encoding='latin1') as f:
            f.write(SSH_PRIVKEY)
        (self.var / 'id_ed25519').chmod(0o600)

        local_disk = self.var / 'local-disk.img'
        subprocess.run([
            'qemu-img', 'create', '-q',
            '-f', 'qcow2',
            '-b', str(self.platform_home / 'disk.img'),
            str(local_disk),
        ], check=True, )

    def qemu_argv(self):
        arch = get_arch()
        qemu_binary = 'qemu-system-{}'.format(arch)

        if arch == 'aarch64':
            qemu_binary = str(paths.QEMU_HACKED_ARM)

        yield from [
            qemu_binary,
            '-daemonize',
            '-display', 'none',
            '-chardev', 'socket,id=mon-qmp,path=vm.qmp,server,nowait',
            '-mon', 'chardev=mon-qmp,mode=control,default',
            '-serial', 'mon:unix:path=vm.mon,server,nowait',
            '-m', str(self.options.memory),
            '-enable-kvm',
            '-cpu', 'host',
            '-smp', 'cpus={}'.format(self.options.smp),
        ]

        if arch == 'aarch64':
            yield from [
                '-bios', str(self.platform_home / 'arm-bios.fd'),
            ]

        netdev_arg = (
            'user,id=user,net=192.168.1.0/24,hostname=vm-factory'
            ',hostfwd=tcp:127.0.0.1:{}-:22'.format(self.port)
            + ''.join(
                ',hostfwd=tcp:127.0.0.1:{}-:{}'.format(*spec.split(':'))
                for spec in self.options.tcp
            )
            + ''.join(
                ',hostfwd=udp:127.0.0.1:{}-:{}'.format(*spec.split(':'))
                for spec in self.options.udp
            )
        )

        yield from [
            '-netdev', netdev_arg,
            '-device', 'virtio-net-pci,netdev=user',
        ]

        disk = (
            'if=none,id=drive0,snapshot=off,discard=unmap,detect-zeroes=unmap,'
            'file={}/local-disk.img'
            .format(self.var)
        )

        yield from [
            '-device', 'virtio-scsi-pci,id=scsi',
            '-device', 'scsi-hd,drive=drive0',
            '-drive', disk,
        ]

        for i, path, _ in self.shares:
            yield from [
                '-fsdev', 'local,id=fsdev{i},security_model=none,path={path}'
                    .format(i=i, path=path),
                '-device', 'virtio-9p-pci,fsdev=fsdev{i},mount_tag=path{i}'
                    .format(i=i),
            ]

    def vm_bootstrap_commands(self):
        yield from [
            'mkdir -p ~/.ssh',
            'echo "{}" >> ~/.ssh/authorized_keys'.format(SSH_PUBKEY),
            'chmod 700 ~/.ssh',
            'chmod 600 ~/.ssh/authorized_keys',
        ]

        for i, _, mountpoint in self.shares:
            quoted_mountpoint = shlex.quote(mountpoint)
            yield 'sudo mkdir -p {}'.format(quoted_mountpoint)
            yield (
                'sudo mount -t 9p -o trans=virtio path{} {} -oversion=9p2000.L'
                .format(i, quoted_mountpoint)
            )

    def vm_bootstrap(self, timeout=60):
        password = self.login['password']
        bootstrap = ' && '.join(self.vm_bootstrap_commands())
        t0 = time()
        while time() < t0 + timeout:
            try:
                sys.stdout.write('.')
                sys.stdout.flush()
                pty_ssh(self.remote, self.port, password, bootstrap)

            except PtyProcessError:
                sleep(1)
                continue

            else:
                sys.stdout.write(':)\n')
                sys.stdout.flush()
                return

        raise RuntimeError("VM not up after {} seconds".format(timeout))

    @contextmanager
    def boot(self):
        with cd(self.var):
            try:
                qemu = list(self.qemu_argv())
                subprocess.Popen(qemu)

                self.vm_bootstrap()

                yield

            finally:
                kill_qemu_via_qmp('vm.qmp')

    @staticmethod
    def invoke_ssh(cmd):
        subprocess.run(cmd, check=True)

    def ssh(self, cmd=None):
        ssh_command = [
            'ssh',
            self.remote,
            '-p', str(self.port),
            '-o', 'UserKnownHostsFile=known_hosts',
            '-o', 'ConnectTimeout=1',
            '-o', 'IdentitiesOnly=yes',
            '-i', 'id_ed25519',
        ]

        if cmd:
            ssh_command.append(cmd)

        self.invoke_ssh(ssh_command)


def add_vm_arguments(parser):
    parser.add_argument('--share', action='append', default=[])
    parser.add_argument('-m', '--memory', default=512, type=int)
    parser.add_argument('-p', '--smp', default=1, type=int)
    parser.add_argument('--tcp', action='append', default=[])
    parser.add_argument('--udp', action='append', default=[])


def run_factory(platform, *args):
    parser = ArgumentParser()
    add_vm_arguments(parser)
    parser.add_argument('args', nargs=REMAINDER)
    options = parser.parse_args(args)

    with instance(platform, options) as vm:
        args = ['sudo'] + options.args
        cmd = ' '.join(shlex.quote(a) for a in args)
        vm.ssh(cmd)


def login(platform, *args):
    parser = ArgumentParser()
    add_vm_arguments(parser)
    options = parser.parse_args(args)

    with instance(platform, options) as vm:
        vm.ssh()


CLOUD_INIT_YML = """\
#cloud-config
password: ubuntu
chpasswd: { expire: False }
ssh_pwauth: True
runcmd:
  - "dd if=/dev/zero of=/var/local/swap1 bs=1M count=2048"
  - "mkswap /var/local/swap1"
  - "echo '/var/local/swap1 none swap sw 0 0' >> /etc/fstab"
  - "touch /etc/cloud/cloud-init.disabled"
  - "systemctl disable apt-daily.service"
  - "systemctl disable apt-daily.timer"
  - "poweroff"
"""

def download_if_missing(path, url):
    if not path.is_file():
        echo_run(['wget', url, '-O', str(path), '-q'])

class BaseBuilder:

    def __init__(self, db_root, workbench):
        self.workbench = workbench
        self.db = db_root / self.name
        self.db.mkdir(exist_ok=True)
        self.disk = self.workbench / 'disk.img'
        upstream_image_name = self.upstream_image_url.rsplit('/', 1)[-1]
        self.upstream_image = self.db / upstream_image_name

    def download(self):
        download_if_missing(self.upstream_image, self.upstream_image_url)

    def unpack_upstream(self):
        echo_run(['qemu-img', 'convert', '-O', 'qcow2',
                    str(self.upstream_image), str(self.disk)])

        echo_run(['qemu-img', 'resize', str(self.disk), '10G'])

    def create_cloud_init_image(self):
        self.cloud_init_yml = self.workbench / 'cloud-init.yml'
        self.cloud_init_img = self.workbench / 'cloud-init.img'
        with self.cloud_init_yml.open('w', encoding='utf8') as f:
            f.write(CLOUD_INIT_YML)

        echo_run([
            'cloud-localds',
            str(self.cloud_init_img),
            str(self.cloud_init_yml),
        ])

    def cleanup(self):
        self.cloud_init_img.unlink()
        self.cloud_init_yml.unlink()

    def build(self):
        self.download()
        self.unpack_upstream()
        self.create_cloud_init_image()
        self.run_qemu()
        self.cleanup()


class Builder_x86_64(BaseBuilder):

    name = 'cloud-x86_64'

    upstream_image_url = (
        'https://cloud-images.ubuntu.com/server/releases/16.04/release/'
        'ubuntu-16.04-server-cloudimg-amd64-disk1.img'
    )

    def run_qemu(self):
        echo_run([
            'qemu-system-x86_64',
            '-enable-kvm',
            '-nographic',
            '-m', '512',
            '-netdev', 'user,id=user',
            '-device', 'virtio-net-pci,netdev=user',
            '-drive', 'index=0,media=disk,file=' + str(self.disk),
            '-drive', 'index=1,media=disk,format=raw,file='
                + str(self.cloud_init_img),
        ])


class Builder_arm64(BaseBuilder):

    name = 'cloud-arm64'

    upstream_image_url = (
        'https://cloud-images.ubuntu.com/server/releases/16.04/release/'
        'ubuntu-16.04-server-cloudimg-arm64-uefi1.img'
    )

    bios_url = (
        'https://releases.linaro.org/components/kernel/uefi-linaro/15.12/'
        'release/qemu64/QEMU_EFI.fd'
    )

    def __init__(self, *args):
        super().__init__(*args)
        self.arm_bios_fd = self.workbench / 'arm-bios.fd'

    def download(self):
        download_if_missing(self.arm_bios_fd, self.bios_url)
        super().download()

    def run_qemu(self):
        echo_run([
            'qemu-system-aarch64',
            '-cpu', 'host',
            '-enable-kvm',
            '-nographic',
            '-m', '512',
            '-machine', 'virt',
            '-bios', str(self.arm_bios_fd),
            '-netdev', 'user,id=user',
            '-device', 'virtio-net-pci,netdev=user,romfile=',
            '-device', 'virtio-blk-device,drive=image',
            '-drive', 'if=none,id=image,file=' + str(self.disk),
            '-device', 'virtio-blk-device,drive=cloud-init',
            '-drive', 'if=none,id=cloud-init,format=raw,file='
                + str(self.cloud_init_img),
        ])

PLATFORMS = {
    'cloud-x86_64': Builder_x86_64,
    'cloud-arm64': Builder_arm64,
}

def prepare_cloud_image(platform, *args):
    parser = ArgumentParser()
    parser.add_argument('--db', default=str(Path.home() / '.factory'))
    options = parser.parse_args(args)

    print("Preparing factory image for", platform)
    builder_cls = PLATFORMS[platform]

    db_root = Path(options.db)
    db_root.mkdir(exist_ok=True)

    workbench = paths.repo / 'images' / platform
    workbench.mkdir()
    try:
        builder_cls(db_root, workbench).build()
    except:
        shutil.rmtree(str(workbench))
        raise

COMMANDS = {
    'run': run_factory,
    'login': login,
    'prepare-cloud-image': prepare_cloud_image,
}

DEFAULTS = {
    'x86_64': 'cloud-x86_64',
    'aarch64': 'cloud-arm64',
}


def main(argv):
    arch = get_arch()
    if arch in DEFAULTS.keys():
        default_platform = DEFAULTS[arch]
    else:
        raise RuntimeError("Architecture {} not supported.".format(arch))

    platform_list = [x.name for x in paths.IMAGES.iterdir() if x.is_dir()]

    parser = ArgumentParser()
    parser.add_argument('--platform',
                        choices=platform_list,
                        default=default_platform)
    parser.add_argument('command', choices=COMMANDS.keys())
    (options, args) = parser.parse_known_args(argv)
    COMMANDS[options.command](options.platform, *args)