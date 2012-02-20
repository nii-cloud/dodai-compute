from suds.client import Client
from nova import exception
from nova import db

import logging

logging.getLogger('suds').setLevel(logging.INFO)

def update_for_run_instance(service_url, region_name, server_port1, server_port2, dpid1, dpid2):
    # check region name
    client = Client(service_url + "?wsdl")

    client.service.setServerPort(dpid1, server_port1, region_name)
    client.service.setServerPort(dpid2, server_port2, region_name)

def update_for_terminate_instance(service_url, region_name, server_port1, server_port2, dpid1, dpid2, vlan_id):
    client = Client(service_url + "?wsdl")
    client.service.clearServerPort(dpid1, server_port1)
    client.service.clearServerPort(dpid2, server_port2)

    has_port = False
    ports = client.service.showPorts(dpid1) + client.service.showPorts(dpid2)
    for port in ports:
        if port.type != "ServerPort":
            continue

        if port.regionName == region_name:
            has_port = True
            break

    if has_port:
        return

    remove_region(service_url, region_name, vlan_id)

def create_region(service_url, region_name, vlan_id):
    client = Client(service_url + "?wsdl")
    try:
        client.service.createRegion(region_name)
    except:
        raise exception.OFCRegionCreationFailed(region_name=region_name) 

    try:
        switches = db.switch_get_all(None)
        for switch in switches:
            client.service.setOuterPortAssociationSetting(switch["dpid"], switch["outer_port"], vlan_id, 65535, region_name)
    except:
        client.service.destroyRegion(region_name)
        raise exception.OFCRegionSettingOuterPortAssocFailed(region_name=region_name, vlan_id=vlan_id)

def remove_region(service_url, region_name, vlan_id):
    client = Client(service_url + "?wsdl")

    try:
        switches = db.switch_get_all(None)
        for switch in switches:
            client.service.clearOuterPortAssociationSetting(switch["dpid"], switch["outer_port"], vlan_id)
    except:
        pass

    client.service.destroyRegion(region_name)

def has_region(service_url, region_name):
    client = Client(service_url + "?wsdl")
    return region_name in [x.regionName for x in client.service.showRegion()]
