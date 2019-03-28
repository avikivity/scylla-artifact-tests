#!/usr/bin/python

import os
import re
import logging
import threading
import datetime
from pkg_resources import parse_version
try:
    from check_version import CheckVersionDB
except:
    # Avocado may not copy check_version.py to VM
    print "failed to import CheckVersionDB"

from avocado import Test
from avocado import main
from avocado.utils import distro
from avocado.utils import process
from avocado.utils import path
from avocado.utils import service
from avocado.utils import wait
from avocado.utils import network

from avocado.utils.software_manager import AptBackend
from avocado.utils.software_manager import YumBackend
from avocado.utils.software_manager import DnfBackend
from avocado.utils.software_manager import ZypperBackend
from avocado.utils.software_manager import SystemInspector
from avocado.utils import path as utils_path


SCRIPTLET_FAILURE_LIST = []
TEST_PARAMS = {}


def _search_scriptlet_failure(result):
    global SCRIPTLET_FAILURE_LIST
    output = result.stdout + result.stderr
    failure_pattern = 'scriptlet failure in rpm package scylla.*'
    if re.search(failure_pattern, output):
        pkgs = []
        occurrences = re.findall(failure_pattern, output)
        for occurrence in occurrences:
            pkg = occurrence.split()[-1]
            pkgs.append(pkg)
            SCRIPTLET_FAILURE_LIST.append(pkg)


class ScyllaYumBackend(YumBackend):

    def install(self, name):
        """
        Installs package [name]. Handles local installs.
        """
        i_cmd = self.base_command + ' ' + 'install' + ' ' + name

        try:
            _search_scriptlet_failure(process.run(i_cmd, sudo=True))
            return True
        except process.CmdError:
            return False


class ScyllaDnfBackend(DnfBackend):

    def install(self, name):
        """
        Installs package [name]. Handles local installs.
        """
        i_cmd = self.base_command + ' ' + 'install' + ' ' + name

        try:
            _search_scriptlet_failure(process.run(i_cmd, sudo=True))
            return True
        except process.CmdError:
            return False


class ScyllaAptBackend(AptBackend):

    def __init__(self):
        process.run('sudo apt-get update')
        process.run('sudo apt-get install -y apt-transport-https', shell=True)
        super(ScyllaAptBackend, self).__init__()
        executable = utils_path.find_command('apt-get')
        self.base_command = executable + ' --yes'
        if TEST_PARAMS.get('allow_unauth', default=False):
            self.base_command += ' --allow-unauthenticated'

    def upgrade(self, name=None):
        """
        Upgrade all packages of the system with eventual new versions.

        Optionally, upgrade individual packages.

        :param name: optional parameter wildcard spec to upgrade
        :type name: str
        """
        def update_pkg_list():
            ud_command = 'update'
            ud_cmd = self.base_command + ' ' + ud_command
            try:
                process.system(ud_cmd)
                return True
            except process.CmdError:
                return False

        wait.wait_for(update_pkg_list, timeout=300, step=30,
                      text="Wait until package list is up to date...")

        if name:
            up_command = 'install --only-upgrade'
            up_cmd = " ".join([self.base_command, self.dpkg_force_confdef,
                               up_command, name])
        else:
            up_command = 'upgrade'
            up_cmd = " ".join([self.base_command, self.dpkg_force_confdef,
                               up_command])

        try:
            process.system(up_cmd, shell=True, sudo=True)
            return True
        except process.CmdError:
            return False


class ScyllaSoftwareManager(object):

    """
    Package management abstraction layer.

    It supports a set of common package operations for testing purposes, and it
    uses the concept of a backend, a helper class that implements the set of
    operations of a given package management tool.
    """

    def __init__(self):
        """
        Lazily instantiate the object
        """
        self.initialized = False
        self.backend = None
        self.lowlevel_base_command = None
        self.base_command = None
        self.pm_version = None

    def _init_on_demand(self):
        """
        Determines the best supported package management system for the given
        operating system running and initializes the appropriate backend.
        """
        if not self.initialized:
            inspector = SystemInspector()
            backend_type = inspector.get_package_management()
            backend_mapping = {'apt-get': ScyllaAptBackend,
                               'yum': ScyllaYumBackend,
                               'dnf': ScyllaDnfBackend,
                               'zypper': ZypperBackend}

            if backend_type not in backend_mapping.keys():
                raise NotImplementedError('Unimplemented package management '
                                          'system: %s.' % backend_type)

            backend = backend_mapping[backend_type]
            self.backend = backend()
            self.initialized = True

    def __getattr__(self, name):
        self._init_on_demand()
        return self.backend.__getattribute__(name)


