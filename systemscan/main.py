
# Copyright 2008-2012 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import sys, getopt
import xmlrpclib
import string
import os
import commands
import pprint
import math
import json
import re
import shutil
import glob
import tempfile
from subprocess import Popen, PIPE

sys.path.append("/usr/share/smolt/client")
import smolt
from systemscan.disks import Disks
from procfs import procfs

USAGE_TEXT = """
Usage:  beaker-system-scan [-d] [-j] [[-h <HOSTNAME>] [-S server]]
"""

def get_helper_program_output(program, *args):
    """ Run an external program and return it's output"""
    env = dict(os.environ)
    env['PATH'] = '/usr/libexec/beaker-system-scan:..:.:' + env['PATH']
    proc = Popen([program] + list(args), env=env,
                stdout=PIPE, stderr=PIPE)
    out, err = proc.communicate()
    if proc.returncode:
        raise RuntimeError('Error %s running %s: %s' % (proc.returncode, program, err))
    return out

def push_inventory(method, hostname, inventory):
   session = xmlrpclib.Server(lab_server, allow_none=True)
   try:
      resp = getattr(session, method)(hostname, inventory)
      if(resp != 0) :
         raise NameError, "ERROR: Pushing Inventory for host %s." % hostname
   except:
      raise

def check_for_virt_iommu():

    virt_iommu = 0
    cpu_info = smolt.read_cpuinfo()
    cpu_info_pat = re.compile("x86")

    if not cpu_info_pat.search(cpu_info['platform']):
        #only x86 boxes support virt iommu
        return 0

    #test what type of system we are on
    if os.path.exists("/sys/firmware/acpi/tables/DMAR"):
        # alright we are on an Intel vt-d box
        hwu = False
        ba = False

        # iasl can't read directly from /sys
        shutil.copyfile('/sys/firmware/acpi/tables/DMAR', 'DMAR.dat')

        # create ascii file
        os.system("iasl -d DMAR.dat > /dev/null 2>&1")
        if os.path.exists("DMAR.dsl"):
            f = open("DMAR.dsl", 'r')

            #look for keywords to validate ascii file
            hwu_pat = re.compile ('Hardware Unit')
            ba_pat = re.compile ('Base Address')
            ba_inv_pat = re.compile ('0000000000000000|FFFFFFFFFFFFFFFF')

            for line in f.readlines():
                if hwu_pat.search(line):
                    hwu = True
                if ba_pat.search(line):
                    if ba_inv_pat.search(line):
                        print >> sys.stderr, "VIRT_IOMMU: Invalid Base address: 0's or F's"
                    else:
                        ba = True
            if not hwu:
                print >> sys.stderr, "VIRT_IOMMU: No Hardware Unit"
            elif not ba:
                print >> sys.stderr, "VIRT_IOMMU: No Base Address"
            else:
                virt_iommu = 1
        else:
            print >> sys.stderr, "VIRT_IOMMU: Failed to create DMAR.dsl"

    elif os.path.exists("/sys/firmware/acpi/tables/IVRS"):
        # alright we are on an AMD iommu box
        #  we don't have a good way to validate this
        virt_iommu = 1

    return virt_iommu

