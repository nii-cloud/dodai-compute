from suds.client import Client
from nova import exception

def update_for_run_instance(service_url, region_name, dpid, port_no, vlan_id, create_region):
    # check region name
    client = Client(url)
    if region_name not in client.service.showRegion():
       if not create_region:
           raise exception.OFCRegionNotFound(region_name)

       client.service.createRegion(region_name) 

    client.service.setServerPort(dpid, port_no, region_name)
    client.service.setOuterPort(dpid, port_no)
    client.service.setOuterPortAssociationSetting(dpid, port_no, vlan_id, vlan_id, region_name)
    client.service.save()

def update_for_terminate_instance():
    pass