class StartServiceError(Exception):
    pass


class StopServiceError(Exception):
    pass


class RestartServiceError(Exception):
    pass


class InstallPackageError(Exception):
    pass


def get_scylla_logs():
    try:
        journalctl_cmd = path.find_command('journalctl')
        process.run('sudo %s -f --no-tail '
                    '-u scylla-io-setup.service '
                    '-u scylla-server.service '
                    '-u scylla-ami-setup.service '
                    '-u scylla-housekeeping-daily.service '
                    '-u scylla-housekeeping-restart.service '
                    '-u scylla-jmx.service' % journalctl_cmd,
                    verbose=True, ignore_status=True)
    except path.CmdNotFoundError:
        process.run('tail -f /var/log/syslog | grep scylla', shell=True,
                    verbose=True, ignore_status=True)


class ScyllaServiceManager(object):

    def __init__(self):
        self.services = ['scylla-server', 'scylla-jmx']
        self.start_time = None

    def _scylla_service_is_up(self):
        srv_manager = service.ServiceManager()
        try:
            journalctl_cmd = path.find_command('journalctl')
            timestamp = '--since "%s"' % self.start_time if self.start_time else ''
            result = process.run('sudo %s --no-tail '
                                 '-u scylla-io-setup.service '
                                 '-u scylla-server.service '
                                 '-u scylla-ami-setup.service '
                                 '-u scylla-housekeeping-daily.service '
                                 '-u scylla-housekeeping-restart.service '
                                 '-u scylla-jmx.service '
                                 '%s' % (journalctl_cmd, timestamp),
                                 ignore_status=True)
        except path.CmdNotFoundError:
            result = process.run('cat /var/log/syslog | grep scylla', shell=True,
                                 ignore_status=True)
        error_list = ['I/O Scheduler is not properly configured!', 'Failed to start Scylla Server', 'Failed to write into /proc/irq']
        for err in error_list:
            if err in result.stdout:
                raise StartServiceError('Fail to start scylla-server, err: %s' % err)

        for srv in self.services:
            srv_manager.status(srv)
        return not network.is_port_free(9042, 'localhost')

    def wait_services_up(self):
        service_start_timeout = 900
        output = wait.wait_for(func=self._scylla_service_is_up,
                               timeout=service_start_timeout, step=5)
        if output is None:
            e_msg = 'Scylla service does not appear to be up after %s s' % service_start_timeout
            raise StartServiceError(e_msg)

    def start_services(self):
        self.start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.start(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to start service %s '
                         '(see logs for details)' % srv)
                raise StartServiceError(e_msg)

    def stop_services(self):
        srv_manager = service.ServiceManager()
        for srv in reversed(self.services):
            srv_manager.stop(srv)
        for srv in self.services:
            if srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to stop service %s '
                         '(see logs for details)' % srv)
                raise StopServiceError(e_msg)

    def restart_services(self):
        self.start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        srv_manager = service.ServiceManager()
        for srv in self.services:
            srv_manager.restart(srv)
        for srv in self.services:
            if not srv_manager.status(srv):
                if service.get_name_of_init() == 'systemd':
                    process.run('journalctl -xe', ignore_status=True, verbose=True)
                e_msg = ('Failed to restart service %s '
                         '(see logs for details)' % srv)
                raise RestartServiceError(e_msg)


