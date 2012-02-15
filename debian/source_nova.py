#!/usr/bin/python

'''openstack Apport interface

Copyright (C) 2010 Canonical Ltd.
Author: Chuck Short <chuck.short@canonical.com>

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation; either version 2 of the License, or (at your
option) any later version.  See http://www.gnu.org/copyleft/gpl.html for
the full text of the license.
'''

import os
import subprocess
from apport.hookutils import *

def add_info(report,ui):
	response = ui.yesno("The contents of your /etc/nova/nova.conf file "
			    "may help developers diagnose your bug more "
			    "quickly. However, it may contain sensitive "
			    "information. Do you want to include it in your "
			    "bug report?")
	if response == None: # user cancelled
		raise StopIteration

	elif response == True:
		attach_file(report, '/etc/nova/nova.conf', 'NovaConf')

		attach_related_packages(report,
				      ['python-nova', 'nova-common', 'nova-compute', 'nova-scheduler',
					'nova-volume', 'nova-api', 'nova-network', 'nova-objectstore',
					'nova-doc'])
