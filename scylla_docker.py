#!/usr/bin/python

import json
import time
import logging
from avocado import Test
from avocado.utils import process
from avocado import main

log = logging.getLogger('scylla_docker')


class DockerCommandError(Exception):
    pass


class DockerContainerNotExists(Exception):
    pass


class ScyllaDocker(object):
    """
    Implements methods for deploying scylla with docker
    """

    def __init__(self, *args, **kwargs):
        self._image = kwargs.get('image', 'scylladb/scylla-nightly')
        self._node_cnt = kwargs.get('node_cnt', 1)
        self._seed_name = 'node1'
        self._nodes = list()
        self._start_timeout = kwargs.get('start_timeout', 30)

    @property
    def nodes(self):
        return self._nodes

    @staticmethod
    def _cmd(cmd, timeout=10, sudo=False):
        res = process.run('docker {}'.format(cmd), ignore_status=True, timeout=timeout, sudo=sudo)
        if res.exit_status:
            if 'No such container:' in res.stderr:
                raise DockerContainerNotExists(res.stderr)
            raise DockerCommandError('command: {}, error: {}, output: {}'.format(cmd, res.stderr, res.stdout))
        return res.stdout

    def clean_old_images(self):
        images = self._cmd('images -f "dangling=true" -q')
        if images:
            images_str = ' '.join(images.split())
            self._cmd('rmi {}'.format(images_str), timeout=90)

    def update_image(self):
        log.debug('update scylla image')
        self._cmd('pull {}'.format(self._image), timeout=600)
        try:
            self.clean_old_images()
        except Exception as ex:
            log.debug(ex)

    def get_node_ip(self, node_name):
        out = self._cmd("inspect --format='{{{{ .NetworkSettings.IPAddress }}}}' {}".format(node_name))
        return out.strip()

    def node_status(self, node):
        out = self._cmd("inspect --format='{{{{json .State.Running}}}}' {}".format(node))
        return json.loads(out)

    def start_node(self, node):
        self._cmd('start {}'.format(node))

    def stop_node(self, node):
        self._cmd('stop {}'.format(node), timeout=30)

    def restart_node(self, node):
        self._cmd('restart {}'.format(node), timeout=30)

    def remove_node(self, node):
        self._cmd('rm {}'.format(node))

    def create_cluster(self):
        log.debug('create cluster')
        self._cmd('run --name {} -d {}'.format(self._seed_name, self._image))
        self.nodes.append(self._seed_name)
        if self._node_cnt > 1:
            seed_ip = self.get_node_ip(self._seed_name)
            for i in range(2, self._node_cnt + 1):
                node_name = '{}{}'.format(self._seed_name.strip('1'), i)
                self._cmd('run --name {} -d {} --seeds="{}"'.format(node_name, self._image, seed_ip))
                self.nodes.append(node_name)
        status = False
        try_cnt = 0
        while not status and try_cnt < 10:
            status = self.node_status(self._seed_name)
            time.sleep(2)
            try_cnt += 1
        if not self.wait_for_cluster_up():
            self.destroy_cluster()
            raise Exception('Failed to start cluster: timeout expired.')
        if not self.wait_for_cql_available():
            raise Exception('Failed to start cluster: timeout expired.')
        return self.nodes

    def wait_for_cluster_up(self):
        node_ips = [self.get_node_ip(name) for name in self.nodes]
        up_nodes = list()
        try_cnt = 0
        while len(up_nodes) < len(node_ips) and try_cnt < self._start_timeout:
            try:
                res = self.run_nodetool('status')
                for line in res.splitlines():
                    for ip in node_ips:
                        if ip in line and ip not in up_nodes:
                            if line.split()[0].strip() == 'UN':
                                up_nodes.append(ip)
            except Exception:
                pass
            time.sleep(2)
            try_cnt += 1
        return len(up_nodes) == len(node_ips)

    def wait_for_cql_available(self):
        try_cnt = 0
        output = ''
        while try_cnt < self._start_timeout:
            try:
                output = self._cmd('exec {} cqlsh -e help'.format(self._seed_name))
                log.debug(output)
                if 'CQL help topics' in output:
                    break
            except Exception as ex:
                log.debug(ex)
                pass
            time.sleep(1)
            try_cnt += 1
        return 'CQL help topics' in output

    def stop_cluster(self, system=False):
        log.debug('stop cluster')
        if system:
            self._cmd('exec {} systemctl stop scylla.service'.format(self._seed_name), sudo=True)
        else:
            for node in self.nodes:
                self.stop_node(node=node)

    def start_cluster(self, system=False):
        log.debug('start cluster')
        if system:
            self._cmd('exec {} systemctl start scylla.service'.format(self._seed_name), sudo=True)
        else:
            for node in self.nodes:
                self.start_node(node=node)

    def restart_cluster(self):
        self._cmd('exec {} systemctl restart scylla.service'.format(self._seed_name), sudo=True)

    def destroy_cluster(self):
        log.debug('destroy cluster')
        self.stop_cluster()
        for node in self.nodes:
            self.remove_node(node)

    def run_nodetool(self, cmd):
        log.debug('run nodetool %s' % cmd)
        return self._cmd('exec {} nodetool {}'.format(self._seed_name, cmd))

    def run_stress_test(self, opt, sub_opt, results=True):
        log.debug('run stress %s' % opt)
        out = self._cmd('exec {} cassandra-stress {} {} -node {}'.format(
            self._seed_name, opt, sub_opt, self.get_node_ip(self._seed_name)), timeout=60)
        return self.get_stress_results(out) if results else out

    @staticmethod
    def get_stress_results(stress_out):
        results = {}
        start = 0
        for line in stress_out.splitlines():
            if line.startswith('Results:'):
                start = 1
                continue
            elif line.startswith('END'):
                break
            if start and line.strip():
                try:
                    res = line.split(':')
                    key = res[0].strip()
                    val = res[1].split()[0].strip().replace(',', '') if key != 'Total operation time' else\
                        ':'.join([res[1], res[2], res[3]]).strip()
                    results[key] = float(val) if val != 'NaN' and key != 'Total operation time' else val
                except Exception as ex:
                    log.error('Failed parsing stress results: %s, error: %s', line, ex)
        return results