class ScyllaInstallGeneric(object):

    def __init__(self, sw_repo=None):
        self.base_url = 'https://s3.amazonaws.com/downloads.scylladb.com/'
        self.sw_manager = ScyllaSoftwareManager()
        self.sw_repo_src = sw_repo
        self.sw_repo_dst = None
        self.log = logging.getLogger('avocado.test')
        self.srv_manager = ScyllaServiceManager()
        self.is_enterprise = None

    def scylla_pkg(self):
        return 'scylla'

    def is_systemd(self):
        result = process.run("cat /proc/1/comm")
        return 'systemd' in result.stdout

    def try_report_uuid(self):
        uuid_path = '/var/lib/scylla-housekeeping/housekeeping.uuid'
        mark_path = '/var/lib/scylla-housekeeping/housekeeping.uuid.marked'
        cmd = 'curl "https://i6a5h9l1kl.execute-api.us-east-1.amazonaws.com/prod/check_version?uu=%s&mark=scylla"'
        wait.wait_for(lambda: os.path.exists(uuid_path), timeout=30, step=5,
                      text='Waiting for housekeeping.uuid generated')

        assert os.path.exists(uuid_path), "housekeeping.uuid doesn't exist"

        if os.path.exists(uuid_path) and not os.path.exists(mark_path):
            with open(uuid_path) as uuid_file:
                uuid = uuid_file.read().strip()
            self.log.debug('housekeeping.uuid is %s', uuid)
            process.run(cmd % uuid, shell=True, verbose=True)
            process.run('sudo -u scylla touch %s' % mark_path, verbose=True)

    def download_scylla_repo(self):
        if self.uuid:
            last_id = self.cvdb.get_last_id(self.uuid, self.repoid, self.version)
        process.run('sudo curl %s -o %s -L' % (self.sw_repo_src, self.sw_repo_dst),
                    shell=True)
        if self.uuid:
            assert self.cvdb.check_new_record(self.uuid, self.repoid, self.version, last_id)

    def run(self):
        wait.wait_for(self.sw_manager.upgrade, timeout=300, step=30,
                      text="Wait until system is up to date...")
        # setup software repo and other environment before install test packages
        pkgs = self.env_setup()
        # check install
        if self.uuid:
            version = self.version.replace('scylladb-', '')
            last_id = self.cvdb.get_last_id(self.uuid, self.repoid, self.version, table='housekeeping.repodownload', add_filter="and file_name like 'scylla%server%{}%'".format(version))
        for pkg in pkgs:
            if not self.sw_manager.install(pkg):
                e_msg = ('Package %s could not be installed '
                         '(see logs for details)' % os.path.basename(pkg))
                raise InstallPackageError(e_msg)
        # check install
        if self.uuid:
            assert self.cvdb.check_new_record(self.uuid, self.repoid, self.version, last_id, table='housekeeping.repodownload', add_filter="and file_name like 'scylla%server%{}%'".format(version))

        # enable raid setup when second disk exists
        result = process.run('ip -o link show', shell=True, verbose=True)
        result = process.run('ip -o link show |grep ether |awk -F": " \'{print $2}\'', shell=True, verbose=True)
        devname = result.stdout.strip()
        setup_cmd = '/usr/lib/scylla/scylla_setup --nic %s' % devname
        result = process.run('ls /dev/[hvs]db', shell=True, ignore_status=True)
        devlist = result.stdout.split()

        if devlist and not os.path.ismount('/var/lib/scylla'):
            setup_cmd += ' --disks %s' % devlist[-1]
        else:
            setup_cmd += ' --no-raid-setup'

        if os.path.exists('/usr/bin/node_exporter'):
            setup_cmd += ' --no-node-exporter'

        # disable cpuscaling setup for known issue:
        # https://github.com/scylladb/scylla/issues/2051
        with open('/usr/lib/scylla/scylla_setup', 'r') as f:
            script_content = f.read()
        if '--no-cpuscaling-setup' in script_content:
            setup_cmd += ' --no-cpuscaling-setup'
        # check setup
        if self.uuid:
            version = self.version.replace('scylladb-', '')
            last_id = self.cvdb.get_last_id_v2("select * from housekeeping.checkversion where repoid='{}' and ruid='{}' and version like '{}%' and statuscode='i'".format(self.repoid, self.uuid, version))
        process.run(setup_cmd, shell=True, verbose=True, timeout=600)
        # check setup
        if self.uuid:
            assert self.cvdb.check_new_record_v2("select * from housekeeping.checkversion where repoid='{}' and ruid='{}' and version like '{}%' and statuscode='i'".format(self.repoid, self.uuid, version), last_id)

        self.srv_manager.start_services()
        self.srv_manager.wait_services_up()
        self.try_report_uuid()

        # verify SELinux setup on Red Hat variants
        detected_distro = distro.detect()
        distro_name = detected_distro.name.lower()
        distro_version = detected_distro.version
        is_debian_variant = 'ubuntu' in distro_name or 'debian' in distro_name
        if not is_debian_variant:
            result = process.run('getenforce')
            assert 'Enforcing' not in result.stdout, "SELinux is still actived"
        # verify node_exporter install
        assert os.path.exists('/usr/bin/node_exporter'), "node_exporter isn't installed"
        # verify raid setup
        if devlist:
            assert os.path.ismount('/var/lib/scylla'), "RAID setup failed, scylla directory isn't mounted rightly"
        # verify ntp
        if is_debian_variant:
            process.run('service ntp status')
        else:
            process.run('systemctl status ntpd')
        # verify coredump setup
        if self.is_systemd() and 'debian' not in distro_name:
            result = process.run('coredumpctl info', ignore_status=True)
            assert 'No coredumps found.' == result.stderr.strip(), "Coredump info doesn't work"
            if devlist:
                coredump_err = "Coredump directory isn't pointed to raid disk"
                assert os.path.realpath('/var/lib/systemd/coredump') == '/var/lib/scylla/coredump', coredump_err
        elif distro_name == 'debian' and distro_version == '9':
            result = process.run('sysctl kernel.core_pattern')
            assert 'systemd-coredump' in result.stdout
        else:
            result = process.run('sysctl kernel.core_pattern')
            assert 'scylla_save_coredump' in result.stdout
        # verify io and sysconfig setup
        if self.is_systemd():
            process.run('systemctl status scylla-server')
            #process.run('systemctl status scylla-housekeeping-restart.timer')
        else:
            result = process.run('service scylla-server status')
            assert 'running' in result.stdout


