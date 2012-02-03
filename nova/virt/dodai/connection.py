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
import os
import os.path
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
from nova.virt.dodai import ofc_utils
from nova.compute import vm_states

from eventlet import greenthread

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

        instance_ids = []
        bmms = db.bmm_get_all(None)
        for bmm in bmms:
            if bmm["status"] != "active":
                continue
            instance_ids.append(bmm["instance_id"])

        return instance_ids

    def list_instances_detail(self):
        """Return a list of InstanceInfo for all registered VMs"""
        LOG.debug("list_instances_detail")

        info_list = []
        bmms = db.bmm_get_all(None)
        for bmm in bmms:
            if bmm["status"] != "used":
                continue
            info_list.append(driver.InstanceInfo(bmm["instance_id"], "running"))

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

        instance_zone, cluster_name, vlan_id, create_cluster = self._parse_zone(instance["availability_zone"])

        # update instances table
        bmm, reuse = self._get_a_bare_metal_machine(context, instance)
        instance["display_name"] = bmm["name"]
        instance["availability_zone"] = instance_zone
        db.instance_update(context, 
                           instance["id"], 
                           {"display_name": bmm["name"],
                            "availability_zone": instance_zone})
 
        if instance_zone == "resource_pool":
            self._install_machine(context, instance, bmm, cluster_name, vlan_id)
        else: 
            self._update_ofc(bmm, cluster_name, vlan_id, create_cluster)
            if bmm["instance_id"]:
                db.instance_destroy(context, bmm["instance_id"])

            if reuse:
                db.bmm_update(context, bmm["id"], {"availability_zone": cluster_name, 
                                                   "status": "used", 
                                                   "instance_id": instance["id"]}) 
            else:
                self._install_machine(context, instance, bmm, cluster_name, vlan_id)

    def _parse_zone(self, zone):
        create_cluster = False
        vlan_id = None
        cluster_name = "resource_pool" 
        instance_zone = zone 
        parts = zone.split(",")
        if len(parts) >= 2:
            if parts[0] == "C":
                parts.pop(0)
                create_cluster = True

            cluster_name, vlan_id = parts
            vlan_id = int(vlan_id)
            instance_zone = ",".join(parts)

        return instance_zone, cluster_name, vlan_id, create_cluster

    def _install_machine(self, context, instance, bmm, cluster_name, vlan_id, update_instance=False):
        db.bmm_update(context, bmm["id"], {"status": "processing", "instance_id": instance["id"]})
        mac = self._get_pxe_mac(bmm)

        # fetch image
        utils.execute('mkdir', '-p', self._get_cobbler_path(instance))
        image_path = self._get_cobbler_path(instance, "disk")
        image_meta = images.fetch(context, 
                     instance["image_ref"], 
                     image_path, 
                     instance["user_id"], 
                     instance["project_id"])
        LOG.debug(image_meta) 
        image_type = "server"
        image_name = image_meta["name"] or image_meta["properties"]["image_location"]
        if image_name.find("dodai-deploy") == -1:
            image_type = "node"

        # begin to install os
        pxe_ip = bmm["pxe_ip"] or "None"
        pxe_mac = bmm["pxe_mac"] or "None"
        storage_ip = bmm["storage_ip"] or "None"
        storage_mac = bmm["storage_mac"] or "None"
 
        self._cp_template("create.sh", 
                          self._get_cobbler_path(instance, "create.sh"),
                          {"INSTANCE_ID": instance["id"], 
                           "COBBLER": FLAGS.cobbler, 
                           "HOST_NAME": bmm["name"], 
                           "STORAGE_IP": storage_ip,
                           "STORAGE_MAC": storage_mac,
                           "PXE_IP": pxe_ip, 
                           "PXE_MAC": pxe_mac,
                           "IMAGE_TYPE": image_type,
                           "MONITOR_PORT": FLAGS.dodai_monitor_port,
                           "ROOT_SIZE": FLAGS.dodai_partition_root_gb,
                           "SWAP_SIZE": FLAGS.dodai_partition_swap_gb,
                           "EPHEMERAL_SIZE": FLAGS.dodai_partition_ephemeral_gb,
                           "KDUMP_SIZE": FLAGS.dodai_partition_kdump_gb})
        self._cp_template("pxeboot_create", 
                          self._get_pxe_boot_file(mac), 
                          {"INSTANCE_ID": instance["id"], "COBBLER": FLAGS.cobbler})
 
        LOG.debug("reboot or power on.")
        self._reboot_or_power_on(bmm["ipmi_ip"])
 
        # wait until starting to install os
        while self._get_state(instance) != "install":
            greenthread.sleep(20)
            LOG.debug("wait until begin to install instance %s." % instance["id"])
        self._cp_template("pxeboot_start", self._get_pxe_boot_file(mac), {})
 
        # wait until installation of os finished
        while self._get_state(instance) != "installed":
            greenthread.sleep(20)
            LOG.debug("wait until instance %s installation finished." % instance["id"])
 
        if cluster_name == "resource_pool":
            status = "active"
        else:
            status = "used"

        db.bmm_update(context, bmm["id"], 
                               {"availability_zone": cluster_name,
                                "vlan_id": vlan_id,
                                "service_ip": None,
                                "status": status})

        if update_instance:
            db.instance_update(context, instance["id"], {"vm_state": vm_states.ACTIVE})
    
    def _update_ofc(self, bmm, cluster_name, vlan_id, create_cluster):
        try:
            ofc_utils.update_for_run_instance(FLAGS.ofc_service_url, 
                                              cluster_name, 
                                              bmm["server_port1"],
                                              bmm["server_port2"],
                                              bmm["dpid1"],
                                              bmm["dpid2"],
                                              vlan_id,
                                              create_cluster)
        except:
            pass

    def _get_state(self, instance):
        path = self._get_cobbler_path(instance, "state")
        if not os.path.exists(path):
            return ""

        if not os.path.isfile(path):
            return ""

        f = open(path)
        state = f.read().strip()
        f.close()
       
        LOG.debug("State of instance %d: %s" % (instance["id"], state))
        return state 

    def _get_pxe_mac(self, bmm):
        return "01-%s" % bmm["pxe_mac"].replace(":", "-").lower()

    def _get_a_bare_metal_machine(self, context, instance):
        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)

        bmms = db.bmm_get_all_by_instance_type_and_zone(context, inst_type["name"], "resource_pool")
        for bmm in bmms:
            LOG.debug(bmm["status"])
            LOG.debug(instance["image_ref"])
            if bmm["status"] != "active":
                continue 

            instance_ref = db.instance_get(context, bmm["instance_id"])
            LOG.debug(instance_ref["image_ref"])
            if instance_ref["image_ref"] == instance["image_ref"]:
                return bmm, True

        for bmm in db.bmm_get_all_by_instance_type(context, inst_type["name"]):
            if bmm["status"] != "used" and bmm["status"] != "processing":
                return bmm, False

        raise exception.BareMetalMachineUnavailable()  

    def _get_cobbler_path(self, instance, file_name = ""):
        return os.path.join(FLAGS.cobbler_path,
                     "images",
                     str(instance["id"]),
                     file_name)

    def _get_pxe_boot_file(self, mac):
        return os.path.join(FLAGS.pxe_boot_path, mac)

    def _get_disk_size_mb(self, instance):
        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)
        if inst_type["local_gb"] == 0:
          return 10 * 1024

        return inst_type["local_gb"] * 1024

    def _reboot_or_power_on(self, ip):
        power_manager = PowerManager(ip)
        status = power_manager.status()
        LOG.debug("The power is " + status)
        if status == "off":
            power_manager.on()
        else:
            power_manager.reboot()

    def _cp_template(self, template_name, dest_path, params):
        f = open(utils.abspath("virt/dodai/" + template_name + ".template"), "r")
        content = f.read()
        f.close()

        path = os.path.dirname(dest_path)
        if not os.path.exists(path):
           os.makedirs(path) 

        for key, value in params.iteritems():
            content = content.replace(key, str(value))

        f = open(dest_path, "w")
        f.write(content) 
        f.close 


    def destroy(self, context, instance, network_info, cleanup=True):
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

        bmm = db.bmm_get_by_instance_id(context, instance["id"])
        mac = self._get_pxe_mac(bmm)

        # begin to delete os
        self._cp_template("delete.sh",
                          self._get_cobbler_path(instance, "delete.sh"), 
                          {"INSTANCE_ID": instance["id"],
                           "COBBLER": FLAGS.cobbler,
                           "MONITOR_PORT": FLAGS.dodai_monitor_port})
        self._cp_template("pxeboot_delete",
                          self._get_pxe_boot_file(mac),
                          {"INSTANCE_ID": instance["id"], "COBBLER": FLAGS.cobbler})
        self._reboot_or_power_on(bmm["ipmi_ip"])

        # wait until starting to delete os
        while self._get_state(instance) != "deleted":
            greenthread.sleep(20)
            LOG.debug("wait until data of instance %s was deleted." % instance["id"])

        utils.execute("rm", "-rf", self._get_cobbler_path(instance));
        db.bmm_update(context, bmm["id"], {"status": "inactive"})

        bmms = db.bmm_get_by_availability_zone(context, bmm["availability_zone"])
        delete_cluster = len(bmms) == 1

        # update ofc
        try:
            ofc_utils.update_for_terminate_instance(FLAGS.ofc_service_url,
                                                    bmm["availability_zone"],
                                                    bmm["server_port1"],
                                                    bmm["server_port2"],
                                                    bmm["dpid1"],
                                                    bmm["dpid2"],
                                                    bmm["vlan_id"],
                                                    delete_cluster)
        except:
            pass

        # update db
        db.bmm_update(context, bmm["id"], {"instance_id": None, 
                                        "availability_zone": "resource_pool",
                                        "vlan_id": None,
                                        "status": "inactive"})

        return db.bmm_get(context, bmm["id"])

    def add_to_resource_pool(self, context, instance, bmm):
        # begin to install default os
        self._install_machine(context, instance, bmm, "resource_pool", None, True)

    def stop(self, context, instance):
        LOG.debug("stop")
        bmm = db.bmm_get_by_instance_id(context, instance["id"])
        PowerManager(bmm["ipmi_ip"]).off() 

    def start(self, context, instance):
        LOG.debug("start")
        bmm = db.bmm_get_by_instance_id(context, instance["id"])
        PowerManager(bmm["ipmi_ip"]).on() 

    def reboot(self, instance, network_info):
        """Reboot the specified instance.

        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        """
        LOG.debug("reboot")

        bmm = db.bmm_get_by_instance_id(None, instance["id"])
        PowerManager(bmm["ipmi_ip"]).reboot()

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

    def __init__(self, ip):
        self.ip = ip

    def on(self):
        return self._execute("on")

    def off(self):
        return self._execute("off")

    def reboot(self):
        return self._execute("reset")

    def status(self):
        parts = self._execute("status").split(" ")
        return parts[3].strip()

    def _execute(self, subcommand):
        out, err = utils.execute("/usr/bin/ipmitool", "-I", "lan", "-H", self.ip, "-U", FLAGS.ipmi_username, "-P", FLAGS.ipmi_password, "chassis", "power", subcommand)
        return out
