# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Tests for AWS CloudFormation installer.
"""

from datetime import timedelta
import json
import os
from subprocess import check_call, check_output
import time

from twisted.internet import reactor
from twisted.internet.task import deferLater
from twisted.internet.defer import maybeDeferred

from twisted.python.filepath import FilePath
from eliot import Message


from ...common.runner import run_ssh
from ...common import gather_deferreds
from ...testtools import AsyncTestCase, async_runner
from ..testtools import Cluster, ControlService
from ...ca import treq_with_authentication, UserCredential
from ...apiclient import FlockerClient
from ...control.httpapi import REST_API_PORT

REGION = os.environ['REGION']
# CLIENT_IP = os.environ['CLIENT_IP']
# DOCKER_HOST = os.environ['DOCKER_HOST']
# NODE0 = os.environ['CLUSTER_NODE0']
# NODE1 = os.environ['CLUSTER_NODE1']
ACCESS_KEY_ID = os.environ['ACCESS_KEY_ID']
SECRET_ACCESS_KEY = os.environ['SECRET_ACCESS_KEY']

PARAMETERS = [
    {
        'ParameterKey': 'KeyName',
        'ParameterValue': os.environ['KEY_NAME']
    },
    {
        'ParameterKey': 'AccessKeyID',
        'ParameterValue': os.environ['ACCESS_KEY_ID']
    },
    {
        'ParameterKey': 'SecretAccessKey',
        'ParameterValue': os.environ['SECRET_ACCESS_KEY']
    }
]

COMPOSE_NODE0 = '/home/ubuntu/postgres/docker-compose-node0.yml'
COMPOSE_NODE1 = '/home/ubuntu/postgres/docker-compose-node1.yml'
RECREATE_STATEMENT = 'drop table if exists test; create table test(i int);'
INSERT_STATEMENT = 'insert into test values(1);'
SELECT_STATEMENT = 'select count(*) from test;'

CLOUDFORMATION_STACK_NAME = 'testinstallerstack'
S3_CLOUDFORMATION_TEMPLATE = (
    'https://s3.amazonaws.com/'
    'installer.downloads.clusterhq.com/flocker-cluster.cloudformation.json'
)


def remote_command(node, *args):
    print "REMOTE COMMAND"
    command_output = []
    d = run_ssh(
        reactor,
        'ubuntu',
        node,
        args,
        handle_stdout=command_output.append
    )
    d.addCallback(
        lambda process_result: (process_result, command_output)
    )
    return d


def remote_docker_compose(client_ip, docker_host, compose_file_path, *args):
    print "REMOTE DOCKER"

    docker_compose_output = []
    d = run_ssh(
        reactor,
        'ubuntu',
        client_ip,
        ('DOCKER_HOST={}'.format(docker_host),
         'docker-compose', '--file', compose_file_path) + args,
        handle_stdout=docker_compose_output.append
    )
    d.addCallback(
        lambda process_result: (process_result, docker_compose_output)
    )
    return d


def remote_postgres(client_ip, host, command):
    print "REMOTE PG"

    postgres_output = []
    d = deferLater(
        reactor,
        60,
        run_ssh,
        reactor,
        'ubuntu',
        client_ip,
        ('psql', 'postgres://flocker:flocker@' + host + ':5432',
         '--command={}'.format(command)),
        handle_stdout=postgres_output.append
    )
    d.addCallback(
        lambda process_result: (process_result, postgres_output)
    )
    return d


def cleanup(test_case, local_certs_path):
    print "CLEANING UP"

    certificates_path = FilePath(local_certs_path)
    cluster_cert = certificates_path.child(b"cluster.crt")
    user_cert = certificates_path.child(b"user1.crt")
    user_key = certificates_path.child(b"user1.key")
    user_credential = UserCredential.from_files(user_cert, user_key)
    cluster = Cluster(
        control_node=ControlService(public_address=test_case.node0.encode("ascii")),
        nodes=[],
        treq=treq_with_authentication(
            reactor, cluster_cert, user_cert, user_key),
        client=FlockerClient(reactor, test_case.node0.encode("ascii"),
                             REST_API_PORT, cluster_cert, user_cert, user_key),
        certificates_path=certificates_path,
        cluster_uuid=user_credential.cluster_uuid,
    )
    d_node1_compose = remote_docker_compose(COMPOSE_NODE0, 'stop')
    d_node1_compose.addCallback(
        lambda ignored: remote_docker_compose(
            COMPOSE_NODE0, 'rm', '-f'
        )
    )

    d_node2_compose = remote_docker_compose(COMPOSE_NODE1, 'stop')
    d_node2_compose.addCallback(
        lambda ignored: remote_docker_compose(
            COMPOSE_NODE1, 'rm', '-f'
        )
    )

    d = gather_deferreds([d_node1_compose, d_node2_compose])

    d.addCallback(
        lambda ignored: cluster.clean_nodes()
    )

    d.addCallback(
        lambda ignored: delete_cloudformation_stack(test_case.stack_id)
    )
    return d


def get_stack_report(stack_id):
    output = check_output(
        ['aws', '--region', REGION, 'cloudformation', 'describe-stacks',
         '--stack-name', stack_id]
    )
    results = json.loads(output)
    return results['Stacks'][0]


def wait_for_stack_status(stack_id, target_status, time_limit=600):
    start_time = time.time()
    while True:
        stack_report = get_stack_report(stack_id)
        current_status = stack_report['StackStatus']
        if current_status == target_status:
            return stack_report
        time_running = time.time() - start_time
        if time_running > time_limit:
            Message.new(
                message='Timeout waiting for stack target status',
                stack_id=stack_id,
                target_status=target_status,
                current_status=current_status,
            ).write()
            return False
        else:
            Message.new(
                message='Waiting for stack target status',
                stack_id=stack_id,
                target_status=target_status,
                current_status=current_status,
                time_running=time_running,
            ).write()

            time.sleep(10)


def create_cloudformation_stack():
    """
    """
    # Request stack creation.

    output = check_output(
        ['aws', '--region', REGION, 'cloudformation', 'create-stack',
         '--parameters', json.dumps(PARAMETERS),
         '--stack-name', CLOUDFORMATION_STACK_NAME + str(int(time.time())),
         '--template-body', S3_CLOUDFORMATION_TEMPLATE]
    )

    output = json.loads(output)
    stack_id = output['StackId']
    Message.new(cloudformation_stack_id=stack_id)
    stack_report = wait_for_stack_status(stack_id, 'CREATE_COMPLETE')
    return stack_report


def delete_cloudformation_stack(stack_id):
    """
    """
    print "DELETING STACK"
    result = get_stack_report(stack_id)
    outputs = result['Outputs']
    s3_bucket_name = get_output(outputs, 'S3BucketName')
    check_call(
        ['aws', 's3', 'rb', 's3://{}'.format(s3_bucket_name), '--force']
    )

    check_call(
        ['aws', 'cloudformation', 'delete-stack',
         '--stack-name', stack_id]
    )

    return wait_for_stack_status(stack_id, 'DELETE_COMPLETE')


def get_output(outputs, key):
    for output in outputs:
        if output['OutputKey'] == key:
            return output['OutputValue']


class DockerComposeTests(AsyncTestCase):
    """
    Tests for AWS CloudFormation installer.
    """
    run_tests_with = async_runner(timeout=timedelta(minutes=10))

    def setUp(self):
        print "CREATING STACK"
        stack_report = create_cloudformation_stack()
        outputs = stack_report['Outputs']
        self.stack_id = stack_report['StackId']
        self.client_ip = get_output(outputs, 'ClientIP')
        self.node0 = get_output(outputs, 'FlockerNode0IP')
        self.node1 = get_output(outputs, 'FlockerNode1IP')
        self.docker_host = self.node0 + ':2376'
        local_certs_path = self.mktemp()
        self.addCleanup(cleanup, self, local_certs_path)
        print "WAITING FOR CLIENT NODE"
        time.sleep(120)
        check_call(
            ['scp', '-o', 'StrictHostKeyChecking no', '-r',
             'ubuntu@{}:/etc/flocker'.format(self.client_ip),
             local_certs_path]
        )
        d = maybeDeferred(super(DockerComposeTests, self).setUp)
        d.addCallback(lambda ignored: cleanup(self, local_certs_path))
        return d

    def test_docker_compose_up_postgres(self):
        """
        """
        d = remote_docker_compose(
            self.client_ip, self.docker_host, COMPOSE_NODE0, 'up', '-d'
        )

        d.addCallback(
            lambda ignored: remote_postgres(
                self.client_ip, self.node0,
                RECREATE_STATEMENT + INSERT_STATEMENT
            )
        )

        d.addCallback(
            lambda ignored: remote_postgres(
                self.client_ip, self.node0, SELECT_STATEMENT
            )
        )

        d.addCallback(
            lambda ignored: remote_docker_compose(
                self.client_ip, self.docker_host, COMPOSE_NODE0, 'stop'
            )
        )

        d.addCallback(
            lambda ignored: remote_docker_compose(
                self.client_ip, self.docker_host,
                COMPOSE_NODE0, 'rm', '--force'
            )
        )

        d.addCallback(
            lambda ignored: remote_docker_compose(
                self.client_ip, self.docker_host,
                COMPOSE_NODE1, 'up', '-d'
            )
        )

        d.addCallback(
            lambda ignored: remote_postgres(
                self.client_ip,
                self.node1, SELECT_STATEMENT
            )
        )

        d.addCallback(
            lambda (process_status, process_output): self.assertEqual(
                "1", process_output[2].strip()
            )
        )

        return d