class ScyllaInstallDebian(ScyllaInstallGeneric):

    def __init__(self, sw_repo):
        super(ScyllaInstallDebian, self).__init__(sw_repo)
        self.sw_repo_dst = '/etc/apt/sources.list.d/scylla.list'

    def prepare_extend_repo(self):
        raise NotImplementedError

    def install_java18(self, args=''):
        # fixme: update the version for enterprise in future when it requests java 1.8
        result = process.run('sudo apt-cache show {}'.format(self.scylla_pkg()))
        ver = re.findall("Version: ([\d.]+)", result.stdout)[0].strip('.')
        request_ver = '2017.666' if self.is_enterprise else '1.7'
        if parse_version(ver) < parse_version(request_ver):
            self.log.info("Java 1.8 isn't requested by current version {}".format(ver))
            return
        process.run('sudo apt-get update')
        process.run('sudo apt-get install -y openjdk-8-jre-headless {}'.format(args), shell=True)
        process.run('sudo update-java-alternatives -s java-1.8.0-openjdk-amd64', shell=True)

    def scylla_pkg(self):
        """
        Get package name, compat both of scylla and scylla-enterprise.
        """
        if self.is_enterprise is None:
            result = process.run('sudo apt-cache search scylla-enterprise')
            self.is_enterprise = True if 'scylla-enterprise' in result.stdout else False
        return 'scylla-enterprise' if self.is_enterprise else 'scylla'


