#!/usr/bin/env python

import os
import sys
import socket
import time

from esxsnmp.config import get_opt_parser, get_config, get_config_path
from esxsnmp.api import ESxSNMPAPI

def gen_ma_storefile():
    """Translated from the original Perl by jdugan"""

    argv = sys.argv
    oparse = get_opt_parser(default_config_file=get_config_path())
    (opts, args) = oparse.parse_args(args=argv)

    try:
        config = get_config(opts.config_file, opts)
    except ConfigError, e:
        print >>sys.stderr, e
        sys.exit(1)

    if not config.esdb_uri:
        print >>sys.stderr, "error: esdb_uri not specified in config"
        sys.exit(1)

    debug = opts.debug

    params = {}
    params['hostname'] = socket.gethostname()
    params['date'] = time.asctime()
    params['user'] = os.getlogin()
    params['args'] = " ".join(sys.argv)

    AUTHREALM = "ESnet-Public"
    DOMAIN = "es.net"
    HEADER = """<?xml version="1.0" encoding="UTF-8"?>


<!-- ===================================================================
<description>
   MA RRD configuration file

   $Id$
   project: perfSONAR

Notes:
   This is the configuration file which contains the information 
   about RRD files from ESnet.

   It was generated by %(user)s on %(hostname)s using %(args)s
   at %(date)s


    -Joe Metzger


</description>
==================================================================== -->
<nmwg:store
         xmlns:nmwg="http://ggf.org/ns/nmwg/base/2.0/"
         xmlns:netutil="http://ggf.org/ns/nmwg/characteristic/utilization/2.0/"
         xmlns:neterr="http://ggf.org/ns/nmwg/characteristic/errors/2.0/"
         xmlns:netdisc="http://ggf.org/ns/nmwg/characteristic/discards/2.0/"
         xmlns:nmwgt="http://ggf.org/ns/nmwg/topology/2.0/" 
         xmlns:snmp="http://ggf.org/ns/nmwg/tools/snmp/2.0/"         
         xmlns:nmwgt3="http://ggf.org/ns/nmwg/topology/3.0/" >

     <!-- Note: The URNs and the nmwgt3 namespace are possible implementations, and not standard.
          The URNs should not be meta-data IDs. But maybe they should be idRefs. But this seems
          to be expanding the scope of a reference significantly...  Joe
      -->

     <!--  metadata section  -->

""" % params


    bogusIP = "BOGUS1"

    print HEADER

    client = ESxSNMPAPI(config.esdb_uri)

    oidset_rtr_map = {}
    interfaces = []

    rtrs = client.get_routers()
    devices = [ x['name'] for x in rtrs['children']]

    for device in devices:
        if device.startswith('wifi'):
            continue

        try:
            device_fqdn = socket.gethostbyaddr(device)[0]
        except socket.herror:
            device_fqdn = device

        if debug:
            print >>sys.stderr, "starting %s" % device

        ifaces = client.get_interfaces(device)['children']

        for iface in ifaces:
            if debug:
                print >>sys.stderr, iface['name']

            if iface['ipAddr']:
                try:
                    iface['dns'] = socket.gethostbyaddr(iface['ipAddr'])[0]
                except socket.herror:
                    iface['dns'] = ''
            else:
                iface['dns'] = ''

            iface['key'] = '%s:%s' % (device, iface['ifDescr'])

            iface['device'] = device
            iface['device_fqdn'] = device_fqdn
            
            if iface['ifHighSpeed'] == 0:
                iface['speed'] = iface['ifSpeed']
            else:
                iface['speed'] = iface['ifHighSpeed'] * int(1e6)

            iface['domain'] = DOMAIN
            iface['authrealm'] = AUTHREALM
            iface['ifAlias'] = iface['ifAlias'].replace('&','')

            interfaces.append(iface)

        if debug:
            print >>sys.stderr, "done with %s" % (device)

    #
    # Now need to generate XML name spaces/rnc info
    #

    DATA = [] # Contains the data

    i = 0

    for iface in interfaces:
        if not iface['ifDescr']:
            continue

        if iface['ipAddr']:
            iface['ipaddr_line'] = """
\t\t\t\t<nmwgt:ifAddress type="ipv4">%s</nmwgt:ifAddress>""" % iface['ipAddr']
        else:
            iface['ipaddr_line'] = ''

        for subj, event_type, suffix, units in (
                ('netutil', 'utilization', '',         'bps'),
                ('neterr',  'errors',      '/error',   'Eps'),
                ('netdisc', 'discards',    '/discard', 'Dps'),
                ):
            iface['subj'] = subj
            iface['event_type'] = event_type
            iface['units'] = units
            for dir in ('in', 'out'):
                i += 1
                iface['i'] = i
                iface['dir'] = dir
                iface['name'] = iface['uri'] + suffix + '/' + dir

                d = """
\t<nmwg:metadata xmlns:nmwg="http://ggf.org/ns/nmwg/base/2.0/" id="meta%(i)d">
\t\t<%(subj)s:subject xmlns:%(subj)s="http://ggf.org/ns/nmwg/characteristic/%(event_type)s/2.0/" id="subj%(i)d">
\t\t\t<nmwgt:interface xmlns:nmwgt="http://ggf.org/ns/nmwg/topology/2.0/">
\t\t\t\t<nmwgt3:urn xmlns:nmwgt3="http://ggf.org/ns/nmwg/topology/base/3.0/">urn:ogf:network:domain=%(domain)s:node=%(device)s:port=%(ifDescr)s</nmwgt3:urn>%(ipaddr_line)s
\t\t\t\t<nmwgt:hostName>%(device_fqdn)s</nmwgt:hostName>
\t\t\t\t<nmwgt:ifName>%(ifDescr)s</nmwgt:ifName>
\t\t\t\t<nmwgt:ifDescription>%(ifAlias)s</nmwgt:ifDescription>
\t\t\t\t<nmwgt:capacity>%(speed)s</nmwgt:capacity>
\t\t\t\t<nmwgt:direction>%(dir)s</nmwgt:direction>
\t\t\t\t<nmwgt:authRealm>%(authrealm)s</nmwgt:authRealm>
\t\t\t</nmwgt:interface>
\t\t</%(subj)s:subject>
\t\t<nmwg:eventType>http://ggf.org/ns/nmwg/characteristic/%(event_type)s/2.0</nmwg:eventType>
\t\t<nmwg:parameters id="metaparam%(i)d">
\t\t\t<nmwg:parameter
name="supportedEventType">http://ggf.org/ns/nmwg/characteristic/%(event_type)s/2.0</nmwg:parameter>
\t\t\t<nmwg:parameter name="supportedEventType">http://ggf.org/ns/nmwg/tools/snmp/2.0</nmwg:parameter>
\t\t</nmwg:parameters>
\t</nmwg:metadata>
\t<nmwg:data xmlns:nmwg="http://ggf.org/ns/nmwg/base/2.0/" id="data%(i)d" metadataIdRef="meta%(i)d">
\t\t<nmwg:key id="keyid%(i)d">
\t\t\t<nmwg:parameters id="dataparam%(i)d">
\t\t\t\t<nmwg:parameter name="type">esxsnmp</nmwg:parameter>
\t\t\t\t<nmwg:parameter name="valueUnits">%(units)s</nmwg:parameter>
\t\t\t\t<nmwg:parameter name="name">%(name)s</nmwg:parameter>
\t\t\t\t<nmwg:parameter name="eventType">http://ggf.org/ns/nmwg/characteristic/%(event_type)s/2.0</nmwg:parameter>
\t\t\t</nmwg:parameters>
\t\t</nmwg:key>
\t</nmwg:data>""" % iface

            DATA.append(d)


    print ''.join(DATA)
    print '</nmwg:store>'
           
