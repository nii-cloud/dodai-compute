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
from nova.compute import instance_types
from nova.virt import driver
from nova import db
from nova.virt import images
from nova import flags

from eventlet import greenthread

LOG = logging.getLogger('nova.virt.dodai')
FLAGS = flags.FLAGS

flags.DEFINE_string('cobbler', None, 'IP address of cobbler')
flags.DEFINE_string('cobbler_path', '/var/www/cobbler', 'Path of cobbler')
flags.DEFINE_string('pxe_boot_path', '/var/lib/tftpboot/pxelinux.cfg', 'Path of pxeboot folder')
flags.DEFINE_string('ofc_service_url', None, 'URL of open flow controller service.')
flags.DEFINE_string('ofc_dpid', None, 'Dpid of open flow controller.')
flags.DEFINE_integer('bmm_port', 3333, '')
flags.DEFINE_string('bmm_status_path', "status", '')
flags.DEFINE_string('bmm_action_path', "action", '')
flags.DEFINE_integer('dodai_default_image', 1, '')

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

        # find a bare metal machine
        bmm = self._find_a_bare_metal_machine(instance)
        mac = bmm["pxe_mac"]

        # fetch image
        utils.execute('mkdir', '-p', self._get_cobbler_path(instance))
        image_path = self._get_cobbler_path(instance, "disk")
        images.fetch(context, 
                     instance["image_ref"], 
                     image_path, 
                     instance["user_id"], 
                     instance["project_id"])

        # begin to install os
        self._cp_template("create.sh", 
                          self._get_cobbler_path(instance, "create.sh"),
                          {"INSTANCE_ID": instance["name"], 
                           "COBBLER": FLAGS.cobbler, 
                           "DISK_SIZE": self._get_disk_size_mb(instance)})
        self._cp_template("pxeboot_create", 
                          self._get_pxe_boot_file(), 
                          {"INSTANCE_ID": instance["name"], "COBBLER": FLAGS.cobbler})

        LOG.debug("reboot or power on.")
        self._reboot_or_power_on(bmm["ipmi_ip"])

        # wait until starting to install os
        while BmmService.is_listening(bmm["ipmi_ip"]):
            greenthread.sleep(10)
        self._cp_template("pxeboot_start", self._get_pxe_boot_file(), {})

        # wait until installation of os finished
        while not BmmService.is_listening(bmm["ipmi_ip"]):
            greenthread.sleep(10)

        # update db
        parts = instance["availability_zone"].split(":")
        create_cluster = False
        if len(parts) == 3 and parts[0] == "C":
            parts.pop(0)
            create_cluster = True

        cluster_name, vlan_id = parts
        vlan_id = int(vlan_id)

        db.bmm_update(context, bmm["id"], {"instance_id": instance["id"],
                                           "availability_zone": cluster_name,
                                           "vlan_id": vlan_id,
                                           "status": "used"})

        # update ofc
        ofc_utils.update_for_run_instance(FLAGS.ofc_service_url, 
                                          cluster_name, 
                                          FLAGS.ofc_dpid,
                                          bmm["ofc_port"] 
                                          vlan_id,
                                          create_cluster)

    def _find_a_bare_metal_machine(self, instance):
        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)
        return db.bmm_get_by_instance_type(inst_type)

    def _get_cobbler_path(self, instance, file_name = ""):
        return os.path.join(FLAGS.cobbler_path,
                     "images",
                     instance["name"],
                     file_name)

    def _get_pxe_boot_file(self):
        return os.path.join(FLAGS.pxe_boot_path, mac)

    def _get_disk_size_mb(self, instance):
        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)
        if inst_type["local_gb"] == 0:
          return 10 * 1024

        return inst_type["local_gb"] * 1024

    def _reboot_or_power_on(self, ip):
        # TODO: to implement with ipmi
        greenthread.sleep(120)

    def _cp_template(self, template_name, dest_path, params):
        f = open(utils.abspath("virt/dodai/" + template_name + ".template"), "r")
        content = f.read()
        f.close()

        for key, value in params.iteritems():
            content = content.replace(key, str(value))

        f = open(dest_path, "w")
        f.write(content) 
        f.close 


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

        bmm = db.bmm_get_by_instance_id(None, instance["id"])

        # begin to delete os
        self._cp_template("delete.sh",
                          self._get_cobbler_path(instance, "delete.sh"), 
                          {})
        self._cp_template("pxeboot_delete",
                          self._get_pxe_boot_file(),
                          {"INSTANCE_ID": instance["name"], "COBBLER": FLAGS.cobbler})
        self._reboot_or_power_on()

        # wait until starting to delete os
        while BmmService.is_listening(bmm["ipmi_ip"]):
            greenthread.sleep(10)

        utils.execute("rm", "-rf", self._get_cobbler_path(instance));
        db.bmm_update(None, bmm["id"], {"status": "inactive"})

        # begin to install default os
        # fetch image
        utils.execute('mkdir', '-p', self._get_cobbler_path(instance))
        image_path = self._get_cobbler_path(instance, "disk")
        images.fetch(None,
                     FLAGS.dodai_default_image,
                     image_path,
                     instance["user_id"],
                     instance["project_id"])

        self._cp_template("create.sh",
                          self._get_cobbler_path(instance, "create.sh"),
                          {"INSTANCE_ID": instance["name"],
                           "COBBLER": FLAGS.cobbler,
                           "DISK_SIZE": 10240})
        self._cp_template("pxeboot_create",
                          self._get_pxe_boot_file(),
                          {"INSTANCE_ID": instance["name"], "COBBLER": FLAGS.cobbler})

        # wait until os installation finished.
        while not BmmService.is_listening(bmm["ipmi_ip"]):
            greenthread.sleep(10)

        bmms = db.bmm_get_by_availability_zone(None, bmm["availability_zone"])
        delete_cluster = len(bmms) == 1

        # update db
        db.bmm_update(None, bmm["id"], {"instance_id": None, 
                                        "availability_zone": None,
                                        "vlan_id": None,
                                        "status": "active"})

        # update ofc
        ofc_utils.update_for_terminate_instance(FLAGS.ofc_service_url,
                                                bmm["availability_zone"],
                                                FLAGS.ofc_dpid,
                                                bmm["ofc_port"],
                                                bmm["vlan_id"],
                                                delete_cluster)

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

class PowerManager(object):

    def __init__(self, instance_id, ip):
        self.cobbler = capi.BootAPI()

        system = self.cobbler.new_system()
        system.set_name(instance_id)
        system.set_power_type("ipmi")
        system.set_power_user(FLAGS.ipmi_user)
        system.set_power_pass(FLAGS.ipmi_password)
        system.set_power_address(ip)
        self.cobbler.add_system(system)
        self.system = system

    def on(self):
        return self.cobbler.power_on(self.system)

    def off(self):
        return self.cobbler.power_off(self.system)

    def reboot(self):
        return self.cobbler.reboot(self.system)

    def status(self):
        return self.cobbler.power_status(self.system)

class BmmService(object):

    @staticmethod
    def is_listening(ip):
        try:
            conn = httplib.HTTPConnection(ip, FLAGS.bmm_port)
            conn.close()
        except:
            return False

        return True