class ScyllaInstallUbuntu1404(ScyllaInstallDebian):
    def prepare_extend_repo(self):
        process.run('sudo apt-get install software-properties-common -y', shell=True)
        process.run('sudo add-apt-repository -y ppa:openjdk-r/ppa', shell=True)
        process.run('sudo add-apt-repository -y ppa:scylladb/ppa', shell=True)

    def env_setup(self):
        self.download_scylla_repo()
        self.prepare_extend_repo()
        process.run('sudo apt-get update')
        self.install_java18()
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallUbuntu1604(ScyllaInstallDebian):
    def prepare_extend_repo(self):
        process.run('sudo apt-get install software-properties-common -y', shell=True)
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 6B2BFD3660EF3F5B")
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 17723034C56D4B19")
        process.run('sudo add-apt-repository -y ppa:scylladb/ppa', shell=True)

    def env_setup(self):
        self.download_scylla_repo()
        self.prepare_extend_repo()
        process.run('sudo apt-get update')
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallUbuntu1804(ScyllaInstallDebian):
    def prepare_extend_repo(self):
        process.run('sudo apt-get install software-properties-common -y', shell=True)
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 6B2BFD3660EF3F5B")
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 17723034C56D4B19")
        process.run('sudo add-apt-repository -y ppa:scylladb/ppa', shell=True)

    def env_setup(self):
        self.download_scylla_repo()
        self.prepare_extend_repo()
        process.run('sudo apt-get update')
        self.install_java18()
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallDebian8(ScyllaInstallDebian):
    def __init__(self, sw_repo):
        process.run("sed -i -e 's/jessie-updates/stable-updates/g' /etc/apt/sources.list", shell=True)
        super(ScyllaInstallDebian8, self).__init__(sw_repo)

    def prepare_extend_repo(self):
        process.run("echo 'deb http://archive.debian.org/debian jessie-backports main' > /etc/apt/sources.list.d/jessie-backports.list", shell=True)
        process.run("echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99jessie-backports", shell=True)
        process.run("apt-get install gnupg-curl -y")
        process.run("apt-key adv --fetch-keys https://download.opensuse.org/repositories/home:/scylladb:/scylla-3rdparty-jessie/Debian_8.0/Release.key")
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 17723034C56D4B19")
        process.run("echo 'deb http://download.opensuse.org/repositories/home:/scylladb:/scylla-3rdparty-jessie/Debian_8.0/ /' > /etc/apt/sources.list.d/scylla-3rdparty.list", shell=True)

    def env_setup(self):
        self.download_scylla_repo()
        self.prepare_extend_repo()
        process.run('sudo apt-get update')
        self.install_java18(args=' -t jessie-backports')
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallDebian9(ScyllaInstallDebian):
    def prepare_extend_repo(self):
        process.run("apt-get install gnupg1-curl dirmngr -y")
        process.run("apt-key adv --fetch-keys https://download.opensuse.org/repositories/home:/scylladb:/scylla-3rdparty-stretch/Debian_9.0/Release.key")
        process.run("sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 17723034C56D4B19")
        process.run("echo 'deb http://download.opensuse.org/repositories/home:/scylladb:/scylla-3rdparty-stretch/Debian_9.0/ /' > /etc/apt/sources.list.d/scylla-3rdparty.list")

    def env_setup(self):
        self.download_scylla_repo()
        self.prepare_extend_repo()
        process.run('sudo apt-get update')
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallFedora(ScyllaInstallGeneric):

    def __init__(self, sw_repo):
        super(ScyllaInstallFedora, self).__init__(sw_repo)
        self.sw_repo_dst = '/etc/yum.repos.d/scylla.repo'


class ScyllaInstallFedora22(ScyllaInstallFedora):

    def env_setup(self):
        self.download_scylla_repo()
        self.sw_manager.upgrade()
        return ['scylla']


class ScyllaInstallCentOS(ScyllaInstallGeneric):

    def __init__(self, sw_repo):
        super(ScyllaInstallCentOS, self).__init__(sw_repo)
        self.sw_repo_dst = '/etc/yum.repos.d/scylla.repo'

    def _centos_remove_system_packages(self):
        self.sw_manager.remove('boost-thread')
        self.sw_manager.remove('boost-system')
        self.sw_manager.remove('abrt')

    def scylla_pkg(self):
        """
        Get package name, compat both of scylla and scylla-enterprise.
        """
        if self.is_enterprise is None:
            result = process.run('sudo yum search scylla-enterprise')
            self.is_enterprise = True if 'scylla-enterprise.x86_64' in result.stdout else False
        return 'scylla-enterprise' if self.is_enterprise else 'scylla'


class ScyllaInstallCentOS7(ScyllaInstallCentOS):

    def env_setup(self):
        self._centos_remove_system_packages()
        self.download_scylla_repo()
        self.sw_manager.upgrade()
        return [self.scylla_pkg()]


