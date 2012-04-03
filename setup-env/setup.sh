#!/bin/bash

install() {
  target=$1
  echo "-----------------Begin to install $target-----------------------"
  install_$target
  echo "-----------------Finished---------------------------------------"
  echo ""
}

install_prerequired() {
  apt-get update
  apt-get install iscsitarget libgpm2 libpython2.7 libreadline5 libsigsegv2 socat vim-runtime liberror-perl git -y
}

install_mysql() {
  cat << MYSQL_PRESEED | debconf-set-selections
mysql-server-5.1 mysql-server/root_password password $MYSQL_PASS
mysql-server-5.1 mysql-server/root_password_again password $MYSQL_PASS
mysql-server-5.1 mysql-server/start_on_boot boolean true
MYSQL_PRESEED

  apt-get install mysql-server -y
}

install_glance() {
  apt-get install glance -y
  glance add name="default-kernel" is_public=true container_format=aki disk_format=aki < images/default-kernel
}

install_cobbler() {
  cat << COBBLER_PRESEED | debconf-set-selections
cobbler cobbler/password password cobbler
cobbler cobbler/server_and_next_server string $COBBLER_IP
COBBLER_PRESEED

  apt-get install isc-dhcp-server cobbler cobbler-web -y

  cp configs/cobbler/* /etc/cobbler/
  sed -i -e s/COBBLER_IP/$COBBLER_IP/ /etc/cobbler/settings
  sed -i -e s/COBBLER_IP_RANGE/"$COBBLER_IP_RANGE"/ /etc/cobbler/dhcp.template
  sed -i -e s/COBBLER_IP/$COBBLER_IP/ /etc/cobbler/dhcp.template
  sed -i -e s/COBBLER_SUBNET/$COBBLER_SUBNET/ /etc/cobbler/dhcp.template
  sed -i -e s/COBBLER_NETMASK/$COBBLER_NETMASK/ /etc/cobbler/dhcp.template
  sed -i -e s/COBBLER_GW/$COBBLER_GW/ /etc/cobbler/dhcp.template
  sed -i -e s/COBBLER_DNS/$COBBLER_DNS/ /etc/cobbler/dhcp.template

  service cobbler stop
  service cobbler start
  sleep 5
  cobbler sync

  #TODO: How to download os-duper?
  #cp -r images/os-duper /var/lib/tftpboot/
}

install_dodai_compute() {
  apt-get install python-novaclient python-nova nova-common nova-api nova-objectstore nova-volume nova-compute nova-compute-kvm nova-network nova-scheduler -y
  apt-get install python-suds -y

  cp -r ../nova /usr/lib/python2.7/dist-packages/
  cp ../bin/nova-manage /usr/bin/
  cp ../bin/dodai-* /usr/bin/
  cp ../debian/dodai-machine-state-monitor.conf /etc/init/

  cp configs/dodai-compute/nova.conf /etc/nova/
  sed -i -e s/COBBLER_IP/$COBBLER_IP/ /etc/nova/nova.conf
  sed -i -e s/HOST_IP/$HOST_IP/ /etc/nova/nova.conf
  sed -i -e s/MYSQL_PASS/$MYSQL_PASS/ /etc/nova/nova.conf
  sed -i -e s/OFC_IP/$OFC_IP/ /etc/nova/nova.conf

  mysql -uroot -p$MYSQL_PASS -e 'CREATE DATABASE nova;'
  nova-manage db sync

  mysql -uroot -p$MYSQL_PASS -e 'CREATE DATABASE dodai;'
  nova-manage dodai_db sync

  for service in nova-api nova-network nova-compute nova-scheduler nova-objectstore dodai-machine-state-monitor
  do
    stop $service
    start $service
  done

  # change owner to nova because dodai-compute will write to those folders.
  chown -R nova:root /usr/share/cobbler/webroot/cobbler/
  chown -R nova:root /var/lib/tftpboot/pxelinux.cfg/

  # remove existed image types
  for type in `nova-manage flavor list | awk '{print $1}' | cut -f1 -d ":"`
  do 
    nova-manage flavor delete $type
  done
}

screen_it() {
  NL=`echo -ne '\015'`
  echo "Add tab $1 with command \"$2\"."
  screen -S dodai-compute -X screen -t $1
  sleep 1.5
  screen -S dodai-compute -p $1 -X stuff "$2$NL"
}

install_monitor_screen() {
  screen -d -m -S dodai-compute -t shell -s /bin/bash
  sleep 1
  screen -r dodai-compute -X hardstatus alwayslastline "%{.bW}%-w%{.rW}%n %t%{-}%+w %=%{..G}%H %{..Y}%d/%m %c"

  screen_it n-api "tail -f /var/log/nova/nova-api.log"
  screen_it d-compute "tail -f /var/log/nova/nova-compute.log"
  screen_it d-monitor "tail -f /var/log/nova/dodai-machine-state-monitor"
  screen_it n-scheduler "tail -f /var/log/nova/nova-scheduler.log"
  screen_it n-object "tail -f /var/log/nova/nova-objectstore.log"
  screen_it g-api "tail -f /var/log/glance/api.log"
  screen_it g-reg "tail -f /var/log/glance/registry.log"
}

confirm_services_status() {
  echo "-----------------Begin to confirm states of services----------------------------------"

  error=0
  for service in nova-api nova-compute nova-scheduler nova-objectstore nova-network glance-api glance-registry mysql rabbitmq-server cobbler isc-dhcp-server
  do
    service $service status
    if [ $? != 0 ]; then
      echo "Service $service is not running."
      error=1
    fi
  done

  status dodai-machine-state-monitor
  if [ $? != 0 ]; then
    echo "Service dodai-machine-state-monitor is not running."
    error=1
  fi

  if [ $error != 0 ]; then
    exit $error
  fi

  echo "-----------------Finished---------------------------------------"
  echo ""
}

cmd_succeed_or_exit() {
  if [ $? != 0 ]; then
    echo "Failed."
    exit 1
  fi
  echo "OK."
  echo ""
}

do_test() {
  echo "-----------------Begin to test----------------------------------"

  echo "Test to create nova user."
  nova-manage user create test_user
  cmd_succeed_or_exit

  echo "Test to create nova project."
  nova-manage project create test_proj test_user
  cmd_succeed_or_exit

  echo "Test to use euca2ools."
  nova-manage project zipfile test_proj test_user ~/nova.zip
  unzip -d ~/nova ~/nova.zip
  . ~/nova/novarc
  euca-describe-images
  cmd_succeed_or_exit

  echo "Remove test user, project and other files."
  nova-manage project delete test_proj
  nova-manage user delete test_user
  rm ~/nova.zip
  rm -rf ~/nova

  echo "-----------------Finished---------------------------------------"
  echo ""
}

show_next_steps() {
  echo "-----------------Show next steps----------------------------------"

  cat next-steps.txt

  echo "-----------------Finished---------------------------------------"
  echo ""
}

load_setting() {
  source localrc
  HOST_IP=`LC_ALL=C /sbin/ifconfig eth0 | grep -m 1 'inet addr:'| cut -d: -f2 | awk '{print $1}'`
  if [ "$COBBLER_IP" = "" ]; then
    COBBLER_IP=$HOST_IP
  fi
}

start_time=`date +%s`

cd `dirname $0`
load_setting

for soft in prerequired mysql glance cobbler dodai_compute monitor_screen
do
  install $soft
done

confirm_services_status
do_test
show_next_steps

end_time=`date +%s`
time=$(( $end_time - $start_time ))
echo "The installation time: $time seconds."
