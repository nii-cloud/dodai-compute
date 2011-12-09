# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
A dodai hypervisor.

"""
import cobbler.api as capi
import os
import tempfile

from nova import exception
from nova import log as logging
from nova import utils
from nova.compute import power_state
from nova.virt import driver
from nova import db
from nova.virt import images
from nova import flags

LOG = logging.getLogger('nova.virt.dodai')
FLAGS = flags.FLAGS

def get_connection(_):
    # The read_only parameter is ignored.
    return DodaiConnection.instance()


class DodaiInstance(object):

    def __init__(self, name, state):
        self.name = name
        self.state = state

class DodaiConnection(driver.ComputeDriver):
    """Dodai hypervisor driver"""

    def __init__(self):
        self.instances = {}

    @classmethod
    def instance(cls):
        if not hasattr(cls, '_instance'):
            cls._instance = cls()
        return cls._instance

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host."""
        LOG.debug("init_host")


    def get_info(self, instance_name):
        """Get the current status of an instance, by name (not ID!)

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds
        """
        LOG.debug("get_info")
        if instance_name not in self.instances:
            raise exception.InstanceNotFound(instance_id=instance_name)

        i = self.instances[instance_name]
        return {'state': i.state,
                'max_mem': 0,
                'mem': 0,
                'num_cpu': 2,
                'cpu_time': 0}

    def list_instances(self):
        """
        Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        LOG.debug("list_instances")
        return self.instances.keys()

    def _map_to_instance_info(self, instance):
        instance = utils.check_isinstance(instance, DodaiInstance)
        info = driver.InstanceInfo(instance.name, instance.state)
        return info

    def list_instances_detail(self):
        """Return a list of InstanceInfo for all registered VMs"""
        LOG.debug("list_instances_detail")
        info_list = []
        for instance in self.instances.values():
            info_list.append(self._map_to_instance_info(instance))
        return info_list        

    def spawn(self, context, instance,
              network_info=None, block_device_info=None):
        """
        Create a new instance/VM/domain on the virtualization platform.

        Once this successfully completes, the instance should be
        running (power_state.RUNNING).

        If this fails, any partial instance should be completely
        cleaned up, and the virtualization platform should be in the state
        that it was before this call began.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
                         This function should use the data there to guide
                         the creation of the new instance.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info:
        """
        LOG.debug("spawn")

        # fetch image
        def basepath(fname=''):
            return os.path.join(FLAGS.instances_path,
                                instance['name'],
                                fname)         
        utils.execute('mkdir', '-p', basepath())
        image_path = basepath("disk")
        images.fetch(context, 
                     instance["image_ref"], 
                     image_path, 
                     instance["user_id"], 
                     instance["project_id"])
        LOG.debug(image_path)        

        # add image to cobbler
        self._import_image(image_path)

        name = instance.name
        state = power_state.RUNNING
        dodai_instance = DodaiInstance(name, state)
        self.instances[name] = dodai_instance
        db.bmm_create(context, {"name": name})

    def _import_image(self, file_path):
        device = self._link_device(file_path)

        tmpdir = tempfile.mkdtemp()
        try:
            # mount loopback to dir
            out, err = utils.execute('mount', device, tmpdir,
                                     run_as_root=True)
            if err:
                raise exception.Error(_('Failed to mount filesystem: %s')
                                      % err)

            cobbler = capi.BootAPI()
            cobbler.import_tree(tmpdir, 
                                "ubuntu-mini", 
                                breed="ubuntu", 
                                logger=logging.getLogger('cobbler'))

        finally:
            utils.execute('umount', device, run_as_root=True)             
            utils.execute('rmdir', tmpdir)
        

    def _link_device(self, file_path):
        out, err = utils.execute('losetup', '--find', '--show', file_path,
                                 run_as_root=True)
        if err:
            raise exception.Error(_('Could not attach image to loopback: %s')
                                  % err)
        return out.strip()

    def destroy(self, instance, network_info, cleanup=True):
        """Destroy (shutdown and delete) the specified instance.

        If the instance is not found (for example if networking failed), this
        function should still succeed.  It's probably a good idea to log a
        warning in that case.

        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param cleanup:

        """
        LOG.debug("destroy")
        key = instance['name']
        if key in self.instances:
            del self.instances[key]
            bmm = db.bmm_get_by_name(None, key)
            LOG.debug(bmm)
            db.bmm_destroy(None, bmm["id"]) 
        else:
            LOG.warning("Key '%s' not in instances '%s'" %
                        (key, self.instances))

    def reboot(self, instance, network_info):
        """Reboot the specified instance.

        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        """
        LOG.debug("reboot")

    def update_available_resource(self, ctxt, host):
        """Updates compute manager resource info on ComputeNode table.

        This method is called when nova-compute launches, and
        whenever admin executes "nova-manage service update_resource".

        :param ctxt: security context
        :param host: hostname that compute manager is currently running

        """
        LOG.debug("update_available_resource")
        return

    def reset_network(self, instance):
        """reset networking for specified instance"""
        LOG.debug("reset_network")
        return
