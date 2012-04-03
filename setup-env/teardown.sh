#!/bin/bash

apt-get remove nova-common python-nova python-novaclient --purge -y
apt-get remove glance python-glance --purge -y
apt-get remove cobbler python-cobbler cobbler-common cobbler-web --purge -y
apt-get remove mysql-server mysql-common --purge -y

rm -rf /etc/nova
rm -rf /var/lib/nova
rm -rf /var/log/nova

rm -rf /etc/glance
rm -rf /var/lib/glance
rm -rf /var/log/glance

rm -rf /etc/cobbler
rm -rf /var/lib/cobbler
rm -rf /var/log/cobbler

rm -rf /etc/mysql
rm -rf /var/lib/mysql
rm -rf /var/log/mysql

screen -X -S dodai-compute quit