def kernel_inventory():
    data = {}
    data['VIRT_IOMMU'] = False

    ##########################################
    # check for virtual iommu/vt-d capability
    # if this passes, assume we pick up sr-iov for free

    if check_for_virt_iommu():
        data['VIRT_IOMMU'] = True

    ##########################################
    # determine which stroage controller has a disk behind it
    path = "/sys/block"
    virt_pat = re.compile('virtual')
    floppy_pat = re.compile('fd[0-9]')
    sr_pat = re.compile('sr[0-9]')
    for block in glob.glob( os.path.join(path, '*')):
        #skip read only/floppy devices
        if sr_pat.search(block) or floppy_pat.search(block):
            continue

        #skip block devices that don't point to a device
        if not os.path.islink(block + "/device"):
            continue
        sysfs_link = os.readlink(block + "/device")

        #skip virtual devices
        if virt_pat.search(sysfs_link):
            continue

        #cheap way to create an absolute path, there is probably a better way
        sysfs_path = sysfs_link.replace('../..','/sys')

        #start abusing hal to give us the info we want
        cmd = 'hal-find-by-property --key linux.sysfs_path --string %s' % sysfs_path
        status,udi =  commands.getstatusoutput(cmd)
        if status:
            print >> sys.stderr, "DISK_CONTROLLER: hal-find-by-property failed: %d" % status
            continue

        while udi:
            cmd = 'hal-get-property --udi %s --key info.linux.driver 2>/dev/null' % udi
            status, driver = commands.getstatusoutput(cmd)
            if status == 0 and driver != "sd" and driver != "sr":
                #success
                data['DISK_CONTROLLER'] = driver
                break

            #get the parent and try again
            cmd = 'hal-get-property --udi %s  --key info.parent' % udi
            status,udi =  commands.getstatusoutput(cmd)
            if status:
                print >> sys.stderr, "DISK_CONTROLLER: hal-get-property failed: %d" % status
                break

        if not udi:
            print >> sys.stderr, "DISK_CONTROLLER: can not determine driver for %s" %block

    ##########################################
    # determine if machine is using multipath or not

    #ok, I am really lazy
    #remove the default blacklist in /etc/multipath.conf
    if os.path.exists('/etc/multipath.conf'):
        os.system("sed -i '/^blacklist/,/^}$/d' /etc/multipath.conf")
        #restart multipathd to see what it detects
        #this spits out errors if the root device is on a
        #multipath device, I guess ignore for now and hope the code
        #correctly figures things out
        os.system("service multipathd restart > /dev/null")
        #the multipath commands will display the topology if it
        #exists otherwise nothing
        #filter out vbds and single device paths
        status, mpaths = commands.getstatusoutput("multipath -ll")
        mp = False
        if status:
            print >> sys.stderr, "MULTIPATH: multipath -ll failed with %d" % status
        else:
            count = 0
            mpath_pat = re.compile(" dm-[0-9]* ")
            sd_pat = re.compile(" sd[a-z]")
            for line in mpaths.split('\n'):
                #reset when a new section starts
                if mpath_pat.search(line):
                    # found at least one mp instance, declare success
                    if count > 1:
                        mp = True
                        break
                    count = 0

                #a hit! increment to indicate this
                if sd_pat.search(line):
                    count = count + 1

        if mp == True:
            data['DISK_MULTIPATH'] = True
        else:
            data['DISK_MULTIPATH'] = False

    return data

def legacy_inventory(inv):
    # Take the gathered inventory data, fill legacy key/value schema with it
    # and gather any missing bits.
    data = {}
    data['MODULE'] = []
    data['HVM'] = False
    data['DISK'] = []
    data['BOOTDISK'] = []
    data['DISKSPACE'] = 0
    data['NR_DISKS'] = 0
    data['NR_ETH'] = 0
    data['NR_IB'] = 0

    data['ARCH'] = inv['Arch'][0]
    data['CPUFAMILY'] = "%s" % inv['Cpu']['family']
    data['CPUVENDOR'] = inv['Cpu']['vendor']
    data['CPUMODEL'] = inv['Cpu']['modelName']
    data['CPUMODELNUMBER'] = "%s" % inv['Cpu']['model']
    data['PROCESSORS'] = inv['Cpu']['processors']
    data['VENDOR'] = inv['vendor']
    data['MODEL'] = inv['model']
    data['FORMFACTOR'] = inv['formfactor']
    data['CPUFLAGS'] = inv['Cpu']['CpuFlags']
    data['PCIID'] = ["%s:%s" % (d['vendorID'], d['deviceID']) for d in inv['Devices'] if d['bus'] == 'pci']
    data['USBID'] = ["%s:%s" % (d['vendorID'], d['deviceID']) for d in inv['Devices'] if d['bus'] == 'usb']

    # The below data (and kernel_inventory()) has not (yet) made it to the new schema
    # (and formfactor above)

    modules =  commands.getstatusoutput('/sbin/lsmod')[1].split('\n')[1:]
    for module in modules:
        data['MODULE'].append(module.split()[0])

    # Find Active Storage Driver(s)
    bootdisk = None
    bootregex = re.compile(r'/dev/([^ ]+) on /boot')
    disks = commands.getstatusoutput('/bin/mount')[1].split('\n')[1:]
    for disk in disks:
        if bootregex.search(disk):
            # Replace / with !, needed for cciss
            bootdisk = bootregex.search(disk).group(1).replace('/','!')

    if bootdisk:
        try:
            drivers = get_helper_program_output('getdriver.sh', bootdisk).split('\n')[1:]
        except RuntimeError:
            # /boot might be on a device-mapper device, but getdriver.sh
            # doesn't handle those properly. We don't care much about
            # BOOTDISK though so just ignore failures.
            pass
        else:
            for driver in drivers:
                data['BOOTDISK'].append(driver)
    # Find Active Network interface
    iface = None
    for line in  commands.getstatusoutput('route -n')[1].split('\n'):
        if line.find('0.0.0.0') == 0:
            iface = line.split()[-1:][0] #eth0, eth1, etc..
    if iface:
        drivers = get_helper_program_output('getdriver.sh', iface).split('\n')
        if len(drivers) == 1:
            data['NETWORK'] = drivers[0]
        else:
            data['NETWORK'] = drivers[1:][0]

    # disk sizes are converted to GiB in key-values for backwards compatibility
    disks = Disks()
    data['DISK'] = []
    for disk in disks:
        data['DISK'].append(disk.size / 1024**2)
    data['DISKSPACE'] = disks.disk_space / 1024**2
    data['NR_DISKS'] = disks.nr_disks

    # finding out eth and ib interfaces...
    eth_pat = re.compile ('^ *eth\d+:')
    ib_pat  = re.compile ('^ *ib\d+:')
    for line in open("/proc/net/dev", "r"):
        if eth_pat.match(line):
           data['NR_ETH'] += 1
        elif ib_pat.match(line):
           data['NR_IB'] += 1

    # checking for whether or not the machine is hvm-enabled.
    caps = ""
    if os.path.exists("/sys/module/kvm_amd") or \
       os.path.exists("/sys/module/kvm_intel"):
           data['HVM'] = True
    elif os.path.exists('/proc/pal/cpu0/processor_info'): # ia64
        for line in open('/proc/pal/cpu0/processor_info', 'r'):
            if re.match('Virtual machine features.*: On', line):
                data['HVM'] = True

    if os.path.exists("/root/NETBOOT_METHOD.TXT"):
        data['NETBOOT_METHOD'] = open('/root/NETBOOT_METHOD.TXT', 'r').readline()[:-1]
    return data

