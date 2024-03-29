#!/usr/bin/env python

import logging
import os
import os.path
import sys
import time
import signal
import errno
import __main__

from subprocess import Popen, PIPE, STDOUT

import cPickle as pickle

try:
    import json
except ImportError:
    import simplejson as json

import sqlalchemy

import tsdb
import tsdb.row
from tsdb.error import TSDBError, TSDBAggregateDoesNotExistError, \
        TSDBVarDoesNotExistError, InvalidMetaData

import esxsnmp.sql

from esxsnmp.util import setproctitle, init_logging, get_logger, \
        remove_metachars, decode_alu_port
from esxsnmp.util import daemonize, setup_exc_handler
from esxsnmp.config import get_opt_parser, get_config, get_config_path
from esxsnmp.error import ConfigError
from esxsnmp.sql import IfRef, LSPOpStatus, ALUSAPRef

try:
    import cmemcache as memcache
except ImportError:
    try:
        import memcache
    except:
        raise Exception('no memcache library found')

PERSIST_SLEEP_TIME = 1


class PollResult(object):
    """PollResult contains the results of a polling run.

    The internals of PollResults may vary on a per subclass implementation,
    however all subclasses must implement a ``pickle`` method as these provide
    a baseline functionality for the generic ``PollPersister`` class.

    ``oidset_name``
        this is used to determine which PollPersisters are used to store this
        PollResult.
    ``prefix``
        the prefix where these results are to be stored
    ``timestamp``
        the timestamp for this PollResult
    ``data``
        the data to be stored, this is opaque at this level but must be
        pickleable.  some PollPersister require a particular format for
        ``data``.
    ``metadata``
        a dict of additional data about this data.  some PollPersisters require
        specific keys to exist in the ``metadata`` dict.
    """
    def __init__(self, oidset_name, device_name, oid_name, timestamp, data,
            metadata, **kwargs):
        self.oidset_name = oidset_name
        self.device_name = device_name
        self.oid_name = oid_name
        self.timestamp = timestamp
        self.data = data
        self.metadata = metadata

    def __str__(self):
        return '%s.%s %d' % (self.device_name, self.oidset_name,
                self.timestamp)

    def __iter__(self):
        return self.results.__iter__()

    def pickle(self):
        """Produce a pickle which represents this ``PollResult``."""
        return pickle.dumps(self)

    def json(self):
        return json.dumps(dict(
            oidset_name=self.oidset_name,
            device_name=self.device_name,
            oid_name=self.oid_name,
            timestamp=self.timestamp,
            data=self.data,
            metadata=self.metadata))


class PollPersister(object):
    """A PollPersister implements a storage method for PollResults."""
    STATS_INTERVAL = 60

    def __init__(self, config, qname):
        self.log = get_logger("espersistd.%s" % qname)
        self.config = config
        self.qname = qname
        self.running = False

        self.persistq = MemcachedPersistQueue(qname, config.espersistd_uri)

        self.data_count = 0
        self.last_stats = time.time()

    def store(self, result):
        pass

    def stop(self, x, y):
        self.log.debug("stop")
        self.running = False

    def run(self):
        self.log.debug("run")
        self.running = True
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        while self.running:
            task = self.persistq.get()
            if task:
                self.store(task)
                self.data_count += len(task.data)
                now = time.time()
                if now > self.last_stats + self.STATS_INTERVAL:
                    self.log.info("%d records written, %f records/sec" % \
                            (self.data_count,
                                float(self.data_count) / self.STATS_INTERVAL))
                    self.data_count = 0
                    self.last_stats = now
                del task
            else:
                time.sleep(PERSIST_SLEEP_TIME)


