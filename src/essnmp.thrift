#!/usr/local/bin/thrift -cpp -py -perl -r

cpp_namespace ESxSNMP
perl_package ESxSNMP

struct OIDType {
    1: i32 id,
    2: string name
}

struct OIDCorrelator {
    1: i32 id,
    2: string name
}

struct OID {
    1: i32 id,
    2: string name,
    4: i32 oidtypeid
}

struct Poller {
    1: i32 id,
    2: string name
}

struct OIDSet {
    1: i32 id,
    2: string name,
    3: i32 frequency,
    4: list<OID> oids,
    5: i32 pollerid
}

struct DeviceTag {
    1: i32 id,
    2: string name
}

struct Device {
    1: i32 id,
    2: string name,
    3: i32 begin_time,
    4: i32 end_time,
    5: string community,
    6: list<OIDSet> oidsets
}

struct IfRef {
    1: i32 id,
    2: Device device,
    3: i32 ifindex,
    4: string ifdescr,
    5: string ifalias,
    6: string ipaddr,
    7: i32 ifspeed,
    8: i32 ifhighspeed,
    9: string connection,
    10: string conntype,
    11: string usage,
    12: string visibility,
    13: string grouping
    /*
    14: string begin_time,
    15: string end_time,
    */
}

enum Grouping {
    Commercial = 1,
    Internal = 2,
    ResearchEducation = 3,
    Education = 4,
    Site = 5
}

struct Counter32 {
    1: i32 flags,
    2: i32 timestamp,
    3: i32 value,
    4: byte version = 1,
    5: byte type_id = 1
}

struct Counter64 {
    1: i32 flags,
    2: i32 timestamp,
    3: i64 value,
    4: byte version = 1,
    5: byte type_id = 2
}

struct Gauge32 {
    1: i32 flags,
    2: i32 timestamp,
    3: i32 value,
    4: byte version = 1,
    5: byte type_id = 3
}

struct VarList {
    1: list<Counter32> counter32,
    2: list<Counter64> counter64,
    3: list<Gauge32> gauge32
}

struct SNMPPollResultPair {
    1: string OIDName,
    2: string value
}

struct SNMPPollResult {
    1: i32 device_id,
    2: i32 oidset_id,
    3: i32 timestamp,
    4: list<list<string>> vars
}

struct Rate {
    1: i32 timestamp,
    2: double rate
}

exception ESDBError {
    1: string what
}

service ESDB {
    list<string> list_devices(1: bool active),
    Device get_device(1: string name),
    map<string, Device> get_all_devices(1: bool active),
    void add_device(1: string name, 2: string begin_time, 3: string end_time),
    void update_device(1: string name, 2: string begin_time, 3: string end_time),
    list<OIDSet> list_device_oidsets(1: Device device),

    list<string> list_oids(),
    OID get_oid(1: string name),
    void add_oid(1: string name, 2: string storage, 3: string oidtype),
    
    list<string> list_oidsets(),
    OIDSet get_oidset(1: string name),
    list<Device> get_oidset_devices(1: OIDSet oidset),

    VarList get_vars_by_grouping(1: Grouping grouping), // should grouping e
#    void insert_counter32(list<Var> vars, list<Counter32> values),
#    void insert_counter64(list<Var> vars, list<Counter64> values),
#    void insert_gauge32(list<Var> vars, list<Gauge32> values),
    byte store_poll_result(SNMPPollResult result),

    VarList select(1: string device, 2: string iface_name, 3: string oidset, 4: string oid, 5: string begin_time, 6: string end_time, 7: string flags, 8: string cf, 9: string resolution) throws (1: ESDBError error),

    #
    # get interfaces for device, limit to those with a description of
    # has_descr is True
    #
    list<IfRef> get_interfaces(1: string device, 2: bool has_descr)
}
