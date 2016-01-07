# -*- test-case-name: flocker.node.agents.functional.test_pd -*-
# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
A PD implementation of the ``IBlockDeviceAPI``.
"""

import requests

from bitmath import GiB, Byte
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from oauth2client.gce import AppAssertionCredentials
from socket import gethostname
from twisted.python.filepath import FilePath
from uuid import UUID
from zope.interface import implementer

from .blockdevice import (
    IBlockDeviceAPI, BlockDeviceVolume, AlreadyAttachedVolume, UnknownVolume,
    UnattachedVolume
)
from ...common import poll_until

# GCE instances have a metadata server that can be queried for information
# about the instance the code is being run on.
_METADATA_SERVER = u'http://169.254.169.254/computeMetadata/v1/'
_METADATA_HEADERS = {u'Metadata-Flavor': u'Google'}


def _get_metadata_path(path):
    """
    Requests a metadata path from the metadata server available within GCE.

    The metadata server is a good way to query information about the currently
    running instance and project it is in. It is also the mechanism used to
    inject ssh public keys and service account session tokens into the VM.

    :param unicode path: The path on the metadata server to query.

    :returns unicode: The resulting value from the metadata server.
    """
    timeout_sec = 3
    r = requests.get(_METADATA_SERVER + path,
                     headers=_METADATA_HEADERS,
                     timeout=timeout_sec)
    return r.text


# The prefix added to dataset_ids to turn them into blockdevice_ids.
_PREFIX = 'flocker-v1-'


def _blockdevice_id_to_dataset_id(blockdevice_id):
    """
    Computes a dataset_id from a blockdevice_id.

    :param unicode blockdevice_id: The blockdevice_id to get the dataset_id
        for.

    :returns UUID: The corresponding dataset_id.
    """
    return UUID(blockdevice_id[len(_PREFIX):])


def _dataset_id_to_blockdevice_id(dataset_id):
    """
    Computes a blockdevice_id from a dataset_id.

    :param UUID dataset_id: The dataset_id to get the blockdevice_id for.

    :returns unicode: The corresponding blockdevice_id.
    """
    return _PREFIX + unicode(dataset_id)


def _extract_attached_to(disk):
    """
    Given a GCE disk resource, determines the unicode name of the machine that
    it is attached to.

    :param dict disk: A GCE disk resource as returned from the API.

    :returns: The `unicode` name of the instance the disk is attached to or
        `None` if it is not attached to anything.
    """
    # TODO(mewert): determine how this works with a disk being attached to
    # multiple machines, update comment above.
    users = disk.get('users', [])
    if not users:
        return None
    return unicode(users[0].split('/')[-1])


@implementer(IBlockDeviceAPI)
class PDBlockDeviceAPI(object):
    """
    A PD implementation of ``IBlockDeviceAPI`` which creates block devices in a
    GCE project.

    Constraints imposed from GCE:
        - GCE does not have a grab-bag of metadata you can attach to disks.
            Instead, it has two fields in the disk resource you can set:
                - name (must be unique within a project,
                        limited to 63 characters,
                        must start with letter)
                - description (free form text field)
        - GCE does allow you to pick the blockdevice_id of your volumes (the
            unique identifier that will be used to identify your volume on
            subsequent API calls). In GCE terms this is the disk resource name.
        - GCE lets you filter on both of these string fields, but only for
            equals and not equals.
        - GCE lets you set a token in the SCSI info pages on your blockdevices
            every time you attach them to a VM so that symlinks are created at
            '/dev/disk/by-id/google-<token>'.
            - This technically is brittle to implementation, configuration, and
                version of udev and the SCSI driver within the guest OS.
        - GCE resource names and descriptions are immutable after creation.

    Design:
        - For a given dataset_id (06e07bcc-fea1-4810-8c6d-7487196998b6) prefix
            the dataset_id with "flocker-v1-" to get the unicode blockdevice-id
            (flocker-v1-06e07bcc-fea1-4810-8c6d-7487196998b6).
        - Set the name of the GCE disk resource to the blockdevice_id. This
            lets us perform operations on the disk only knowing its
            blockdevice_id.
        - Set the description of the disk resource to:
            "flocker-v1-cluster: <cluster-uuid>".
        - Whenever attaching a disk to an instance, set <token> to the
            blockdevice_id.

    Design implications:
        - The GCE disk names meet the length and format requirements, and are
            as unique as blockdevice_ids.
        - We can perform operations on a disk only knowing its blockdevice_id
            without making additional API calls or looking things up in a
            table.
        - dataset_id is a pure function of blockdevice_id and vice versa.
        - You can have multiple clusters within the same project.
        - Multiple clusters within the same project cannot have datasets with
            the same UUID. **
        - We could add filtering by cluster by filtering on description.
        - The path of the volume (or at least the path to a symlink to a path
            of the volume) is a pure function of blockdevice_id.
    """
    # TODO(mewert): Logging throughout.

    def __init__(self, project, zone):
        """
        Initialize the PDBlockDeviceAPI.

        :param unicode project: The project where all GCE operations will take
            place.
        :param unicode zone: The zone where all GCE operations will take place.
        """
        # TODO(mewert): Also enable credentials via service account private
        # keys.
        credentials = AppAssertionCredentials(
            "https://www.googleapis.com/auth/cloud-platform")
        self._compute = discovery.build(
            'compute', 'v1', credentials=credentials)
        self._project = project
        self._zone = zone

    def _do_blocking_operation(self, function, **kwargs):
        """
        Perform a GCE operation, blocking until the operation completes.

        This will call `function` with the passed in keyword arguments plus
        additional keyword arguments for project and zone which come from the
        private member variables with the same name. It is expected that
        `function` returns an object that has an `execute()` method that
        returns a GCE operation resource dict.

        This function will then poll the operation until it reaches state
        'DONE' or times out, and then returns the final operation resource
        dict.

        :param function: Callable that takes keyword arguments project and
            zone, and returns an executable that results in a GCE operation
            resource dict as described above.
        :param kwargs: Additional keyword arguments to pass to function.

        :returns dict: A dict representing the concluded GCE operation
            resource.
        """
        # TODO(mewert): Be more sophisticated about timeout and retry loop.
        # Look at EBS code, read up on how GCE behaves, potentially allow each
        # operation to specify its own timeout. Also pass a reactor in so you
        # can test the timeout error paths in unit tests. Also document what
        # happens on timeout.
        args = dict(project=self._project, zone=self._zone)
        args.update(kwargs)
        operation = function(**args).execute()
        operation_name = operation['name']

        def finished_operation_result():
            latest_operation = self._compute.zoneOperations().get(
                project=self._project,
                zone=self._zone,
                operation=operation_name).execute()
            # TODO Logging
            if latest_operation['status'] == 'DONE':
                return latest_operation
            return None

        return poll_until(finished_operation_result, [1]*35)

    def allocation_unit(self):
        """
        Can only allocate PDs in GiB units.

        Documentation claims `GB` but experimentally this was determined to
        actually be `GiB`.
        """
        return int(GiB(1).to_Byte().value)

    def list_volumes(self):
        # TODO(mewert) Walk the pages.
        # TODO(mewert) Only get volumes for _this_ cluster.
        result = self._compute.disks().list(project=self._project,
                                            zone=self._zone).execute()
        return list(
            BlockDeviceVolume(
                blockdevice_id=unicode(x['name']),
                size=int(GiB(int(x['sizeGb'])).to_Byte()),
                attached_to=_extract_attached_to(x),
                dataset_id=_blockdevice_id_to_dataset_id(x['name'])
            )
            for x in result['items']
            if x['name'].startswith(_PREFIX)
        )

    def compute_instance_id(self):
        """
        GCE does operations based on the `name` of resources, and also assigns
        the name

        """
        # TODO(mewert): Consider getting this from the metadata server instead.
        #               Technically people can change their hostname.
        return unicode(gethostname())

    def create_volume(self, dataset_id, size):
        # TODO(mewert): Set cluster_id in the metadata.
        blockdevice_id = _dataset_id_to_blockdevice_id(dataset_id)
        sizeGiB = int(Byte(size).to_GiB())
        config = dict(
            name=blockdevice_id,
            sizeGb=sizeGiB
        )
        # TODO(mewert): Verify timeout and error conditions.
        self._do_blocking_operation(
            self._compute.disks().insert, body=config)

        # TODO(mewert): Test creating a volume in cluster A in this project
        # with the same UUID as a volume in cluster B in the same project.
        return BlockDeviceVolume(
            blockdevice_id=blockdevice_id,
            size=int(GiB(sizeGiB).to_Byte()),
            attached_to=None,
            dataset_id=dataset_id,
        )

    def attach_volume(self, blockdevice_id, attach_to):
        config = dict(
            deviceName=blockdevice_id,
            autoDelete=False,
            boot=False,
            source=(
                "https://www.googleapis.com/compute/v1/projects/%s/zones/%s/"
                "disks/%s" % (self._project, self._zone, blockdevice_id)
            )
        )
        try:
            # TODO(mewert): Verify timeout and error conditions.
            # TODO(mewert): Test what happens when disk is attached RW to a
            #               different instance, raise the correct error.
            result = self._do_blocking_operation(
                self._compute.instances().attachDisk,
                instance=attach_to,
                body=config
            )
        except HttpError as e:
            if e.resp.status == 400:
                # TODO(mewert): verify with the rest API that this is the only
                # way to get a 400.
                raise UnknownVolume(blockdevice_id)
            else:
                raise e
        errors = result.get('error', {}).get('errors', [])
        for e in errors:
            if e.get('code') == u"RESOURCE_IN_USE_BY_ANOTHER_RESOURCE":
                raise AlreadyAttachedVolume(blockdevice_id)

        # TODO(mewert): This is sort of bad to just add an extra API call here
        #               for no reason ($10 says we don't even use the size).
        #               Put this behind a method call so we can add caching
        #               disk size later.
        disk = self._compute.disks().get(project=self._project,
                                         zone=self._zone,
                                         disk=blockdevice_id).execute()
        return BlockDeviceVolume(
            blockdevice_id=blockdevice_id,
            size=int(GiB(int(disk['sizeGb'])).to_Byte()),
            attached_to=attach_to,
            dataset_id=_blockdevice_id_to_dataset_id(blockdevice_id),
        )

    def _get_attached_to(self, blockdevice_id):
        """
        Determines the instance a blockdevice is attached to.

        :param unicode blockdevice_id: The blockdevice_id of the blockdevice to
            query.

        :returns unicode: The name of the instance.

        :raises UnknownVolume: If there is no volume with the given id in the
            cluster.
        :raises UnattachedVolume: If the volume is not attached to any
            instance.
        """
        try:
            # TODO(mewert) verify timeouts and error conditions.
            disk = self._compute.disks().get(project=self._project,
                                             zone=self._zone,
                                             disk=blockdevice_id).execute()
        except HttpError as e:
            if e.resp.status == 404:
                # TODO(mewert) Verify with the rest API this is the only way to
                # get a 404.
                raise UnknownVolume(blockdevice_id)
            else:
                raise e
        attached_to = _extract_attached_to(disk)
        if not attached_to:
            raise UnattachedVolume(blockdevice_id)
        return attached_to

    def detach_volume(self, blockdevice_id):
        attached_to = self._get_attached_to(blockdevice_id)
        # TODO(mewert): Test this race (something else detaches right at this
        # point). Might involve putting all GCE interactions behind a zope
        # interface and then using a proxy implementation to inject code.
        self._do_blocking_operation(
            self._compute.instances().detachDisk, instance=attached_to,
            deviceName=blockdevice_id)
        return None

    def get_device_path(self, blockdevice_id):
        # TODO(mewert): Verify that we need this extra API call.
        self._get_attached_to(blockdevice_id)

        # TODO(mewert): Verify we can get away returning a symlink here, or
        # just walk the symlink.
        return FilePath(u"/dev/disk/by-id/google-" + blockdevice_id)

    def destroy_volume(self, blockdevice_id):
        try:
            # TODO(mewert) verify timeouts and error conditions.
            self._do_blocking_operation(
                self._compute.disks().delete,
                disk=blockdevice_id
            )
        except HttpError as e:
            if e.resp.status == 404:
                raise UnknownVolume(blockdevice_id)
            else:
                raise e
        return None