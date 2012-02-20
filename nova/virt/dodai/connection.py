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
from nova.db.sqlalchemy.session import get_session_dodai

from eventlet import greenthread

LOG = logging.getLogger('nova.virt.dodai')
FLAGS = flags.FLAGS

def get_connection(_):
    # The read_only parameter is ignored.
    return DodaiConnection.instance()


class DodaiConnection(driver.ComputeDriver):
    """Dodai hypervisor driver"""

    def __init__(self):
        self.host_status = {
          'host_name-description': 'Dodai Compute',
          'host_hostname': 'dodai-compute',
          'host_memory_total': 8000000000,
          'host_memory_overhead': 10000000,
          'host_memory_free': 7900000000,
          'host_memory_free_computed': 7900000000,
          'host_other_config': {},
          'host_ip_address': '192.168.1.109',
          'host_cpu_info': {},
          'disk_available': 500000000000,
          'disk_total': 600000000000,
          'disk_used': 100000000000,
          'host_uuid': 'cedb9b39-9388-41df-8891-c5c9a0c0fe5f',
          'host_name_label': 'dodai-compute'}

    @classmethod
    def instance(cls):
        if not hasattr(cls, '_instance'):
            cls._instance = cls()
        return cls._instance

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host."""
        LOG.debug("init_host")

    def get_host_stats(self, refresh=False):
        """Return Host Status of ram, disk, network."""
        return self.host_status

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

        instance_id = self._instance_name_to_id(instance_name)
        bmm = db.bmm_get_by_instance_id(None, instance_id)
        status = PowerManager(bmm["ipmi_ip"]).status()
        if status == "on":
            inst_power_state = power_state.RUNNING
        else:
            inst_power_state = power_state.SHUTOFF

        return {'state': inst_power_state,
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
            if not bmm["instance_id"]:
                continue
            instance_ids.append(self._instance_id_to_name(bmm["instance_id"]))

        return instance_ids

    def list_instances_detail(self, context):
        """Return a list of InstanceInfo for all registered VMs"""
        LOG.debug("list_instances_detail")

        info_list = []
        bmms = db.bmm_get_all_by_instance_id_not_null(context)
        for bmm in bmms:
            instance = db.instance_get(context, bmm["instance_id"])
            status = PowerManager(bmm["ipmi_ip"]).status()
            if status == "off":
                inst_power_state = power_state.SHUTOFF

                if instance["vm_state"] == vm_states.ACTIVE:
                    db.instance_update(context, instance["id"], {"vm_state": vm_states.STOPPED})
            else:
                inst_power_state = power_state.RUNNING

                if instance["vm_state"] == vm_states.STOPPED:
                    db.instance_update(context, instance["id"], {"vm_state": vm_states.ACTIVE})

            info_list.append(driver.InstanceInfo(self._instance_id_to_name(bmm["instance_id"]), 
                                                 inst_power_state))

        return info_list

    def _instance_id_to_name(self, instance_id):
        return FLAGS.instance_name_template % instance_id

    def _instance_name_to_id(self, instance_name):
        return int(instance_name.split("-")[1], 16)  

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
        bmm, reuse = self._select_machine(context, instance)
        instance["display_name"] = bmm["name"]
        instance["availability_zone"] = instance_zone
        db.instance_update(context, 
                           instance["id"], 
                           {"display_name": bmm["name"],
                            "availability_zone": instance_zone})
        if vlan_id:
            db.bmm_update(context, bmm["id"], {"availability_zone": cluster_name, 
                                               "vlan_id": vlan_id,
                                               "service_ip": None})
 
        if instance_zone == "resource_pool":
            self._install_machine(context, instance, bmm, cluster_name, vlan_id)
        else: 
            self._update_ofc(bmm, cluster_name)
            if bmm["instance_id"]:
                db.instance_destroy(context, bmm["instance_id"])

            if reuse:
                db.bmm_update(context, bmm["id"], {"status": "used", 
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
        db.bmm_update(context, bmm["id"], {"instance_id": instance["id"]})
        mac = self._get_pxe_mac(bmm)

        # fetch image
        image_base_path = self._get_cobbler_image_path()
        if not os.path.exists(image_base_path):
            utils.execute('mkdir', '-p', image_base_path)

        image_path = self._get_cobbler_image_path(instance)
        if not os.path.exists(image_path):
            image_meta = images.fetch(context, 
                                      instance["image_ref"], 
                                      image_path, 
                                      instance["user_id"], 
                                      instance["project_id"])
        else:
            image_meta = images.show(context, instance["image_ref"])

        image_type = "server"
        image_name = image_meta["name"] or image_meta["properties"]["image_location"]
        if image_name.find("dodai-deploy") == -1:
            image_type = "node"

        # begin to install os
        pxe_ip = bmm["pxe_ip"] or "None"
        pxe_mac = bmm["pxe_mac"] or "None"
        storage_ip = bmm["storage_ip"] or "None"
        storage_mac = bmm["storage_mac"] or "None"

        instance_path = self._get_cobbler_instance_path(instance) 
        if not os.path.exists(instance_path):
            utils.execute('mkdir', '-p', instance_path)

        if instance["image_ref"] == 10 or instance["image_ref"] == "10":
            self._cp_template("create.sh.ubuntu", 
                          self._get_cobbler_instance_path(instance, "create.sh"),
                          {"INSTANCE_ID": instance["id"], 
                           "IMAGE_ID": instance["image_ref"], 
                           "COBBLER": FLAGS.cobbler, 
                           "HOST_NAME": bmm["name"], 
                           "STORAGE_IP": storage_ip,
                           "STORAGE_MAC": storage_mac,
                           "PXE_IP": pxe_ip, 
                           "PXE_MAC": pxe_mac,
                           "SERVICE_MAC1": bmm["service_mac1"],
                           "SERVICE_MAC2": bmm["service_mac2"],
                           "IMAGE_TYPE": image_type,
                           "MONITOR_PORT": FLAGS.dodai_monitor_port,
                           "ROOT_SIZE": FLAGS.dodai_partition_root_gb,
                           "SWAP_SIZE": FLAGS.dodai_partition_swap_gb,
                           "EPHEMERAL_SIZE": FLAGS.dodai_partition_ephemeral_gb,
                           "KDUMP_SIZE": FLAGS.dodai_partition_kdump_gb})
        else:
            self._cp_template("create.sh", 
                          self._get_cobbler_instance_path(instance, "create.sh"),
                          {"INSTANCE_ID": instance["id"], 
                           "IMAGE_ID": instance["image_ref"], 
                           "COBBLER": FLAGS.cobbler, 
                           "HOST_NAME": bmm["name"], 
                           "STORAGE_IP": storage_ip,
                           "STORAGE_MAC": storage_mac,
                           "PXE_IP": pxe_ip, 
                           "PXE_MAC": pxe_mac,
                           "SERVICE_MAC1": bmm["service_mac1"],
                           "SERVICE_MAC2": bmm["service_mac2"],
                           "IMAGE_TYPE": image_type,
                           "MONITOR_PORT": FLAGS.dodai_monitor_port,
                           "ROOT_SIZE": FLAGS.dodai_partition_root_gb,
                           "SWAP_SIZE": FLAGS.dodai_partition_swap_gb,
                           "EPHEMERAL_SIZE": FLAGS.dodai_partition_ephemeral_gb,
                           "KDUMP_SIZE": FLAGS.dodai_partition_kdump_gb})

        self._cp_template("pxeboot_create",
                          self._get_pxe_boot_file(mac),
                          {"INSTANCE_ID": instance["id"], "COBBLER": FLAGS.cobbler})

        LOG.debug("Reboot or power on.")
        self._reboot_or_power_on(bmm["ipmi_ip"])

        # wait until starting to install os
        while self._get_state(context, instance) != "install":
            greenthread.sleep(20)
            LOG.debug("Wait until begin to install instance %s." % instance["id"])
        self._cp_template("pxeboot_start", self._get_pxe_boot_file(mac), {})

        # wait until starting to reboot 
        while self._get_state(context, instance) != "install_reboot":
            greenthread.sleep(20)
            LOG.debug("Wait until begin to reboot instance %s after os has been installed." % instance["id"])
        power_manager = PowerManager(bmm["ipmi_ip"])
        power_manager.soft_off()
        while power_manager.status() == "on":
            greenthread.sleep(20)
            LOG.debug("Wait unit the instance %s shuts down." % instance["id"])
        power_manager.on()

        # wait until installation of os finished
        while self._get_state(context, instance) != "installed":
            greenthread.sleep(20)
            LOG.debug("Wait until instance %s installation finished." % instance["id"])
 
        if cluster_name == "resource_pool":
            status = "active"
        else:
            status = "used"

        db.bmm_update(context, bmm["id"], {"status": status})

        if update_instance:
            db.instance_update(context, instance["id"], {"vm_state": vm_states.ACTIVE})
    
    def _update_ofc(self, bmm, cluster_name):
        try:
            ofc_utils.update_for_run_instance(FLAGS.ofc_service_url, 
                                              cluster_name, 
                                              bmm["server_port1"],
                                              bmm["server_port2"],
                                              bmm["dpid1"],
                                              bmm["dpid2"])
        except Exception as ex:
            LOG.exception(_("OFC exception %s"), unicode(ex))

    def _get_state(self, context, instance):
        # check if instance exists
        instance_ref = db.instance_get(context, instance["id"])
        if instance_ref["deleted"]:
            raise exception.InstanceNotFound(instance_id=instance["id"]) 

        path = self._get_cobbler_instance_path(instance, "state")
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

    def _select_machine(self, context, instance):
        inst_type = instance_types.get_instance_type(instance['instance_type_id'])

        bmm_found = None
        reuse = False

        # create a non autocommit session
        session = get_session_dodai(False)
        session.begin()
        try:
            bmms = db.bmm_get_all_by_instance_type(context, inst_type["name"], session)
            for bmm in bmms:
                if bmm["availability_zone"] != "resource_pool":
                    continue

                if bmm["status"] != "active":
                    continue 
    
                instance_ref = db.instance_get(context, bmm["instance_id"])
                if instance_ref["image_ref"] != instance["image_ref"]:
                    continue

                bmm_found = bmm
                reuse = True
                break
   
            if not bmm_found:
                for bmm in bmms:
                    if bmm["status"] == "used" or bmm["status"] == "processing":
                        continue

                    bmm_found = bmm
                    reuse = False
                    break

            if bmm_found:
                db.bmm_update(context, bmm_found["id"], {"status": "processing"}, session)
        except Exception as ex:
            LOG.exception(ex)
            session.rollback()
            raise exception.BareMetalMachineUnavailable() 

        session.commit()

        if bmm_found:
            return bmm_found, reuse

        raise exception.BareMetalMachineUnavailable()

    def _get_cobbler_instance_path(self, instance, file_name = ""):
        return os.path.join(FLAGS.cobbler_path,
                     "instances",
                     str(instance["id"]),
                     file_name)

    def _get_cobbler_image_path(self, instance = None):
        if instance:
            return os.path.join(FLAGS.cobbler_path,
                                "images",
                                str(instance["image_ref"]))
        else:
            return os.path.join(FLAGS.cobbler_path,
                                "images")

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
        db.bmm_update(context, bmm["id"], {"status": "processing"})
        mac = self._get_pxe_mac(bmm)

        # update ofc
        self._update_ofc_for_destroy(context, bmm)
        db.bmm_update(context, bmm["id"], {"vlan_id": None,
                                           "availability_zone": "resource_pool"})

        # begin to delete os
        self._cp_template("delete.sh",
                          self._get_cobbler_instance_path(instance, "delete.sh"), 
                          {"INSTANCE_ID": instance["id"],
                           "COBBLER": FLAGS.cobbler,
                           "MONITOR_PORT": FLAGS.dodai_monitor_port})
        self._cp_template("pxeboot_delete",
                          self._get_pxe_boot_file(mac),
                          {"INSTANCE_ID": instance["id"], "COBBLER": FLAGS.cobbler})
        self._reboot_or_power_on(bmm["ipmi_ip"])

        # wait until starting to delete os
        while self._get_state(context, instance) != "deleted":
            greenthread.sleep(20)
            LOG.debug("Wait until data of instance %s was deleted." % instance["id"])

        utils.execute("rm", "-rf", self._get_cobbler_instance_path(instance));

        # update db
        db.bmm_update(context, bmm["id"], {"instance_id": None, 
                                           "service_ip": None})

        return db.bmm_get(context, bmm["id"])

    def _update_ofc_for_destroy(self, context, bmm):
        # update ofc
        try:
            LOG.debug("vlan_id: " + str(bmm["vlan_id"]))
            ofc_utils.update_for_terminate_instance(FLAGS.ofc_service_url,
                                                    bmm["availability_zone"],
                                                    bmm["server_port1"],
                                                    bmm["server_port2"],
                                                    bmm["dpid1"],
                                                    bmm["dpid2"],
                                                    bmm["vlan_id"])
        except Exception as ex:
            LOG.exception(_("OFC exception %s"), unicode(ex))

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

    def soft_off(self):
        return self._execute("soft")

    def reboot(self):
        return self._execute("reset")

    def status(self):
        parts = self._execute("status").split(" ")
        return parts[3].strip()

    def _execute(self, subcommand):
        out, err = utils.execute("/usr/bin/ipmitool", "-I", "lan", "-H", self.ip, "-U", FLAGS.ipmi_username, "-P", FLAGS.ipmi_password, "chassis", "power", subcommand)
        return out