class StreamingPollPersister(PollPersister):
    """A StreamingPollPersister stores PollResults to a log file.

    ``conf.streaming_log_dir``
        Specifies the path name of the log file.

    """
    def __init__(self, config, q):
        PollPersister.__init__(self, config, q)

        self.filename = None
        self.fd = None

    def _rotate_file(self, dst):
        if self.fd:
            self.fd.close()

        self.filename = dst
        self.fd = open(os.path.join(self.config.streaming_log_dir,
            self.filename), "a")

    def store(self, result):
        dst = time.strftime("%Y%m%d_%H", time.gmtime(result.timestamp))
        if dst != self.filename:
            self._rotate_file(dst)

        self.fd.write(result.json())
        self.fd.write("\n\n")
        self.log.debug("stored %s %s %s to streaming log" % (result.oidset_name,
            result.oid_name, result.device_name))

class TSDBPollPersister(PollPersister):
    """Given a ``PollResult`` write the data to a TSDB.

    The TSDBWriter will use ``tsdb_root`` in ``config`` as the TSDB instance to
    write to.

    The ``data`` member of the PollResult must be a list of (name,value)
    pairs.  The ``metadata`` member of PollResult must contain the following
    keys::

        ``tsdb_flags``
            TSDB flags to be used

    """

    def __init__(self, config, qname):
        PollPersister.__init__(self, config, qname)

        self.tsdb = tsdb.TSDB(self.config.tsdb_root)

        session = esxsnmp.sql.Session()

        self.oidsets = {}
        self.poller_args = {}
        self.oids = {}
        self.oid_type_map = {}

        oidsets = session.query(esxsnmp.sql.OIDSet)

        for oidset in oidsets:
            self.oidsets[oidset.name] = oidset
            d = {}
            if oidset.poller_args:
                for arg in oidset.poller_args.split():
                    (k, v) = arg.split('=')
                    d[k] = v
                self.poller_args[oidset.name] = d

            for oid in oidset.oids:
                self.oids[oid.name] = oid
                try:
                    self.oid_type_map[oid.name] = eval("tsdb.row.%s" % \
                            oid.type.name)
                except AttributeError:
                    self.log.warning(
                            "warning don't have a TSDBRow for %s in %s" %
                            (oid.type.name, oidset.name))
        session.close()

    def store(self, result):
        oidset = self.oidsets[result.oidset_name]
        set_name = self.poller_args[oidset.name].get('set_name', oidset.name)
        basename = os.path.join(result.device_name, set_name)
        oid = self.oids[result.oid_name]
        flags = result.metadata['tsdb_flags']

        var_type = self.oid_type_map[oid.name]

        t0 = time.time()
        nvar = 0

        for var, val in result.data:
            if set_name == "SparkySet": # This is pure hack. A new TSDB row type should be created for floats
                val = float(val) * 100
            nvar += 1

            var_name = os.path.join(basename, var)

            try:
                tsdb_var = self.tsdb.get_var(var_name)
            except tsdb.TSDBVarDoesNotExistError:
                tsdb_var = self._create_var(var_type, var_name, oidset, oid)
            except tsdb.InvalidMetaData:
                tsdb_var = self._repair_var_metadata(var_type, var_name,
                        oidset, oid)
                continue  # XXX(jdugan): remove this once repair actually works

            tsdb_var.insert(var_type(result.timestamp, flags, val))

            if oid.aggregate:
                # XXX:refactor uptime should be handled better
                uptime_name = os.path.join(basename, 'sysUpTime')
                try:
                    self._aggregate(tsdb_var, var_name, result.timestamp,
                            uptime_name, oidset)
                except TSDBError, e:
                    self.log.error("Error aggregating: %s %s: %s" %
                            (result.device_name, result.oidset_name, str(e)))

        self.log.debug("stored %d vars in %f seconds: %s" % (nvar,
            time.time() - t0, result))

    def _create_var(self, var_type, var, oidset, oid):
        self.log.debug("creating TSDBVar: %s" % str(var))
        chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])

        tsdb_var = self.tsdb.add_var(var, var_type,
                oidset.frequency, chunk_mapper)

        if oid.aggregate:
            self._create_aggs(tsdb_var, oidset)

        tsdb_var.flush()

        return tsdb_var

    def _create_agg(self, tsdb_var, oidset, period):
        chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])
        if period == oidset.frequency:
            aggs = ['average', 'delta']
        else:
            aggs = ['average', 'delta', 'min', 'max']

        try:
            tsdb_var.add_aggregate(str(period), chunk_mapper, aggs)
        except Exception, e:
            self.log.error("Couldn't create aggregate %s" % (e))

    def _create_aggs(self, tsdb_var, oidset):
        self._create_agg(tsdb_var, oidset, oidset.frequency)

        if 'aggregates' in self.poller_args[oidset.name]:
            aggregates = self.poller_args[oidset.name]['aggregates'].split(',')
            for agg in aggregates:
                self._create_agg(tsdb_var, oidset, int(agg))

    def _repair_var_metadata(self, var_type, var, oidset, oid):
        self.log.error("var needs repair, skipping: %s" % var)
        #chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])

    def _aggregate(self, tsdb_var, var_name, timestamp, uptime_name, oidset):
        try:
            uptime = self.tsdb.get_var(uptime_name)
        except TSDBVarDoesNotExistError:
            # XXX this is killing the logger in testing revisit
            #self.log.warning("unable to get uptime for %s" % var_name)
            uptime = None

        min_last_update = timestamp - oidset.frequency * 40

        def log_bad(ancestor, agg, rate, prev, curr):
            self.log.debug("bad data for %s at %d: %f" % (ancestor.path,
                curr.timestamp, rate))

        def update_agg():
            tsdb_var.update_aggregate(str(oidset.frequency),
                uptime_var=uptime,
                min_last_update=min_last_update,
                # XXX(jdugan): should compare to ifHighSpeed?  this is BAD:
                max_rate=int(110e9),
                max_rate_callback=log_bad)

        try:
            update_agg()
        except TSDBAggregateDoesNotExistError:
            # XXX(jdugan): this needs to be reworked when we update all aggs
            self.log.error("creating missing aggregate for %s" % var_name)
            self._create_agg(tsdb_var, oidset, oidset.frequency)
            tsdb_var.flush()
            update_agg()
        except InvalidMetaData:
            self.log.error("bad metadata for %s" % var_name)