class ScyllaDockerSanity(Test):
    """
    Test scylla with docker

    :avocado: enable
    """
    def __init__(self, *args, **kwargs):
        super(ScyllaDockerSanity, self).__init__(*args, **kwargs)
        self.image = self.params.get('docker_image', default='scylladb/scylla-nightly')
        self.docker = None
        self.node_cnt = 2
        self.op_cnt = 300000
        self.start_timeout = self.params.get('start_timeout', default=30)

    def _cleanup(self):
        log.debug('cleanup cluster if exists')
        for i in range(1, self.node_cnt + 1):
            node_name = 'node{}'.format(i)
            for method in ('stop_node', 'remove_node'):
                try:
                    getattr(self.docker, method)(node_name)
                except DockerContainerNotExists:
                    pass

    def setUp(self):
        """
        Update scylla image, create cluster(cleanup if exists)
        """
        self.docker = ScyllaDocker(image=self.image, node_cnt=self.node_cnt, start_timeout=self.start_timeout)
        self.docker.update_image()
        self._cleanup()
        log.debug('Wait cluster timeup: {} seconds'.format(self.start_timeout))
        self.docker.create_cluster()

    def tearDown(self):
        """
        Destroy cluster
        """
        self.docker.destroy_cluster()

    def test_basic_stress(self):
        """
        Run nodetool and cassandra stress utilities
        """
        res = self.docker.run_nodetool('status')
        log.debug(res)
        res = self.docker.run_stress_test('write', 'cl=QUORUM n={} -schema replication(factor={}) -rate threads=10'
                                          .format(self.op_cnt, self.node_cnt))
        self.assertGreaterEqual(res['Total partitions'], self.op_cnt)
        self.assertEquals(int(res['Total errors']), 0)
        res = self.docker.run_stress_test('read', 'cl=QUORUM n={} -rate threads=10'.format(self.op_cnt))
        self.assertGreaterEqual(res['Total partitions'], self.op_cnt)
        self.assertEquals(int(res['Total errors']), 0)

    def test_stress_with_restart(self):
        """
        Run cassandra stress write, restart cluster, run stress read
        """
        res = self.docker.run_stress_test('write', 'cl=QUORUM n={} -schema replication(factor={}) -rate threads=10'
                                          .format(self.op_cnt, self.node_cnt))
        self.assertGreaterEqual(res['Total partitions'], self.op_cnt)
        self.assertEquals(int(res['Total errors']), 0)
        self.docker.stop_cluster()
        self.docker.start_cluster()
        if not self.docker.wait_for_cluster_up():
            raise Exception('Failed to start cluster: timeout expired.')
        if not self.docker.wait_for_cql_available():
            raise Exception('Failed to start cluster: timeout expired.')
        res = self.docker.run_stress_test('read', 'n={} -rate threads=10'.format(self.op_cnt))
        self.assertGreaterEqual(res['Total partitions'], self.op_cnt)
        self.assertEquals(int(res['Total errors']), 0)


if __name__ == '__main__':
    main()
