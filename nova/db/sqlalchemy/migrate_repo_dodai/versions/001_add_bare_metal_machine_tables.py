# Copyright 2010 OpenStack LLC.
# All Rights Reserved.
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

from sqlalchemy import Boolean, Column, DateTime, Integer
from sqlalchemy import MetaData, String, Table
from nova import log as logging

meta = MetaData()

#
# New Tables
#
bare_metal_machines = Table('bare_metal_machines', meta,
        Column('created_at', DateTime(timezone=False)),
        Column('updated_at', DateTime(timezone=False)),
        Column('deleted_at', DateTime(timezone=False)),
        Column('deleted', Boolean(create_constraint=True, name=None)),
        Column('id', Integer(), primary_key=True, nullable=False),
        Column('name', String(length=255)),
        Column('instance_id', Integer()),
        Column('instance_type', String(length=255)),
        Column('vcpus', Integer()),
        Column('memory_mb', Integer()),
        Column('local_gb', Integer()),
        Column('availability_zone', String(length=255)),
        Column('ipmi_ip', String(length=255)),
        Column('pxe_ip', String(length=255)),
        Column('pxe_mac', String(length=255)),
        Column('storage_ip', String(length=255)),
        Column('storage_mac', String(length=255)),
        Column('service_mac1', String(length=255)),
        Column('service_mac2', String(length=255)),
        Column('server_port1', Integer()),
        Column('server_port2', Integer()),
        Column('dpid1', String(length=255)),
        Column('dpid2', String(length=255)),
        Column('vlan_id', Integer()),
        Column('status', String(length=255)),
        )


#
# Tables to alter
#

# (none currently)


def upgrade(migrate_engine):
    # Upgrade operations go here. Don't create your own engine;
    # bind migrate_engine to your metadata
    meta.bind = migrate_engine
    for table in (bare_metal_machines, ):
        try:
            table.create()
        except Exception:
            logging.info(repr(table))