class HistoryTablePersister(PollPersister):
    """Provides common methods for table histories."""

    def update_db(self):
        """Compare the database to the poll results and update.

        This assumes that the database object has a begin_time and end_time
        and that self.new_data has the dictionary representing the new data
        and that self.old_data contains the database objects representing the
        old data.  It uses _new_row_from_dict() to create a new object when
        needed."""

        adds = 0
        changes = 0
        deletes = 0

        # iterate through what is currently in the database
        for old in self.old_data:
            # there is an entry in the new data: has anything changed?
            key = getattr(old, self.key)
            if key in self.new_data:
                new = self.new_data[key]
                attrs = new.keys()
                attrs.remove(self.key)
                changed = False

                for attr in attrs:
                    if not hasattr(old, attr):
                        self.log.error("Field " + attr + " is not contained in the object: %s" % str(old))
                        continue

                    if getattr(old, attr) != new[attr]:
                        changed = True
                        break

                if changed:
                    old.end_time = 'NOW'
                    new_row = self._new_row_from_obj(new)
                    self.db_session.add(new_row)
                    changes += 1

                del self.new_data[key]
            # no entry in self.new_data: interface is gone, update db
            else:
                old.end_time = 'NOW'
                deletes += 1

        # anything left in self.new_data is something new
        for new in self.new_data:
            new_row = self._new_row_from_obj(self.new_data[new])
            self.db_session.add(new_row)
            adds += 1

        self.db_session.commit()

        return (adds, changes, deletes)


