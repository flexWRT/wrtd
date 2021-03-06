#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import re
import json
import dbus
import dbus.service
import logging
import socket
import ipaddress
from wrt_util import WrtUtil
from wrt_common import WrtCommon


################################################################################
# DBus API Docs
################################################################################
#
# ==== Main Application ====
# Service               org.fpemud.WRT
# Interface             org.fpemud.WRT
# Object path           /
#
# Methods:
#   info:json                                                GetRouterInfo()
#   void                                                     AddWanService(name:str, service:json)
#   void                                                     RemoveWanService(name:str)
#   void                                                     AddTrafficFacilityGroup(name:str, priority:int, tfac_group:json)
#   void                                                     ChangeTrafficFacilityGroup(name:str, tfac_group:json)
#   void                                                     RemoveTrafficFacilityGroup(name:str)

class DbusMainObject(dbus.service.Object):

    def __init__(self, param):
        self.param = param
        self.logger = logging.getLogger(self.__module__ + "." + self.__class__.__name__)

        self.wanServOwnerDict = dict()          # dict<wan-service-name,owner>
        self.tfacGroupOwnerDict = dict()        # dict<tfac-group-name,owner>

        # register dbus object path
        bus_name = dbus.service.BusName('org.fpemud.WRT', bus=dbus.SystemBus())
        dbus.service.Object.__init__(self, bus_name, '/org/fpemud/WRT')

        # for handling client process termination
        self.handle = dbus.SystemBus().add_signal_receiver(self.onNameOwnerChanged, 'NameOwnerChanged', None, None)

    def release(self):
        dbus.SystemBus().remove_signal_receiver(self.handle)
        self.remove_from_connection()

    def onNameOwnerChanged(self, name, old, new):
        # focus on name deletion, filter other circumstance
        if not name.startswith(":") or new != "":
            return
        assert name == old

        # remove wan services
        snamelist = []
        for sname, owner in self.wanServOwnerDict.items():
            if owner == name:
                snamelist.append(sname)
        for sname in snamelist:
            self.param.trafficManager.remove_wan_service(sname)
            self.logger.info("WAN service \"%s\" is removed due to owner disappear." % (sname))

        # remove traffic facility groups
        snamelist = []
        for sname, owner in self.tfacGroupOwnerDict.items():
            if owner == name:
                snamelist.append(sname)
        for sname in snamelist:
            self.param.trafficManager.remove_tfac_group(sname)
            self.logger.info("Traffic facility group \"%s\" is removed due to owner disappear." % (sname))

    @dbus.service.method('org.fpemud.WRT', in_signature='', out_signature='s')
    def GetRouterInfo(self):
        ret = dict()

        ret["uuid"] = self.param.uuid
        ret["hostname"] = socket.gethostname()

        if self.param.wanManager.wanConnPlugin is not None:
            plugin = self.param.wanManager.wanConnPlugin
            ret["wconn-plugin"] = dict()
            ret["wconn-plugin"]["name"] = plugin.full_name
            if plugin.is_connected():
                ret["wconn-plugin"]["is-connected"] = True
                ret["wconn-plugin"]["ip"] = plugin.get_ip()
                ret["wconn-plugin"]["is-ip-public"] = self.param.wanManager.wanConnIpIsPublic
            else:
                ret["wconn-plugin"]["is-connected"] = False

        ret["default-bridge"] = dict()
        if True:
            ret["default-bridge"] = dict()
            ret["default-bridge"]["ip"] = WrtCommon.bridgeGetIp(self.param.lanManager.defaultBridge)
            ret["default-bridge"]["mask"] = self.param.lanManager.defaultBridge.get_prefix()[1]

        ret["lif-plugin"] = dict()
        for plugin in self.param.lanManager.lifPluginList:
            ret["lif-plugin"][plugin.full_name] = dict()

        ret["vpns-plugin"] = dict()
        for plugin in self.param.lanManager.vpnsPluginList:
            ret["vpns-plugin"][plugin.full_name] = dict()
            ret["vpns-plugin"][plugin.full_name]["bridge"] = dict()
            ret["vpns-plugin"][plugin.full_name]["bridge"]["name"] = plugin.get_bridge().get_name()
            ret["vpns-plugin"][plugin.full_name]["bridge"]["ip"] = WrtCommon.bridgeGetIp(plugin.get_bridge())
            ret["vpns-plugin"][plugin.full_name]["bridge"]["mask"] = plugin.get_bridge().get_prefix()[1]

        ret["wan-service"] = []
        if True:
            ret["wan-service"] = list(self.param.trafficManager.wanServDict.keys())

        ret["tfac-group"] = dict()
        if True:
            for name, priority in self.param.trafficManager.tfacGroupDict.items():
                ret["tfac-group"][name] = dict()
                ret["tfac-group"][name]["priority"] = priority

        for p in self.param.daemon.managerPluginDict.values():
            ret.update(p.get_router_info())

        return json.dumps(ret)

    @dbus.service.method('org.fpemud.WRT', sender_keyword='sender', in_signature='ss')
    def AddWanService(self, name, service, sender=None):
        if self.param.trafficManager.has_wan_service(name):
            raise Exception("WAN service \"%s\" already exists." % (name))

        self.param.trafficManager.add_wan_service(name, json.loads(service))
        self.wanServOwnerDict[name] = sender
        self.logger.info("WAN service \"%s\" added by %s." % (name, sender))

    @dbus.service.method('org.fpemud.WRT', in_signature='s')
    def RemoveWanService(self, name):
        self.param.trafficManager.remove_wan_service(name)
        del self.wanServOwnerDict[name]
        self.logger.info("WAN service \"%s\" removed." % (name))

    @dbus.service.method('org.fpemud.WRT', sender_keyword='sender', in_signature='sis')
    def AddTrafficFacilityGroup(self, name, priority, tfac_group, sender=None):
        if self.param.trafficManager.has_tfac_group(name):
            raise TfacException("Traffic facility grouop \"%s\" already exists." % (name))
        tfac_group = json.loads(tfac_group)
        checkTrafficFacilityGroup(tfac_group)

        self.param.trafficManager.add_tfac_group(name, priority, tfac_group)
        self.tfacGroupOwnerDict[name] = sender
        self.logger.info("Traffic facility group \"%s\" added by %s." % (name, sender))

    @dbus.service.method('org.fpemud.WRT', in_signature='ss')
    def ChangeTrafficFacilityGroup(self, name, tfac_group):
        if not self.param.trafficManager.has_tfac_group(name):
            raise TfacException("Traffic facility group \"%s\" does not exist." % (name))
        tfac_group = json.loads(tfac_group)
        checkTrafficFacilityGroup(tfac_group)

        self.param.trafficManager.change_tfac_group(name, tfac_group)
        self.logger.info("Traffic facility group \"%s\" changed." % (name))

    @dbus.service.method('org.fpemud.WRT', in_signature='s')
    def RemoveTrafficFacilityGroup(self, name):
        if not self.param.trafficManager.has_tfac_group(name):
            raise TfacException("Traffic facility group \"%s\" does not exist." % (name))

        self.param.trafficManager.remove_tfac_group(name)
        del self.tfacGroupOwnerDict[name]
        self.logger.info("Traffic facility group \"%s\" removed." % (name))


