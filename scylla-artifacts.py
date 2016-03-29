#!/usr/bin/python

import os

from avocado import Test
from avocado import main
from avocado.utils import distro
from avocado.utils import software_manager
from avocado.utils import process
from avocado.utils import path
from avocado.utils import download
from avocado.utils import service
from avocado.utils import wait
from avocado.utils import network

CENTOS_REPOS = """
[scylla]
name=Scylla for Centos $releasever - $basearch
baseurl=https://s3.amazonaws.com/downloads.scylladb.com/rpm/centos/$releasever/$basearch/
enabled=1
gpgcheck=0

[scylla-generic]
name=Scylla for centos $releasever
baseurl=https://s3.amazonaws.com/downloads.scylladb.com/rpm/centos/$releasever/noarch/
enabled=1
gpgcheck=0

[scylla-3rdparty]
name=Scylla 3rdParty for Centos $releasever - $basearch
baseurl=https://s3.amazonaws.com/downloads.scylladb.com/rpm/3rdparty/centos/$releasever/$basearch/
enabled=1
gpgcheck=0

[scylla-3rdparty-generic]
name=Scylla 3rdParty for Centos $releasever
baseurl=https://s3.amazonaws.com/downloads.scylladb.com/rpm/3rdparty/centos/$releasever/noarch/
enabled=1
gpgcheck=0
"""