class IfRefPollPersister(HistoryTablePersister):
    int_oids = ('ifSpeed', 'ifHighSpeed', 'ifMtu', 'ifType',
            'ifOperStatus', 'ifAdminStatus')

    def __init__(self, config, qname):
        HistoryTablePersister.__init__(self, config, qname)
        self.db_session = esxsnmp.sql.Session()

    def store(self, result):
        t0 = time.time()
        self.data = result.data

        self.device = self.db_session.query(esxsnmp.sql.Device).filter(
                esxsnmp.sql.Device.name == result.device_name).filter(
                        esxsnmp.sql.Device.end_time > 'NOW').one()

        self.old_data = self.db_session.query(IfRef).filter(
            sqlalchemy.and_(
                IfRef.deviceid == self.device.id, IfRef.end_time > 'NOW')
        )

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'ifdescr'

        adds, changes, deletes = self.update_db()

        self.db_session.commit()
        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        i = IfRef()
        i.deviceid = self.device.id
        i.begin_time = 'NOW'
        i.end_time = 'Infinity'
        for attr in obj.keys():
            setattr(i, attr, obj[attr])
        return i

    def _resolve_ifdescr(self, ifdescr, ifindex):
        return ifdescr

    def _build_objs(self):
        ifref_objs = {}
        ifIndex_map = {}

        for name, val in self.data['ifDescr']:
            foo, ifIndex = name.split('.')
            ifIndex = int(ifIndex)
            ifDescr = self._resolve_ifdescr(val, ifIndex)
            ifIndex_map[ifIndex] = ifDescr
            ifref_objs[ifDescr] = dict(ifdescr=ifDescr, ifindex=ifIndex)

        for name, val in self.data['ipAdEntIfIndex']:
            foo, ipAddr = name.split('.', 1)
            for (a,b) in self.data['ipAdEntAddr']:
                (foo,index) = a.split('.',1)
                if index == ipAddr:
                    ipAddr = b
                    break
            ifref_objs[ifIndex_map[val]]['ipAddr'] = ipAddr
            

        remaining_oids = self.data.keys()
        remaining_oids.remove('ifDescr')
        remaining_oids.remove('ipAdEntIfIndex')
        remaining_oids.remove('ipAdEntAddr')

        for oid in remaining_oids:
            for name, val in self.data[oid]:
                if oid in self.int_oids:
                    val = int(val)
                if oid == 'ifPhysAddress':
                    if val != '':
                        val = ":".join(["%02x" % ord(i) for i in val])
                    else:
                        val = None
                foo, ifIndex = name.split('.')
                ifIndex = int(ifIndex)
                ifref_objs[ifIndex_map[ifIndex]][oid.lower()] = val

        return ifref_objs

class ALUIfRefPollPersister(IfRefPollPersister):
    """ALU specific hacks for IfRef"""

    def _resolve_ifdescr(self, ifdescr, ifindex):
        """The interface description which is in ifAlias on most platforms is
        the third comma separated field in ifDescr on the ALU.  We normalize
        ifDescr just be the interface name and put a copy of the interface
        description in ifAlias."""

        parts = ifdescr.split(',')
        if len(parts) > 2:
            if not self.data.has_key('ifAlias'):
                self.data['ifAlias'] = []
            ifalias = parts[2].replace('"','')
            self.data['ifAlias'].append(('ifAlias.%d' % ifindex, ifalias))
        return parts[0]