def read_inventory():
    # get the data from SMOLT but modify it for how RHTS expects to see it
    # Eventually we'll switch over to SMOLT properly.
    data = {}
    flags = []
    data['Devices'] = []

    procCpu  = procfs.cpuinfo()
    smoltCpu = smolt.read_cpuinfo()
    memory   = smolt.read_memory()
    profile  = smolt.Hardware()

    arch = smoltCpu['platform']

    if arch in ["i386", "x86_64"]:
        for cpuflag in procCpu.tags['flags'].split(" "):
            flags.append(cpuflag)
        cpu = dict(vendor     = smoltCpu['type'],
                   model      = int(procCpu.tags['model']),
                   modelName  = smoltCpu['model'],
                   speed      = float(procCpu.tags['cpu mhz']),
                   processors = int(procCpu.nr_cpus),
                   cores      = int(procCpu.nr_cores),
                   sockets    = int(procCpu.nr_sockets),
                   CpuFlags   = flags,
                   family     = int(smoltCpu['model_number']),
                   stepping   = int(procCpu.tags['stepping']),
                  )
    elif arch in ["ppc", "ppc64"]:
        cpu = dict(vendor     = "IBM",
                   model      = int(''.join(re.split('^.*([0-9a-f]{4})\s([0-9a-f]{4}).*$',
                                                     procCpu.tags['revision'])), 16),
                   modelName  = str(procCpu.tags['cpu']),
                   speed      = float(re.findall('\d+.+\d+', procCpu.tags['clock'])[0]),
                   processors = int(procCpu.nr_cpus),
                   cores      = 0,
                   sockets    = 0,
                   CpuFlags   = flags,
                   family     = 0,
                   stepping   = 0,
                 )
    elif arch in ["s390", "s390x"]:
        for cpuflag in procCpu.tags['features'].split(" "):
            flags.append(cpuflag)
        proc = dict([tuple(s.strip() for s in kv.split('=')) for kv in procCpu.tags['processor 0'].split(',')])
        cpu = dict(vendor     = str(procCpu.tags['vendor_id']),
                   model      = int(proc['identification'], 16),
                   modelName  = str(proc['machine']),
                   processors = int(procCpu.tags['# processors']),
                   cores      = 0,
                   sockets    = 0,
                   CpuFlags   = flags,
                   family     = 0,
                   speed      = 0,
                   stepping   = 0,
                  )
    elif arch == "ia64":
        for cpuflag in procCpu.tags['features'].split(","):
            flags.append(cpuflag.strip())
        cpu = dict(vendor     = smoltCpu['type'],
                   model      = int(procCpu.tags['model']),
                   modelName  = smoltCpu['model'],
                   speed      = float(procCpu.tags['cpu mhz']),
                   processors = int(procCpu.nr_cpus),
                   cores      = int(procCpu.nr_cores),
                   sockets    = int(procCpu.nr_sockets),
                   CpuFlags   = flags,
                   family     = int(smoltCpu['model_rev']),
                   stepping   = 0,
                  )

    data['Cpu'] = cpu
    data['Arch'] = [arch]
    data['vendor'] = "%s" % profile.host.systemVendor
    data['model'] = "%s" % profile.host.systemModel
    data['formfactor'] = "%s" % profile.host.formfactor
    data['memory'] = int(memory['ram'])

    disklist = []
    diskdata = {}
    disks = Disks()
    for disk in Disks():
        disklist.append(disk.to_dict())
    diskdata['Disks'] = disklist

    data['Disk'] = diskdata

    if hasattr(profile.host, 'numaNodes'):
        data['Numa'] = {'nodes': profile.host.numaNodes}
    else:
        data['Numa'] = {
            'nodes': len(glob.glob('/sys/devices/system/node/node*')), #: number of NUMA nodes in the system, or 0 if not supported
        }
    try:
        hypervisor = get_helper_program_output('hvm_detect')
    except OSError as e:
        if e.errno == os.errno.ENOENT and arch != 'x86_64':
            pass
        else:
            raise
    else:
        hvm_map = {"No KVM or Xen HVM\n"    : None,
                   "KVM guest.\n"           : u'KVM',
                   "Xen HVM guest.\n"       : u'Xen',
                   "Microsoft Hv guest.\n"  : u'HyperV',
                   "VMWare guest.\n"        : u'VMWare',
                }
        data['Hypervisor'] = hvm_map[hypervisor]

    for VendorID, DeviceID, SubsysVendorID, SubsysDeviceID, Bus, Driver, Type, Description in profile.deviceIter():
        device = dict ( vendorID = "%04x" % (VendorID and VendorID or 0),
                        deviceID = "%04x" % (DeviceID and DeviceID or 0),
                        subsysVendorID = "%04x" % (SubsysVendorID and SubsysVendorID or 0),
                        subsysDeviceID = "%04x" % (SubsysDeviceID and SubsysDeviceID or 0),
                        bus = str(Bus),
                        driver = str(Driver),
                        type = str(Type),
                        description = str(Description))
        data['Devices'].append(device)

    return data