class ScyllaArtifactSanity(Test):

    """
    Sanity check of the build artifacts (deb, rpm)

    setup: Install artifacts (deb, rpm)
    1) Run cassandra-stress
    2) Run nodetool
    """
    setup_done_file = None
    services = ['scylla-server', 'scylla-jmx']

    def get_setup_file_done(self):
        tmpdir = os.path.dirname(self.workdir)
        return os.path.join(tmpdir, 'scylla-setup-done')

    def setup_ubuntu_14_04_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo, self.scylla_apt_repo), shell=True)
        self.sw_manager.upgrade()
        return ['scylla-server', 'scylla-jmx', 'scylla-tools']

    def setup_ubuntu_14_04_release(self):
        base_url = os.path.join(self.base_url, 'deb', 'ubuntu', 'dists',
                                'trusty', 'scylladb', 'multiverse',
                                'binary-amd64')
        scylla_server = 'scylla-server_0.13.1-20151210.59cd785-ubuntu1_amd64.deb'
        scylla_jmx = 'scylla-jmx_0.13.1-20151210.fab577c-ubuntu1_all.deb'
        scylla_tools = 'scylla-tools_0.13.1-20151210.76b1b01-ubuntu1_all.deb'
        thrift = 'libthrift0_0.9.1-ubuntu1_amd64.deb'
        debs = []

        debs_download_info = [(base_url, thrift),
                              (base_url, scylla_server),
                              (base_url, scylla_jmx),
                              (base_url, scylla_tools)]

        for b_url, deb in debs_download_info:
            src = os.path.join(b_url, deb)
            dst = os.path.join(self.outputdir, deb)
            debs.append(download.get_file(src, dst))

        return debs

    def setup_fedora_22_ci(self):
        process.run('sudo curl %s -o %s' % (self.sw_repo, self.scylla_yum_repo), shell=True)
        self.sw_manager.upgrade()
        return ['scylla-server', 'scylla-jmx', 'scylla-tools']

    def setup_fedora_22_release(self):
        x86_url = os.path.join(self.base_url, 'rpm', 'fedora', '22', 'x86_64')
        noarch_url = os.path.join(self.base_url, 'rpm', 'fedora', '22',
                                  'noarch')
        scylla_server = 'scylla-server-0.13.1-20151210.59cd785.fc22.x86_64.rpm'
        scylla_jmx = 'scylla-jmx-0.13.1-20151211.fab577c.fc22.noarch.rpm'
        scylla_tools = 'scylla-tools-0.13.1-20151211.76b1b01.fc22.noarch.rpm'
        rpms = []

        rpms_download_info = [(x86_url, scylla_server),
                              (noarch_url, scylla_jmx),
                              (noarch_url, scylla_tools)]

        for b_url, rpm in rpms_download_info:
            src = os.path.join(b_url, rpm)
            dst = os.path.join(self.outputdir, rpm)
            rpms.append(download.get_file(src, dst))

        return rpms

    def _centos_remove_boost(self):
        self.sw_manager.remove('boost-thread')
        self.sw_manager.remove('boost-system')

    def setup_centos_7_ci(self):
        self._centos_remove_boost()
        process.run('sudo curl %s -o %s' % (self.sw_repo, self.scylla_yum_repo), shell=True)
        self.sw_manager.upgrade()
        return ['scylla-server', 'scylla-jmx', 'scylla-tools']

    def setup_centos_7_release(self):
        self._centos_remove_boost()
        scylla_repo_fileobj = open(self.scylla_yum_repo, 'w')
        scylla_repo_fileobj.write(CENTOS_REPOS)
        self.sw_manager.upgrade()
        return ['scylla-server', 'scylla-jmx', 'scylla-tools']

    def setup_ami(self):
        self.log.info("Testing AMI, this system is supposed to be all set...")
        return []

    def _scylla_service_is_up(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.status(srv)
        return not network.is_port_free(9042, 'localhost')

    def wait_services_up(self):
        service_start_timeout = 900
        output = wait.wait_for(func=self._scylla_service_is_up,
                               timeout=service_start_timeout, step=5)
        if output is None:
            self.get_scylla_logs()
            self.error('Scylla service does not appear to be up after %s s' %
                       service_start_timeout)

    def scylla_setup(self):
        self.base_url = 'https://s3.amazonaws.com/downloads.scylladb.com/'
        self.scylla_yum_repo = '/etc/yum.repos.d/scylla.repo'
        self.scylla_apt_repo = '/etc/apt/sources.list.d/scylla.list'
        self.sw_repo = self.params.get('sw_repo', default=None)
        self.sw_manager = software_manager.SoftwareManager()
        self.srv_manager = None

        detected_distro = distro.detect()
        fedora_22 = (detected_distro.name.lower() == 'fedora' and
                     detected_distro.version == '22')
        ubuntu_14_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '14' and
                        detected_distro.release == '04')
        centos_7 = (detected_distro.name.lower() == 'centos' and
                    detected_distro.version == '7')
        pkgs = []

        mode = 'release'

        if self.sw_repo is not None:
            if self.sw_repo.strip() != 'EMPTY':
                mode = 'ci'

        ami = self.params.get('ami', default=False) is True
        if ami:
            pkgs = self.setup_ami()

        elif ubuntu_14_04:
            if mode == 'release':
                pkgs = self.setup_ubuntu_14_04_release()
            elif mode == 'ci':
                pkgs = self.setup_ubuntu_14_04_ci()

        elif fedora_22:
            if mode == 'release':
                pkgs = self.setup_fedora_22_release()
            elif mode == 'ci':
                pkgs = self.setup_fedora_22_ci()

        elif centos_7:
            if mode == 'release':
                pkgs = self.setup_centos_7_release()
            elif mode == 'ci':
                pkgs = self.setup_centos_7_ci()

        else:
            self.skip('Unsupported OS: %s' % detected_distro)

        # Users are expected to install scylla on up to date distros.
        # This might cause trouble from time to time, but it's better
        # than pretending that distro updates can't break our install.
        if not ami:
            self.sw_manager.upgrade()
        for pkg in pkgs:
            if not self.sw_manager.install(pkg):
                self.error('Package %s could not be installed '
                           '(see logs for details)' %
                           os.path.basename(pkg))

        if not ami:
            # Let's use very low/conservative io config numbers
            # as a workaround for now.
            process.run('echo "SEASTAR_IO=\'--max-io-requests=4 --num-io-queues=1\'" | sudo tee /etc/scylla.d/io.conf', shell=True)
            process.run('touch /etc/scylla/io_configured')
            self.start_services()

        self.wait_services_up()
        os.mknod(self.get_setup_file_done())

    def setUp(self):
        if not os.path.isfile(self.get_setup_file_done()):
            self.scylla_setup()

    def start_services(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.start(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                self.error('Failed to start service %s '
                           '(see logs for details)' % srv)

    def stop_services(self):
        srv_manager = service.ServiceManager()
        for srv in reversed(self.services):
            srv_manager.stop(srv)
        for srv in self.services:
            if srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                self.error('Failed to stop service %s '
                           '(see logs for details)' % srv)

    def restart_services(self):
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.restart(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                self.error('Failed to start service %s '
                           '(see logs for details)' % srv)

    def run_cassandra_stress(self):
        def check_output(result):
            output = result.stdout + result.stderr
            lines = output.splitlines()
            for line in lines:
                if 'Exception in thread' in line:
                    self.fail('cassandra-stress: %s' % line.strip())
        cassandra_stress_exec = path.find_command('cassandra-stress')
        stress_populate = ('%s write n=10000 -mode cql3 native' %
                           cassandra_stress_exec)
        result_populate = process.run(stress_populate)
        check_output(result_populate)
        stress_mixed = ('%s mixed duration=1m -mode cql3 native '
                        '-rate threads=10' % cassandra_stress_exec)
        result_mixed = process.run(stress_mixed)
        check_output(result_mixed)

    def get_scylla_logs(self):
        try:
            journalctl_cmd = path.find_command('journalctl')
            process.run('%s --unit scylla-io-setup.service' % journalctl_cmd,
                        ignore_status=True)
            process.run('%s --unit scylla-server.service' % journalctl_cmd,
                        ignore_status=True)
            process.run('%s --unit scylla-jmx.service' % journalctl_cmd,
                        ignore_status=True)
        except path.CmdNotFoundError:
            process.run('grep scylla /var/log/syslog', ignore_status=True)

    def run_nodetool(self):
        nodetool_exec = path.find_command('nodetool')
        nodetool = '%s status' % nodetool_exec
        process.run(nodetool)

    def test_after_install(self):
        try:
            self.run_nodetool()
            self.run_cassandra_stress()
        finally:
            self.get_scylla_logs()

    def test_after_stop_start(self):
        try:
            self.stop_services()
            self.start_services()
            self.wait_services_up()
            self.run_nodetool()
            self.run_cassandra_stress()
        finally:
            self.get_scylla_logs()

    def test_after_restart(self):
        try:
            self.restart_services()
            self.wait_services_up()
            self.run_nodetool()
            self.run_cassandra_stress()
        finally:
            self.get_scylla_logs()

if __name__ == '__main__':
    main()