class ALUSAPRefPersister(HistoryTablePersister):
    int_oids = ('sapIngressQosPolicyId', 'sapEgressQosPolicyId')

    def __init__(self, config, qname):
        HistoryTablePersister.__init__(self, config, qname)
        self.db_session = esxsnmp.sql.Session()

    def store(self, result):
        self.data = result.data
        t0 = time.time()

        self.device = self.db_session.query(esxsnmp.sql.Device).filter(
                esxsnmp.sql.Device.name == result.device_name).filter(
                        esxsnmp.sql.Device.end_time > 'NOW').one()

        self.old_data = self.db_session.query(ALUSAPRef).filter(
            sqlalchemy.and_(ALUSAPRef.deviceid
                == self.device.id, ALUSAPRef.end_time > 'NOW')
        )

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'name'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        r = ALUSAPRef()
        r.deviceid = self.device.id
        r.begin_time = 'NOW'
        r.end_time = 'Infinity'
        for attr in obj.keys():
            setattr(r, attr, obj[attr])

        return r

    def _build_objs(self):
        objs = {}

        for oid, entries in self.data.iteritems():
            for k, val in entries:
                _, vpls, port, vlan  = k.split('.')
                name = "%s-%s-%s" % (vlan, decode_alu_port(port), vlan)

                if oid in self.int_oids:
                    val = int(val)

                if not name in objs:
                    objs[name] = dict(name=name)
                    objs[name]['name'] = name

                o = objs[name]
                o[oid.lower()] = val

        return objs

class LSPOpStatusPersister(HistoryTablePersister):
    def __init__(self, config, qname):
        HistoryTablePersister.__init__(self, config, qname)
        self.db_session = esxsnmp.sql.Session()

    def store(self, result):
        self.lsp_data = result.data
        t0 = time.time()

        self.device = self.db_session.query(esxsnmp.sql.Device).filter(
                esxsnmp.sql.Device.name == result.device_name).filter(
                        esxsnmp.sql.Device.end_time > 'NOW').one()

        self.old_data = self.db_session.query(LSPOpStatus).filter(
            sqlalchemy.and_(LSPOpStatus.deviceid
                == self.device.id, LSPOpStatus.end_time > 'NOW')
        )

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'name'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        r = LSPOpStatus()
        r.deviceid = self.device.id
        r.begin_time = 'NOW'
        r.end_time = 'Infinity'
        for attr in obj.keys():
            setattr(r, attr, obj[attr])

        return r

    oid_name_map = {
            'mplsLspInfoState': 'state',
            'mplsLspInfoFrom': 'srcaddr',
            'mplsLspInfoTo': 'dstaddr',
    }

    def _build_objs(self):
        lsp_objs = {}

        for oid, entries in self.lsp_data.iteritems():
            k = self.oid_name_map[oid]
            for name, val in entries:
                name = name.split('.')[-1].replace("'", "")

                if not name in lsp_objs:
                    lsp_objs[name] = dict(name=name)

                o = lsp_objs[name]
                if oid == 'mplsLspInfoState':
                    o[k] = int(val)
                else:
                    o[k] = val

        return lsp_objs


class InfIfRefPollPersister(IfRefPollPersister):
    """Emulate a IfRef for an Infinera.

    This is a kludge, but it keeps other things relatively simple.

    ifAlias is called gigeClientCtpPmRealCktId
    ifSpeed and ifHighSpeed are apparently not available
    ipAdEntIfIndex doesn't make sense because this is not a layer3 device."""

    def store(self, result):
        keep = []
        result.data['ifAlias'] = []
        result.data['ifSpeed'] = []
        result.data['ifHighSpeed'] = []
        result.data['ipAdEntIfIndex'] = []

        ifalias = {}
        for k, v in result.data['gigeClientCtpPmRealCktId']:
            _, ifidx = k.split('.', 1)
            ifalias[ifidx] = v

        for k, v in result.data['ifDescr']:
            if v.startswith('GIGECLIENTCTP'):
                _, ifdescr = v.split('=', 1)
                keep.append((k, ifdescr))
                _, ifidx = k.split('.', 1)
                result.data['ifAlias'].append(
                            ('ifAlias.' + ifidx, ifalias.get(ifidx, '')))
                for x in ('ifSpeed', 'ifHighSpeed'):
                    result.data[x].append(
                            ('%s.%s' % (x, ifidx), 0))

        result.data['ifDescr'] = keep
        del result.data['gigeClientCtpPmRealCktId']

        IfRefPollPersister.store(self, result)