################################################################################
# DBus API Docs
################################################################################
#
# ==== Main Application ====
# Service               org.fpemud.IpForward
# Interface             org.fpemud.IpForward
# Object path           /
#
# Methods:
# void                  On()
# void                  Off()
#

class DbusIpForwardObject(dbus.service.Object):

    def __init__(self, param):
        # implement a fake IpForward object, since we always set ip_forward to 1
        bus_name = dbus.service.BusName('org.fpemud.IpForward', bus=dbus.SystemBus())
        dbus.service.Object.__init__(self, bus_name, '/org/fpemud/IpForward')

    def release(self):
        self.remove_from_connection()

    @dbus.service.method('org.fpemud.IpForward')
    def On(self):
        pass

    @dbus.service.method('org.fpemud.IpForward')
    def Off(self):
        pass


class TfacException(Exception):
    pass


def checkTrafficFacilityGroup(tfac_group):
    i = 0
    for tfac in tfac_group:
        i += 1

        if "facility-name" not in tfac:
            raise TfacException("Lacking \"facility-name\" for facility No.%d." % (i))

        if "facility-type" not in tfac:
            raise TfacException("Lacking \"facility-type\" for facility \"%s\"." % (tfac["facility-name"]))

        if tfac["facility-type"] == "nameserver":
            if "target" not in tfac:
                raise TfacException("Lacking \"target\" for facility \"%s\"." % (tfac["facility-name"]))
            if not isinstance(tfac["target"], list):
                raise TfacException("Type of \"target\" is invalid for facility \"%s\"." % (tfac["facility-name"]))
            for item in tfac["target"]:
                msg = "Some element in \"target\" is invalid for facility \"%s\"." % (tfac["facility-name"])
                if ":" in item:
                    tlist = item.split(":")
                    if len(tlist) != 2:
                        raise TfacException(msg)
                    if not WrtUtil.is_int(tlist[1]):
                        raise TfacException(msg)

            if "domain-list" not in tfac:
                raise TfacException("Lacking \"domain-list\" for facility \"%s\"." % (tfac["facility-name"]))
            if not isinstance(tfac["domain-list"], list):
                raise TfacException("Type of \"domain-list\" is invalid for facility \"%s\"." % (tfac["facility-name"]))
            for item in tfac["domain-list"]:
                if not isinstance(item, str):
                    raise TfacException("Some element in \"domain-list\" is invalid for facility \"%s\"." % (tfac["facility-name"]))

            continue

        if tfac["facility-type"] == "gateway":
            if "target" not in tfac:
                raise TfacException("Lacking \"target\" for facility \"%s\"." % (tfac["facility-name"]))
            msg = "Invalid \"target\" for facility \"%s\"." % (tfac["facility-name"])
            if not isinstance(tfac["target"], list):
                raise TfacException(msg)
            if len(tfac["target"]) != 2:
                raise TfacException(msg)
            if tfac["target"][0] is not None and not isinstance(tfac["target"][0], str):
                raise TfacException(msg)
            if tfac["target"][1] is not None and not isinstance(tfac["target"][1], str):
                raise TfacException(msg)

            if "network-list" not in tfac:
                raise TfacException("Lacking \"network-list\" for facility \"%s\"." % (tfac["facility-name"]))
            if not isinstance(tfac["network-list"], list):
                raise TfacException("Type of \"network-list\" is invalid for facility \"%s\"." % (tfac["facility-name"]))
            for item in tfac["network-list"]:
                msg = "Some element in \"domain-list\" is invalid for facility \"%s\"." % (tfac["facility-name"])
                if not isinstance(item, str):
                    raise TfacException(msg)
                try:
                    tnet = ipaddress.IPv4Network(item)
                    if tnet.is_private:
                        raise TfacException(msg)
                    if re.match("[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+", item.split("/")[0]) is None:
                        raise TfacException(msg)
                except ipaddress.AddressValueError:
                    raise TfacException(msg)
                except ipaddress.NetmaskValueError:
                    raise TfacException(msg)
                except ValueError:
                    raise TfacException(msg)

            continue

        raise TfacException("Invalid \"facility-type\" for facility \"%s\"." % (tfac["facility-name"]))