def usage():
    print USAGE_TEXT
    sys.exit(-1)

def main():
    global lab_server, hostname

    lab_server = None
    hostname = None
    debug = 0
    json_output = 0

    if ('LAB_SERVER' in os.environ.keys()):
        lab_server = os.environ['LAB_SERVER']
    if ('HOSTNAME' in os.environ.keys()):
        hostname = os.environ['HOSTNAME']

    args = sys.argv[1:]
    try:
        opts, args = getopt.getopt(args, 'dh:S:j', ['server='])
    except:
        usage()
    for opt, val in opts:
        if opt in ('-d', '--debug'):
            debug = 1
        if opt in ('-j', '--json') and debug:
            json_output = 1
        if opt in ('-h', '--hostname'):
            hostname = val
        if opt in ('-S', '--server'):
            lab_server = val

    inventory = read_inventory()
    legacy_inv = legacy_inventory(inventory)
    legacy_inv.update(kernel_inventory())
    del inventory['formfactor']
    if debug:
       if json_output:
          print json.dumps({'legacy':legacy_inv,
                            'Data':inventory})
       else:
          print "Legacy inventory:\n%s\nData:\n%s" % (
             pprint.pformat(legacy_inv), pprint.pformat(inventory))
    else:
        if not hostname:
            print "You must specify a hostname with the -h switch"
            sys.exit(1)

        if not lab_server:
            print "You must specify a lab_server with the -S switch"
            sys.exit(1)

        push_inventory("legacypush", hostname, legacy_inv)
        push_inventory("push", hostname, inventory)


if __name__ == '__main__':
    main()
    sys.exit(0)