class PersistQueue(object):
    """Abstract base class for a persistence queue."""
    def __init__(self, qname):
        self.qname = qname

    def get(self, block=False):
        pass

    def put(self, val):
        pass

    def serialize(self, val):
        return pickle.dumps(val)  # json.encode(val)

    def deserialize(self, val):
        return pickle.loads(val)  # json.decode(val)


class MemcachedPersistQueue(PersistQueue):
    """A simple queue based on memcached.

    Inspired by:

    http://code.google.com/p/memcached/wiki/FAQ#Using_Memcached_as_a_simple_message_queue
    http://github.com/coderrr/memcache_queue/tree/master
    http://bitbucket.org/epoz/python-memcache-queue/overview/

    Code is very similar to python-memcache-queue but tailored to our needs.
    """

    PREFIX = '_mcpq_'

    def __init__(self, qname, memcached_uri):
        super(MemcachedPersistQueue, self).__init__(qname)

        self.log = get_logger("MemcachedPersistQueue_%s" % self.qname)

        self.mc = memcache.Client([memcached_uri])

        self.last_added = '%s_%s_last_added' % (self.PREFIX, self.qname)
        la = self.mc.get(self.last_added)
        if not la:
            self.mc.set(self.last_added, 0)

        self.last_read = '%s_%s_last_read' % (self.PREFIX, self.qname)
        lr = self.mc.get(self.last_read)
        if not lr:
            self.mc.set(self.last_read, 0)

    def __str__(self):
        la = self.mc.get(self.last_added)
        lr = self.mc.get(self.last_read)
        return '<MemcachedPersistQueue: %s last_added: %d, last_read: %d>' \
                % (self.qname, la, lr)

    def put(self, val):
        ser = self.serialize(val)
        if ser:
            qid = self.mc.incr(self.last_added)
            k = '%s_%s_%d' % (self.PREFIX, self.qname, qid)
            self.mc.set(k, ser)
        else:
            self.log.error("failed to serialize: %s" % str(val))

    def get(self, block=False):
        if len(self) <= 0:
            return None

        qid = self.mc.incr(self.last_read)
        k = '%s_%s_%d' % (self.PREFIX, self.qname, qid)
        val = self.mc.get(k)
        self.mc.delete(k)
        if val:
            return self.deserialize(val)
        else:
            self.log.error("failed to deserialize: got None")
            return None

    def __len__(self):
        n = self.mc.get(self.last_added) - self.mc.get(self.last_read)
        if n < 0:
            n = 0
        return n

    def reset(self):
        self.mc.set(self.last_added, 0)
        self.mc.set(self.last_read, 0)


class PersistClient(object):
    def __init__(self, config):
        self.config = config
        self.sinks = []
        self.log = get_logger("espersist.client")

        if not self.config.espoll_persist_uri:
            self.log.warning(
                "espoll_persist_uri not defined: all data will be discarded")
            return

        for uri in config.espoll_persist_uri:
            (kind, kind_uri) = uri.split(':', 1)
            sink = eval('%s(config, "%s")' % (kind, kind_uri))
            self.sinks.append(sink)

    def put(self, result):
        for sink in self.sinks:
            sink.put(result)


class MultiWorkerQueue(object):
    def __init__(self, qprefix, qtype, uri, num_workers):
        self.qprefix = qprefix
        self.qtype = qtype
        self.num_workers = num_workers
        self.cur_worker = 1
        self.queues = {}
        self.worker_map = {}
        self.log = get_logger('MultiWorkerQueue')

        for i in range(1, num_workers + 1):
            name = "%s_%d" % (qprefix, i)
            self.queues[name] = qtype(name, uri)

    def get_worker(self, result):
        k = ":".join((result.oidset_name, result.device_name))
        try:
            w = self.worker_map[k]
        except KeyError:
            w = self.cur_worker
            self.worker_map[k] = w
            self.cur_worker += 1
            self.log.debug("worker assigned: %s %d" % (k, w))

            if self.cur_worker > self.num_workers:
                self.cur_worker = 1

        return '%s_%d' % (self.qprefix, w)

    def put(self, result):
        workerqname = self.get_worker(result)
        workerq = self.queues[workerqname]
        workerq.put(result)


