from suds.client import Client
from nova import exception
from nova import db

import logging

logging.getLogger('suds.client').setLevel(logging.INFO)

def update_for_run_instance(service_url, region_name, server_port1, server_port2, dpid1, dpid2, vlan_id, create_region):
    # check region name
    client = Client(service_url + "?wsdl")

    if region_name not in [x.regionName for x in client.service.showRegion()]:
       if not create_region:
           raise exception.OFCRegionNotFound(region_name)

       client.service.createRegion(region_name)

       switches = db.switch_get_all(None)
       for switch in switches:
           client.service.setOuterPortAssociationSetting(switch["dpid"], switch["outer_port"], vlan_id, 65535, region_name)
    else:
        if create_region:
            raise exception.OFCRegionExisted(region_name)

    client.service.setServerPort(dpid1, server_port1, region_name)
    client.service.setServerPort(dpid2, server_port2, region_name)
    client.service.save()

def update_for_terminate_instance(service_url, region_name, server_port1, server_port2, dpid1, dpid2, vlan_id, delete_region):
    client = Client(service_url + "?wsdl")
    client.service.clearServerPort(dpid1, server_port1)
    client.service.clearServerPort(dpid2, server_port2)
    if not delete_region:
        client.service.save()
        return

    switches = db.switch_get_all(None)
    for switch in switches:
        client.service.clearOuterPortAssociationSetting(switch["dpid"], switch["outer_port"], vlan_id)
    client.service.destroyRegion(region_name)
    client.service.save()
