#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import re
import logging
import ipaddress
import subprocess
import pyroute2
from wrt_util import WrtUtil


class WrtTrafficManager:

    def __init__(self, param):
        self.param = param
        self.cfgFile = os.path.join(self.param.tmpDir, "l2-dnsmasq.conf")
        self.pidFile = os.path.join(self.param.tmpDir, "l2-dnsmasq.pid")

        self.ownerDict = dict()

        self.sourceIpDict = dict()      # dict<source-id, dict<ip, (original-ip, nat-ip)>>
        self.freeIpSet = None

        self.dnsPort = None
        self.dnsmasqProc = None
        try:
            self._runDnsmasq()
            logging.info("Level 2 nameserver started.")
        except BaseException:
            self._stopDnsmasq()
            raise
        logging.info("Started.")

    def dispose(self):
        self._stopDnsmasq()
        logging.info("Terminated.")

    def get_l2_nameserver_port(self):
        return self.dnsPort

    def set_data(self, owner, data):
        self.ownerDict[owner] = data

    def delete_data(self, owner):
        del self.ownerDict[owner]

    def on_wconn_up(self):
        WrtUtil.shell('/sbin/nft add rule wrtd natpost oifname %s masquerade' % (self.wanConnPlugin.get_interface()))
        # WrtUtil.shell('/sbin/nft add rule wrtd fw iifname %s ct state established,related accept' % (intf))
        # WrtUtil.shell('/sbin/nft add rule wrtd fw iifname %s ip protocol icmp accept' % (intf))
        # WrtUtil.shell('/sbin/nft add rule wrtd fw iifname %s drop' % (intf))

    def on_wconn_down(self):
        pass

    def on_client_add_or_change(self, source_id, ip_data_dict):
        if source_id not in self.sourceIpDict:
            self.sourceIpDict[source_id] = dict()

        for ip, data in ip_data_dict:
            if ip in self.sourceIpDict[source_id]:
                if self.freeIpSet is not None:
                    self._natDestroy(source_id, ip)
                    self.freeIpSet.add(self.sourceIpDict[source_id][ip][1])
                del self.sourceIpDict[source_id][ip]

            if "nat-ip" in data:
                self.sourceIpDict[source_id][ip] = (data["nat-ip"], None)
            else:
                self.sourceIpDict[source_id][ip] = (ip, None)

            if self.freeIpSet is not None:
                self.sourceIpDict[source_id][ip] = (self.sourceIpDict[source_id][ip][0], self.freeIpSet.pop())
                self._natCreate(source_id, ip)

    def on_client_remove(self, source_id, ip_list):
        for ip in ip_list:
            if self.freeIpSet is not None:
                self._natDestroy(source_id, ip)
                self.freeIpSet.add(self.sourceIpDict[source_id][ip][1])
            del self.sourceIpDict[source_id][ip]

        if len(self.sourceIpDict[source_id]) == 0:
            del self.sourceIpDict[source_id]

    def on_cascade_upstream_up(self, data):
        # create self.freeIpSet
        self.freeIpSet = set()
        ip1 = ipaddress.IPv4Address(data["subhost-start"])
        ip2 = ipaddress.IPv4Address(data["subhost-end"])
        if ip1 > ip2:
            raise Exception("invalid subhost IP range, %s~%s" % (data["subhost-start"], data["subhost-end"]))
        while ip1 != ip2:
            self.freeIpSet.add(ip1)
            ip1 = ip1 + 1
        self.freeIpSet.add(ip1)

        # fill self.sourceIpDict
        for source_id, ipDict in self.sourceIpDict.items():
            for ip in ipDict.keys():
                ipDict[ip] = (ipDict[ip][0], self.freeIpSet.pop())
                self._natCreate(source_id, ip)

    def on_cascade_upstream_down(self):
        for source_id, ipDict in self.sourceIpDict.items():
            for ip in ipDict.keys():
                ipDict[ip] = (ipDict[ip][0], None)
        self.freeIpSet = None
        # nftables rules and sub-interfaces would be auto-deleted when vpn-interface is removed

    def on_cascade_downstream_new_router(self, data):
        self.on_cascade_downstream_new_or_update_router_client(data)

    def on_cascade_downstream_delete_router(self, data):
        for router_id in data:
            ip_list = list(self.sourceIpDict[router_id].keys())
            self.on_client_remove("downstream-%s" % (router_id), ip_list)

    def on_cascade_downstream_new_or_update_router_client(self, data):
        for router_id, info in data:
            self.on_client_add_or_change("downstream-%s" % (router_id), info["client-list"])

    def on_cascade_downstream_delete_router_client(self, data):
        for router_id, info in data:
            self.on_client_remove("downstream-%s" % (router_id), info["client-list"])

    def _natCreate(self, source_id, ip):
        oriIp, natIp = self.sourceIpDict[source_id][ip]
        intf = self.param.wanManager.vpnPlugin.get_interface()
        with pyroute2.IPRoute() as ipp:
            idx = ip.link_lookup(ifname=intf)[0]
            ipp.addr("add", index=idx, address=natIp)
            WrtUtil.shell('/sbin/nft add rule wrtd natpre ip daddr %s iif %s dnat %s' % (natIp, intf, oriIp))
            WrtUtil.shell('/sbin/nft add rule wrtd natpost ip saddr %s oif %s snat %s' % (oriIp, intf, natIp))

    def _natDestroy(self, source_id, ip):
        oriIp, natIp = self.sourceIpDict[source_id][ip]
        intf = self.param.wanManager.vpnPlugin.get_interface()
        with pyroute2.IPRoute() as ipp:
            self.__removeNftRuleSubHost(intf, oriIp, natIp)
            idx = ipp.link_lookup(ifname=intf)[0]
            ipp.addr("delete", index=idx, address=natIp)

    def __removeNftRuleSubHost(self, intf, subHostIp, natIp):
        rc, msg = WrtUtil.shell('/sbin/nft list table ip wrtd -a', "retcode+stdout")
        if rc != 0:
            return
        m = re.search("\\s*ip daddr %s iif \"%s\" dnat to %s # handle ([0-9]+)" % (natIp, intf, subHostIp), msg, re.M)
        if m is not None:
            WrtUtil.shell("/sbin/nft delete rule wrtd natpre handle %s" % (m.group(1)))
        m = re.search("\\s*ip saddr %s oif \"%s\" snat to %s # handle ([0-9]+)" % (subHostIp, intf, natIp), msg, re.M)
        if m is not None:
            WrtUtil.shell("/sbin/nft delete rule wrtd natpost handle %s" % (m.group(1)))

    def _runDnsmasq(self):
        self.dnsPort = WrtUtil.getFreeSocketPort("tcp")

        # generate dnsmasq config file
        buf = ""
        buf += "strict-order\n"
        buf += "bind-interfaces\n"                            # don't listen on 0.0.0.0
        buf += "interface=lo\n"
        buf += "user=root\n"
        buf += "group=root\n"
        buf += "\n"
        buf += "domain-needed\n"
        buf += "bogus-priv\n"
        buf += "no-hosts\n"
        buf += "resolv-file=%s\n" % (self.param.ownResolvConf)
        buf += "\n"
        with open(self.cfgFile, "w") as f:
            f.write(buf)

        # run dnsmasq process
        cmd = "/usr/sbin/dnsmasq"
        cmd += " --keep-in-foreground"
        cmd += " --port=%d" % (self.dnsPort)
        cmd += " --conf-file=\"%s\"" % (self.cfgFile)
        cmd += " --pid-file=%s" % (self.pidFile)
        self.dnsmasqProc = subprocess.Popen(cmd, shell=True, universal_newlines=True)

    def _stopDnsmasq(self):
        if self.dnsmasqProc is not None:
            self.dnsmasqProc.terminate()
            self.dnsmasqProc.wait()
            self.dnsmasqProc = None
        WrtUtil.forceDelete(self.pidFile)
        WrtUtil.forceDelete(self.cfgFile)