class MemcachedPersistHandler(object):
    def __init__(self, config, uri):
        self.queues = {}
        self.config = config
        self.uri = uri
        self.log = get_logger("MemcachedPersistHandler")

        for qname in config.persist_queues:
            num_workers = self.config.persist_queues[qname][1]
            if num_workers > 1:
                self.queues[qname] = MultiWorkerQueue(qname,
                        MemcachedPersistQueue, uri, num_workers)
            else:
                self.queues[qname] = MemcachedPersistQueue(qname, uri)

    def put(self, result):
        try:
            qnames = self.config.persist_map[result.oidset_name.lower()]
        except KeyError:
            self.log.error("unknown oidset: %s" % result.oidset_name)
            return

        for qname in qnames:
            try:
                q = self.queues[qname]
            except KeyError:
                self.log.error("unknown queue: %s" % (qname,))

            q.put(result)


def do_profile(func_name, myglobals, mylocals):
    import cProfile
    import pstats
    prof = cProfile.Profile()

    def print_stats(prof):
        stats = pstats.Stats(prof)
        #stats.sort_stats("time")  # Or cumulative
        #stats.print_stats()  # 80 = how many to print
        # The rest is optional.
        #stats.print_callees()
        #stats.print_callers()
        stats.dump_stats("/tmp/persists-profile.%d" % os.getpid())
    try:
        prof = prof.runctx(func_name, myglobals, mylocals)
    except Exception, e:
        print_stats(prof)
        raise e
    print_stats(prof)


class QueueStats:
    prefix = '_mcpq_'

    def __init__(self, mc, qname):
        self.mc = mc
        self.qname = qname
        self.last_read = [0, 0]
        self.last_added = [0, 0]
        self.warn = False

    def update_stats(self):
        for k in ('last_read', 'last_added'):
            kk = '%s_%s_%s' % (self.prefix, self.qname, k)
            v = self.mc.get(kk)
            l = getattr(self, k)
            if v:
                l.pop()
                l.insert(0, int(v))
            elif not self.warn:
                print >>sys.stderr, \
                        "warning: no stats, no work queue %s in memcache" \
                                % (self.qname, )
                self.warn = True
                break

    def get_stats(self):
        return (self.qname,
                self.last_added[0] - self.last_read[0],
                self.last_added[0] - self.last_added[1],
                self.last_read[0] - self.last_read[1],
                self.last_added[0])


def stats(name, config, opts):
    stats = {}
    mc = memcache.Client(['127.0.0.1:11211'])

    for qname, qinfo in config.persist_queues.iteritems():
        (qclass, nworkers) = qinfo
        if nworkers == 1:
                stats[qname] = QueueStats(mc, qname)
                stats[qname].update_stats()
        else:
            for i in range(1, nworkers + 1):
                k = "%s_%d" % (qname, i)
                stats[k] = QueueStats(mc, k)
                stats[k].update_stats()

    keys = stats.keys()
    keys.sort()
    while True:
        print "%10s %8s %8s %8s %8s" % (
                "queue", "pending", "new", "done", "max")
        for k in keys:
            stats[k].update_stats()
            print "%10s % 8d % 8d % 8d % 8d" % stats[k].get_stats()
        print ""
        time.sleep(15)


def worker(name, config, opts):
    if not opts.debug:
        exc_handler = setup_exc_handler(name, config)
        exc_handler.install()

    os.umask(0022)

    try:
        esxsnmp.sql.setup_db(config.db_uri)
    except Exception, e:
        self.log.error("Problem setting up database: %s" % e)
        raise

    init_logging(config.syslog_facility, level=config.syslog_priority,
            debug=opts.debug)

    (qclass, nworkers) = config.persist_queues[opts.qname]
    if nworkers > 1:
        name += '_%s' % opts.number
        opts.qname += '_%s' % opts.number

    setproctitle(name)
    klass = eval(qclass)
    worker = klass(config, opts.qname)

    worker.run()
    # do_profile("worker.run()", globals(), locals())