class ScyllaInstallAMI(ScyllaInstallGeneric):
    def check_used_driver(self, expected_driver=''):
        """
        Make sure VPS is enabled and enhanced network driver is used.
        """
        with open('/sys/class/net/eth0/address', 'r') as f:
            address = f.read().strip()
        result = process.run('curl -s http://169.254.169.254/latest/meta-data/network/interfaces/macs/%s/' % address)
        assert 'vpc-id' in result.stdout, 'VPC is not enabled! debug: %s' % result.stdout

        # check driver information
        result = process.run('ethtool -i eth0')
        used_driver = re.findall('^driver:\s(.*)', result.stdout)[0]
        err = "Enhanced networking isn't enabled, current driver: %s, expected: %s" % (used_driver, expected_driver)
        assert expected_driver == used_driver, err
        self.log.info("Enhanced networking isn enabled, current driver: %s" % used_driver)

    def enhanced_net_enabled(self):
        """
        Check the enhanced network is enabled for some special instances.
        """
        result = process.run('curl -s http://169.254.169.254/latest/meta-data/instance-type')
        instance_type = result.stdout
        if '.' in result.stdout:
            maintype, subtype = result.stdout.split('.')
        else:
            maintype = result.stdout
            subtype = ''

        # Referenced: https://github.com/scylladb/scylla/commit/b8f40a2d
        if maintype in ['i3', 'p2', 'r4', 'x1'] or (maintype == 'm4' and subtype == '16xlarge'):
            self.check_used_driver('ena')
        elif maintype in ['c3', 'c4', 'd2', 'i2', 'r3'] or (maintype == 'm4'):
            self.check_used_driver('ixgbevf')
        else:
            process.run('ethtool -i eth0', verbose=True)
            self.log.info("The instance (%s) doesn't support enahanced networking!", result.stdout)

        result = process.run('scylla --version')
        ver = re.findall("(\d+.\d+)", result.stdout)[0]
        request_ver = '2017.666' if self.is_enterprise else '2.0'

        conf_dict = {
            'i3.large': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0-1 "'],
            'i3.xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0-3 "'],
            'i3.2xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0-7 "'],
            'i3.4xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 1-7,9-15 "'],
            'i3.8xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 1-15,17-31 "'],
            'i3.16xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 1-31,33-63 "'],
            'c3.large': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0-1 "'],
            'c3.8xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 1-15,17-31 "'],
            'm3.2xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0-7 "'],
            'm3.medium': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 0 "'],
            'i2.4xlarge': ['SEASTAR_IO="--io-properties-file=/etc/scylla.d/io_properties.yaml"', 'CPUSET="--cpuset 1-7,9-15 "']
        }

        result = process.run('cat /etc/scylla.d/io.conf |grep -v \#',
                             shell=True,
                             ignore_status=True,
                             verbose=True)
        io_conf = result.stdout.strip()

        result = process.run('cat /etc/scylla.d/cpuset.conf |grep -v \#',
                             shell=True,
                             ignore_status=True,
                             verbose=True)
        cpuset_conf = result.stdout.strip()

        if parse_version(ver) >= parse_version(request_ver) and instance_type in conf_dict.keys():
            assert io_conf == conf_dict[instance_type][0]
            assert cpuset_conf == conf_dict[instance_type][1]
            self.log.info("io.conf and cpuset.conf are all good.")

        result = process.run('cat /proc/interrupts |grep eth', shell=True, verbose=True)
        affinity_list = []
        for i in re.findall("\s(\d+):", result.stdout):
            result = process.run('sudo cat /proc/irq/{}/smp_affinity'.format(i), verbose=True)
            if parse_version(ver) >= parse_version(request_ver) and maintype == 'i3':
                if subtype in ['large', 'xlarge', '2xlarge']:
                    assert result.stdout not in affinity_list, 'smp affinity of different interrupt should be different'
                else:
                    assert len(set(affinity_list)) <= 2
            affinity_list.append(result.stdout)

    def run(self):
        self.log.info("Testing AMI, let's just check if the DB is up...")
        self.srv_manager.wait_services_up()
        self.enhanced_net_enabled()
        self.try_report_uuid()


