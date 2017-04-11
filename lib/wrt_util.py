#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import re
import sys
import socket
import shutil
import ipaddress
import logging
import ctypes
import errno
import subprocess


class WrtUtil:

    @staticmethod
    def readDnsmasqHostFile(filename):
        """dnsmasq host file has the following format:
            1.1.1.1 myname
            ^       ^
            IP      hostname

           This function returns [(ip,hostname), (ip,hostname)]
        """
        ret = []
        with open(filename, "r") as f:
            for line in f.read().split("\n"):
                if line.startswith("#") or line.strip() == "":
                    continue
                t = line.split(" ")
                ret.append((t[0], t[1]))
        return ret

    @staticmethod
    def writeDnsmasqHostFile(filename, itemList):
        with open(filename, "w") as f:
            for item in itemList:
                f.write(item[0] + " " + item[1] + "\n")

    @staticmethod
    def recvUntilEof(sock):
        buf = bytes()
        while True:
            buf2 = sock.recv(4096)
            if len(buf2) == 0:
                break
            buf += buf2
        return buf

    @staticmethod
    def recvLine(sock):
        buf = bytes()
        while True:
            buf2 = sock.recv(1)
            if len(buf2) == 0 or buf2 == b'\n':
                break
            buf += buf2
        return buf

    @staticmethod
    def getLoggingLevel(logLevel):
        if logLevel == "CRITICAL":
            return logging.CRITICAL
        elif logLevel == "ERROR":
            return logging.ERROR
        elif logLevel == "WARNING":
            return logging.WARNING
        elif logLevel == "INFO":
            return logging.INFO
        elif logLevel == "DEBUG":
            return logging.DEBUG
        else:
            assert False

    @staticmethod
    def forceDelete(filename):
        if os.path.islink(filename):
            os.remove(filename)
        elif os.path.isfile(filename):
            os.remove(filename)
        elif os.path.isdir(filename):
            shutil.rmtree(filename)

    @staticmethod
    def mkDirAndClear(dirname):
        WrtUtil.forceDelete(dirname)
        os.mkdir(dirname)

    @staticmethod
    def shell(cmd, flags=""):
        """Execute shell command"""

        assert cmd.startswith("/")

        # Execute shell command, throws exception when failed
        if flags == "":
            retcode = subprocess.Popen(cmd, shell=True, universal_newlines=True).wait()
            if retcode != 0:
                raise Exception("Executing shell command \"%s\" failed, return code %d" % (cmd, retcode))
            return

        # Execute shell command, throws exception when failed, returns stdout+stderr
        if flags == "stdout":
            proc = subprocess.Popen(cmd,
                                    shell=True, universal_newlines=True,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out = proc.communicate()[0]
            if proc.returncode != 0:
                raise Exception("Executing shell command \"%s\" failed, return code %d, output %s" % (cmd, proc.returncode, out))
            return out

        # Execute shell command, returns (returncode,stdout+stderr)
        if flags == "retcode+stdout":
            proc = subprocess.Popen(cmd,
                                    shell=True, universal_newlines=True,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out = proc.communicate()[0]
            return (proc.returncode, out)

        assert False

    @staticmethod
    def ensureDir(dirname):
        if not os.path.exists(dirname):
            os.makedirs(dirname)

    @staticmethod
    def interfaceExists(intfName):
        ret = WrtUtil.shell("/bin/ifconfig", "stdout")
        return re.search("^%s: " % (intfName), ret, re.M) is not None

    @staticmethod
    def getGatewayInterface():
        ret = WrtUtil.shell("/bin/route -n4", "stdout")
        # syntax: DestIp GatewayIp DestMask ... OutIntf
        m = re.search("^(0\\.0\\.0\\.0)\\s+([0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+)\\s+(0\\.0\\.0\\.0)\\s+.*\\s+(\\S+)$", ret, re.M)
        if m is None:
            return None
        return m.group(4)

    @staticmethod
    def getGatewayNexthop():
        ret = WrtUtil.shell("/bin/route -n4", "stdout")
        # syntax: DestIp GatewayIp DestMask ... OutIntf
        m = re.search("^(0\\.0\\.0\\.0)\\s+([0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+)\\s+(0\\.0\\.0\\.0)\\s+.*\\s+(\\S+)$", ret, re.M)
        if m is None:
            return None
        return m.group(2)

    @staticmethod
    def ipMaskToLen(mask):
        """255.255.255.0 -> 24"""

        netmask = 0
        netmasks = mask.split('.')
        for i in range(0, len(netmasks)):
            netmask *= 256
            netmask += int(netmasks[i])
        return 32 - (netmask ^ 0xFFFFFFFF).bit_length()

    @staticmethod
    def nftAddRule(table, chain, rule):
        """WARN: rule argument must use **standard** format, or you are not able to find the handle number"""

        # add rule
        WrtUtil.shell('/sbin/nft add rule %s %s %s' % (table, chain, rule))

        # obtain and return rule handle number
        msg = WrtUtil.shell("/sbin/nft list table %s -a" % (table), "stdout")
        mlist = list(re.finditer("^\\s+%s # handle ([0-9]+)$" % (rule), msg, re.M))
        assert len(mlist) == 1
        return int(mlist[0].group(1))

    @staticmethod
    def nftDeleteRule(table, chain, ruleHandle):
        WrtUtil.shell('/sbin/nft delete rule %s %s handle %d' % (table, chain, ruleHandle))

    @staticmethod
    def getFreeSocketPort(portType):
        if portType == "tcp":
            stlist = [socket.SOCK_STREAM]
        elif portType == "udp":
            stlist = [socket.SOCK_DGRAM]
        elif portType == "tcp+udp":
            stlist = [socket.SOCK_STREAM, socket.SOCK_DGRAM]
        else:
            assert False

        for port in range(10000, 65536):
            bFound = True
            for sType in stlist:
                s = socket.socket(socket.AF_INET, sType)
                try:
                    s.bind((('', port)))
                except socket.error:
                    bFound = False
                finally:
                    s.close()
            if bFound:
                return port

        raise Exception("no valid port")

    def ip2ipar(ip):
        AF_INET = 2
        # AF_INET6 = 10
        el = ip.split(".")
        assert len(el) == 4
        return (AF_INET, [bytes([int(x)]) for x in el])

    @staticmethod
    def getReservedIpv4NetworkList():
        return [
            ipaddress.IPv4Network("0.0.0.0/8"),
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("100.64.0.0/10"),
            ipaddress.IPv4Network("127.0.0.0/8"),
            ipaddress.IPv4Network("169.254.0.0/16"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.0.0.0/24"),
            ipaddress.IPv4Network("192.0.2.0/24"),
            ipaddress.IPv4Network("192.88.99.0/24"),
            ipaddress.IPv4Network("192.168.0.0/16"),
            ipaddress.IPv4Network("198.18.0.0/15"),
            ipaddress.IPv4Network("198.51.100.0/24"),
            ipaddress.IPv4Network("203.0.113.0/24"),
            ipaddress.IPv4Network("224.0.0.0/4"),
            ipaddress.IPv4Network("240.0.0.0/4"),
            ipaddress.IPv4Network("255.255.255.255/32"),
        ]

    @staticmethod
    def substractIpv4Network(ipv4Network, ipv4NetworkList):
        netlist = [ipv4Network]
        for n in ipv4NetworkList:
            tlist = []
            for n2 in netlist:
                if not n2.overlaps(n):
                    tlist.append(n2)                                # no need to substract
                    continue
                try:
                    tlist += list(n2.address_exclude(n))            # successful to substract
                except:
                    pass                                            # substract to none
            netlist = tlist
        return netlist

    @staticmethod
    def readDnsmasqLeaseFile(filename):
        """dnsmasq leases file has the following format:
             1108086503   00:b0:d0:01:32:86 142.174.150.208 M61480    01:00:b0:d0:01:32:86
             ^            ^                 ^               ^         ^
             Expiry time  MAC address       IP address      hostname  Client-id

           This function returns [(mac,ip,hostname), (mac,ip,hostname)]
        """

        pattern = "[0-9]+ +([0-9a-f:]+) +([0-9\.]+) +(\\S+) +\\S+"
        ret = []
        with open(filename, "r") as f:
            for line in f.read().split("\n"):
                m = re.match(pattern, line)
                if m is None:
                    continue
                if m.group(3) == "*":
                    item = (m.group(1), m.group(2), "")
                else:
                    item = (m.group(1), m.group(2), m.group(3))
                ret.append(item)
        return ret


class StdoutRedirector:

    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()


class NewMountNamespace:

    _CLONE_NEWNS = 0x00020000               # <linux/sched.h>
    _MS_REC = 16384                         # <sys/mount.h>
    _MS_PRIVATE = 1 << 18                   # <sys/mount.h>
    _libc = None
    _mount = None
    _setns = None
    _unshare = None

    def __init__(self):
        if self._libc is None:
            self._libc = ctypes.CDLL('libc.so.6', use_errno=True)
            self._mount = self._libc.mount
            self._mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
            self._mount.restype = ctypes.c_int
            self._setns = self._libc.setns
            self._unshare = self._libc.unshare

        self.parentfd = None

    def __enter__(self):
        self.parentfd = open("/proc/%d/ns/mnt" % (os.getpid()), 'r')

        # copied from unshare.c of util-linux
        try:
            if self._unshare(self._CLONE_NEWNS) != 0:
                e = ctypes.get_errno()
                raise OSError(e, errno.errorcode[e])

            srcdir = ctypes.c_char_p("none".encode("utf_8"))
            target = ctypes.c_char_p("/".encode("utf_8"))
            if self._mount(srcdir, target, None, (self._MS_REC | self._MS_PRIVATE), None) != 0:
                e = ctypes.get_errno()
                raise OSError(e, errno.errorcode[e])
        except:
            self.parentfd.close()
            self.parentfd = None
            raise

    def __exit__(self, *_):
        self._setns(self.parentfd.fileno(), 0)
        self.parentfd.close()
        self.parentfd = None


# class HostService(threading.Thread):

#     def __init__(self, ip="0.0.0.0", port=2300):
#         threading.Thread.__init__(self)

#         self.mainloop = GLib.MainLoop()

#         self.serverSock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         self.serverSock.bind((ip, port))
#         self.serverSock.listen(5)
#         self.serverSock.setblocking(0)
#         self.serverSourceId = GLib.io_add_watch(self.serverSock, GLib.IO_IN | _flagError, self._onServerAccept)

#         self.threadSet = set()
#         self.threadSetLock = threading.Lock()

#         self.remoteIp = remoteIp

#     def stop(self):
#         self.mainloop.quit()
#         self.join()
#         GLib.source_remove(self.serverSourceId)
#         self.serverSock.close()

#     def _onServerAccept(self, source, cb_condition):
#         assert not (cb_condition & _flagError)

#         try:
#             new_sock, addr = source.accept()
#             with self.threadSetLock:
#                 th = _SubHostProcessThread(self, new_sock)
#                 self.threadSet.add(th)
#                 th.start()
#             return True
#         except socket.error as e:
#             logging.debug("_SubHostListener._onServerAccept: Failed, %s, %s", e.__class__, e)
#             return True


# class _SubHostProcessThread(threading.Thread):

#     def __init__(self, pObj, sock):
#         threading.Thread.__init__(self)
#         self.param = pObj.param
#         self.pObj = pObj
#         self.sock = sock

#     def run(self):
#         fname = os.path.join(self.param.tmpDir, "hosts.d", "hosts.vpn")
#         try:
#             buf = WrtUtil.recvUntilEof(self.sock).decode("utf-8")
#             itemList = self._jsonObj2ItemList(json.loads(buf))
#             WrtUtil.writeDnsmasqHostFile(fname, itemList)
#             WrtCommon.syncToEtcHosts(self.param.tmpDir)
#         finally:
#             with self.pObj.threadSetLock:
#                 self.pObj.threadSet.remove(self)
#             self.sock.close()

#     def _jsonObj2ItemList(self, jsonObj):
#         itemList = []
#         for host in jsonObj:
#             itemList.append((host["ip"], host["hostname"]))
#         return itemList


# _flagError = GLib.IO_PRI | GLib.IO_ERR | GLib.IO_HUP | GLib.IO_NVAL

# @staticmethod
# def getSubInterfaceByIp(ifname, ipaddr):
#     for n in netifaces.interfaces():
#         if re.match("%s:[0-9]+" % (ifname), n) is None:
#             continue
#         ret = netifaces.ifaddresses(n)
#         if 2 not in ret:
#             continue
#         if "addr" not in ret[2][0]:
#             continue
#         if ret[2][0]["addr"] == ipaddr:
#             return n
#     return None