class PersistManager(object):
    def __init__(self, name, config, opts):
        self.name = name
        self.config = config
        self.opts = opts
        self.runing = False

        self.processes = {}

        init_logging(config.syslog_facility, level=config.syslog_priority,
            debug=opts.debug)

        self.log = get_logger(name)
        # save the location of the calling script for later use
        # (os.path.abspath uses current directory and daemonize does a cd /)
        self.caller_path = os.path.abspath(__main__.__file__)

        if not self.opts.debug:
            exc_handler = setup_exc_handler(name, config)
            exc_handler.install()

            daemonize(name, config.pid_dir,
                    log_stdout_stderr=config.syslog_facility)

        os.umask(0022)

        try:
            esxsnmp.sql.setup_db(config.db_uri)
        except Exception, e:
            self.log.error("Problem setting up database: %s" % e)
            raise

        setproctitle(name)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def start_all_children(self):
        for qname, qinfo in self.config.persist_queues.iteritems():
            (qclass, nworkers) = qinfo
            for i in range(1, nworkers + 1):
                self.start_child(qname, qclass, i)

    def start_child(self, qname, qclass, index):
        args = [sys.executable, self.caller_path,
                '-r', 'worker',
                '-q', qname,
                '-f', self.opts.config_file]

        if self.config.persist_queues[qname][1] > 1:
            args.extend(['-n', str(index)])

        p = Popen(args, stdout=PIPE, stderr=STDOUT)

        self.processes[p.pid] = (p, qname, qclass, index)

    def run(self):
        self.log.info("starting")
        self.running = True

        self.start_all_children()

        while self.running:
            try:
                pid, status = os.wait()
            except OSError, e:
                if e.errno == errno.EINTR:
                    continue
                else:
                    raise

            p, qname, qclass, index = self.processes[pid]
            del self.processes[pid]
            self.log.error("child died: pid %d, %s_%d" % (pid, qname, index))
            for line in p.stdout.readlines():
                self.log.error("pid %d: %s" % (pid, line))

            self.start_child(qname, qclass, index)

        for pid, pinfo in self.processes.iteritems():
            p, qname, qclass, index = pinfo
            self.log.info("killing pid %d: %s_%d" % (pid, qname, index))

            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)

        self.log.info("exiting")

    def stop(self, x, y):
        self.log.info("stopping")
        self.running = False


def espersistd():
    """Entry point for espersistd.

    espersistd consists of one PersistenceManager thread and multiple
    worker sub-processes.

    """
    argv = sys.argv
    oparse = get_opt_parser(default_config_file=get_config_path())
    oparse.add_option("-r", "--role", dest="role", default="manager")
    oparse.add_option("-q", "--queue", dest="qname", default="")
    oparse.add_option("-n", "--number", dest="number", default="")
    (opts, args) = oparse.parse_args(args=argv)

    opts.config_file = os.path.abspath(opts.config_file)

    try:
        config = get_config(opts.config_file, opts)
    except ConfigError, e:
        print >>sys.stderr, e
        sys.exit(1)

    name = "espersistd.%s" % opts.role

    if opts.qname:
        name += ".%s" % opts.qname

    log = get_logger(name)

    if opts.role == 'manager':
        try:
            PersistManager(name, config, opts).run()
        except Exception, e:
            log.error("Problem with manager module: %s" % e)
            sys.exit(1)
    elif opts.role == 'worker':
        worker(name, config, opts)
        sys.exit(0)
        try:
            worker(name, config, opts)
        except Exception, e:
            log.error("Problem with worker module: %s" % e, exc_info=True)
            raise e
            sys.exit(1)
    elif opts.role == 'stats':
        stats(name, config, opts)
    else:
        print >>sys.stderr, "unknown role: %s" % opts.role