class ScyllaArtifactSanity(Test):

    """
    Sanity check of the build artifacts (deb, rpm)

    setup: Install artifacts (deb, rpm)
    1) Run cassandra-stress
    2) Run nodetool
    """
    setup_done_file = None
    srv_manager = ScyllaServiceManager()
    cvdb = None
    uuid = None
    repoid = None
    version = None

    def get_setup_file_done(self):
        tmpdir = os.path.dirname(self.workdir)
        return os.path.join(tmpdir, 'scylla-setup-done')

    def scylla_setup(self):
        global TEST_PARAMS
        # Let's start the logs thread before package install
        self._log_collection_thread = threading.Thread(target=get_scylla_logs)
        self._log_collection_thread.start()
        sw_repo = self.params.get('sw_repo', default=None)
        ami = self.params.get('ami', default=False) is True
        TEST_PARAMS = self.params

        detected_distro = distro.detect()
        fedora_22 = (detected_distro.name.lower() == 'fedora' and
                     detected_distro.version == '22')
        ubuntu_14_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '14' and
                        detected_distro.release == '04')
        ubuntu_16_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '16' and
                        detected_distro.release == '04')
        ubuntu_18_04 = (detected_distro.name.lower() == 'ubuntu' and
                        detected_distro.version == '18' and
                        detected_distro.release == '04')
        debian_8 = (detected_distro.name.lower() == 'debian' and
                    detected_distro.version == '8')
        debian_9 = (detected_distro.name.lower() == 'debian' and
                    detected_distro.version == '9')
        centos_7 = (detected_distro.name.lower() in ['centos', 'redhat'] and
                    detected_distro.version == '7')

        installer = None

        if ami:
            installer = ScyllaInstallAMI()

        elif ubuntu_14_04:
            installer = ScyllaInstallUbuntu1404(sw_repo=sw_repo)

        elif ubuntu_16_04:
            installer = ScyllaInstallUbuntu1604(sw_repo=sw_repo)

        elif ubuntu_18_04:
            installer = ScyllaInstallUbuntu1804(sw_repo=sw_repo)

        elif debian_8:
            installer = ScyllaInstallDebian8(sw_repo=sw_repo)

        elif debian_9:
            installer = ScyllaInstallDebian9(sw_repo=sw_repo)

        elif fedora_22:
            installer = ScyllaInstallFedora22(sw_repo=sw_repo)

        elif centos_7:
            installer = ScyllaInstallCentOS7(sw_repo=sw_repo)

        else:
            self.skip('Unsupported OS: %s' % detected_distro)

        installer.cvdb = self.cvdb
        installer.uuid = self.uuid
        installer.repoid = self.repoid
        installer.version = self.version

        installer.run()
        os.mknod(self.get_setup_file_done())

    def setUp(self):
        if self.params.get('host') and self.params.get('user') and self.params.get('passwd'):
            self.cvdb = CheckVersionDB(self.params.get('host'),
                                       self.params.get('user'),
                                       self.params.get('passwd'))
        self.sw_repo = self.params.get('sw_repo', default='')
        priv_repo_flag = re.findall("https://repositories.scylladb.com/scylla/repo/(.*scylladb-[\d\.]+).*\.\w+", self.sw_repo)
        if priv_repo_flag:
            self.uuid, self.repoid, self.version = priv_repo_flag[0].split('/')
            assert self.cvdb, 'check version db must be connected for private repo'
        if not os.path.isfile(self.get_setup_file_done()):
            self.scylla_setup()

    def run_cassandra_stress(self):
        def check_output(result):
            output = result.stdout + result.stderr
            lines = output.splitlines()
            for line in lines:
                if 'java.io.IOException' in line:
                    self.fail('cassandra-stress: %s' % line.strip())
        cassandra_stress_exec = path.find_command('cassandra-stress')
        stress_populate = ('%s write n=10000 -mode cql3 native -pop seq=1..10000' %
                           cassandra_stress_exec)
        result_populate = process.run(stress_populate, timeout=600)
        check_output(result_populate)
        stress_mixed = ('%s mixed duration=1m -mode cql3 native '
                        '-rate threads=10 -pop seq=1..10000' %
                        cassandra_stress_exec)
        result_mixed = process.run(stress_mixed, shell=True, timeout=300)
        check_output(result_mixed)

    def run_nodetool(self):
        nodetool_exec = path.find_command('nodetool')
        nodetool = '%s status' % nodetool_exec
        process.run(nodetool)

    def test_after_install(self):
        self.run_nodetool()
        self.run_cassandra_stress()
        if SCRIPTLET_FAILURE_LIST:
            self.fail('RPM scriptlet failure reported for package(s): %s' %
                      ",".join(SCRIPTLET_FAILURE_LIST))

    def test_after_stop_start(self):
        self.srv_manager.stop_services()
        self.srv_manager.start_services()
        self.srv_manager.wait_services_up()
        self.run_nodetool()
        self.run_cassandra_stress()

    def test_after_restart(self):
        # check restart
        if self.uuid:
            version = self.version.replace('scylladb-', '')
            last_id = self.cvdb.get_last_id_v2("select * from housekeeping.checkversion where ruid='{}' and repoid='{}' and version like '{}%' and statuscode='r'".format(self.uuid, self.repoid, version))
        self.srv_manager.restart_services()
        self.srv_manager.wait_services_up()
        # check restart
        if self.uuid:
            assert self.cvdb.check_new_record_v2("select * from housekeeping.checkversion where ruid='{}' and repoid='{}' and version like '{}%' and statuscode='r'".format(self.uuid, self.repoid, version), last_id)
        self.run_nodetool()
        self.run_cassandra_stress()


if __name__ == '__main__':
    main()
