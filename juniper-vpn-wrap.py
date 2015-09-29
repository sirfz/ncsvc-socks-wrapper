#!/usr/bin/python
# -*- coding: utf-8 -*-

import subprocess
import mechanize
import cookielib
import getpass
import sys
import os
import zipfile
import urllib
import socket
import ssl
import errno
import argparse
import atexit
import signal
import ConfigParser
import time
import binascii
import hmac
import hashlib
import tncc

from urlparse import urlparse, parse_qs
from HTMLParser import HTMLParser

ssl._create_default_https_context = ssl._create_unverified_context


def mkdir_p(path):
    try:
        os.mkdir(path)
    except OSError, exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise
"""
OATH code from https://github.com/bdauvergne/python-oath
Copyright 2010, Benjamin Dauvergne

* All rights reserved.
* Redistribution and use in source and binary forms, with or without
  modification, are permitted provided that the following conditions are met:

     * Redistributions of source code must retain the above copyright
       notice, this list of conditions and the following disclaimer.
     * Redistributions in binary form must reproduce the above copyright
       notice, this list of conditions and the following disclaimer in the
       documentation and/or other materials provided with the distribution.'''
"""


def truncated_value(h):
    bytes = map(ord, h)
    offset = bytes[-1] & 0xf
    v = (bytes[offset] & 0x7f) << 24 | (bytes[offset + 1] & 0xff) << 16 | \
        (bytes[offset + 2] & 0xff) << 8 | (bytes[offset + 3] & 0xff)
    return v


def dec(h, p):
    v = truncated_value(h)
    v = v % (10**p)
    return '%0*d' % (p, v)


def int2beint64(i):
    hex_counter = hex(long(i))[2:-1]
    hex_counter = '0' * (16 - len(hex_counter)) + hex_counter
    bin_counter = binascii.unhexlify(hex_counter)
    return bin_counter


def hotp(key):
    key = binascii.unhexlify(key)
    counter = int2beint64(int(time.time()) / 30)
    return dec(hmac.new(key, counter, hashlib.sha256).digest(), 6)


def read_narport(narport_file):
    with open(narport_file) as f_:
        return int(f_.read().strip())


def get_script_output(script_):
    p = subprocess.Popen(script_.split(), stdout=subprocess.PIPE)
    output_, _ = p.communicate()
    try:
        p.terminate()
    except OSError:
        pass
    return output_.strip()


class RolesParser(HTMLParser):

    _NONE = 0
    _TD_CLOSED = 1
    _TD_WIDTH_100 = 2

    def __init__(self, *args, **kwargs):
        HTMLParser.__init__(self, *args, **kwargs)
        self._last_state = self._NONE

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            if self._last_state == self._TD_CLOSED and dict(attrs).get('width') == '100%':
                self._last_state = self._TD_WIDTH_100
                return
        self._last_state = self._NONE

    def handle_endtag(self, tag):
        if tag == 'td':
            self._last_state = self._TD_CLOSED
        else:
            self._last_state = self._NONE

    def handle_data(self, data):
        if self._last_state == self._TD_WIDTH_100:
            data = data.strip()
            if data:
                print '\t', data


class juniper_vpn_wrapper(object):

    def __init__(self, vpn_host, vpn_url, username, password, password2, oath, socks_port, host_checker):
        self.vpn_host = vpn_host
        self.vpn_url = vpn_url
        self.username = username
        self.password = password
        self.password2 = password2
        self.oath = oath
        self.fixed_password = password is not None
        self.socks_port = socks_port
        self.host_checker = host_checker
        self.last_ncsvc = 0
        self.plugin_jar = '/usr/share/icedtea-web/plugin.jar'

        if not os.path.isfile(self.plugin_jar):
            raise Exception(self.plugin_jar + ' not found')

        self.br = mechanize.Browser()

        self.cj = cookielib.LWPCookieJar()
        self.br.set_cookiejar(self.cj)

        # Browser options
        self.br.set_handle_equiv(True)
        self.br.set_handle_redirect(True)
        self.br.set_handle_referer(True)
        self.br.set_handle_robots(False)

        # Follows refresh 0 but not hangs on refresh > 0
        self.br.set_handle_refresh(mechanize._http.HTTPRefreshProcessor(),
                                   max_time=1)

        # Want debugging messages?
        # self.br.set_debug_http(True)
        # self.br.set_debug_redirects(True)
        # self.br.set_debug_responses(True)

        self.user_agent = ('Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615 '
                           'Fedora/3.0.1-1.fc9 Firefox/3.0.1')
        self.br.addheaders = [('User-agent', self.user_agent)]

        self.last_action = None
        self.tncc_process = None
        self.needs_2factor = False
        self.key = None

        self.tncc_jar = None
        self.ncsvc_bin = None

    def find_cookie(self, name):
        for cookie in self.cj:
            if cookie.name == name:
                return cookie
        return None

    def next_action(self):
        if self.find_cookie('DSID'):
            return 'ncsvc'

        for form in self.br.forms():
            if form.name == 'frmLogin':
                return 'login'
            elif form.name == 'frmDefender':
                return 'key'
            elif form.name == 'frmConfirmation':
                return 'continue'
            elif form.name == 'frmSelectRoles':
                return 'select_roles'
            elif form.name == 'frm':
                url_ = urlparse(self.r.geturl())
                qs = parse_qs(url_.query)
                if 'rolecheck' in qs.get('step', []):
                    print 'Host Cheker has failed:'
                    parser = RolesParser()
                    parser.feed(self.r.read())
                    sys.exit(1)
                raise Exception('Unknown form type "{}" at {}'.format(form.name, url_.geturl()))
            else:
                raise Exception('Unknown form type "{}" at {}'.format(form.name, self.r.geturl()))
        return 'tncc'

    def run(self):
        # Open landing page
        self.r = self.br.open(
            'https://{}/dana-na/auth/{}/welcome.cgi'.format(self.vpn_host, self.vpn_url))
        while True:
            action = self.next_action()
            print 'next action [{}]: {}'.format(self.r.geturl(), action)
            if action == 'tncc':
                self.action_tncc()
            elif action == 'login':
                self.action_login()
            elif action == 'key':
                self.action_key()
            elif action == 'select_roles':
                self.action_select_roles()
            elif action == 'continue':
                self.action_continue()
            elif action == 'ncsvc':
                self.action_ncsvc()

            self.last_action = action

    def action_tncc(self):
        # Run tncc host checker
        dspreauth_cookie = self.find_cookie('DSPREAUTH')
        if dspreauth_cookie is None:
            raise Exception('Could not find DSPREAUTH key for host checker')

        dssignin_cookie = self.find_cookie('DSSIGNIN')
        if self.host_checker:
            t = tncc.tncc(self.vpn_host, self.cj, self.user_agent)
            self.cj.set_cookie(t.get_cookie(dspreauth_cookie, dssignin_cookie))
        else:

            dssignin = (dssignin_cookie.value if dssignin_cookie else 'null')

            if not self.tncc_process:
                self.tncc_start()

            args = [('IC', self.vpn_host), ('Cookie',
                                            dspreauth_cookie.value), ('DSSIGNIN', dssignin)]

            try:
                self.tncc_send('start', args)
                results = self.tncc_recv()
            except:
                self.tncc_start()
                self.tncc_send('start', args)
                results = self.tncc_recv()

            if len(results) < 4:
                raise Exception('tncc returned insufficent results', results)

            if results[0] == '200':
                dspreauth_cookie.value = results[2]
                self.cj.set_cookie(dspreauth_cookie)
            elif self.last_action == 'tncc':
                raise Exception(
                    'tncc returned non 200 code (' + results[0] + ')')
            else:
                self.cj.clear(self.vpn_host, '/dana-na/', 'DSPREAUTH')

        self.r = self.br.open(self.r.geturl())

    def action_login(self):
        # The token used for two-factor is selected when this form is submitted.
        # If we aren't getting a password, then get the key now, otherwise
        # we could be sitting on the two factor key prompt later on waiting
        # on the user.

        self.br.select_form(nr=0)
        if self.password is None or self.last_action == 'login':
            for control in self.br.form.controls:
                if control.name == 'password#2':
                    if self.password2 is None:
                        self.password2 = getpass.getpass('Password#2:')
                    elif self.password2.startswith('script:'):
                        self.password2 = get_script_output(self.password2[7:])
                    self.br.form['password#2'] = self.password2
                    self.r = self.br.submit()
                    return
            if self.fixed_password:
                print 'Login failed (Invalid username or password?)'
                sys.exit(1)
            else:
                self.password = getpass.getpass('Password:')
                self.needs_2factor = False

        if self.needs_2factor:
            if self.oath:
                self.key = hotp(self.oath)
            else:
                self.key = getpass.getpass('Two-factor key:')
        else:
            self.key = None

        if self.password.startswith('script:'):
            self.password = get_script_output(self.password[7:])

        # Enter username/password
        self.br.form['username'] = self.username
        self.br.form['password'] = self.password
        # Untested, a list of availables realms is provided when this
        # is necessary.
        # self.br.form['realm'] = [realm]
        self.r = self.br.submit()

    def action_key(self):
        # Enter key
        self.needs_2factor = True
        if self.oath:
            if self.last_action == 'key':
                print 'Login failed (Invalid OATH key)'
                sys.exit(1)
            self.key = hotp(self.oath)
        elif self.key is None:
            self.key = getpass.getpass('Two-factor key:')
        self.br.select_form(nr=0)
        self.br.form['password'] = self.key
        self.key = None
        self.r = self.br.submit()

    def action_select_roles(self):
        links = list(self.br.links())
        if len(links) == 1:
            link = links[0]
        else:
            print 'Choose one of the following: '
            for i, link in enumerate(links):
                print '{} - {}'.format(i, link.text)
            choice = int(raw_input('Choice: '))
            link = links[choice]
        self.r = self.br.follow_link(text=link.text)

    def action_continue(self):
        # Yes, I want to terminate the existing connection
        self.br.select_form(nr=0)
        self.r = self.br.submit()

    def action_ncsvc(self):
        dspreauth_cookie = self.find_cookie('DSPREAUTH')
        if dspreauth_cookie is not None and not self.host_checker:
            try:
                self.tncc_send(
                    'setcookie', [('Cookie', dspreauth_cookie.value)])
            except:
                # TNCC died, bummer
                self.tncc_stop()
        if self.ncsvc_start() == 3:
            # Code 3 indicates that the DSID we tried was invalid
            self.cj.clear(self.vpn_host, '/', 'DSID')
            self.r = self.br.open(self.r.geturl())

    def tncc_send(self, cmd, params):
        v = cmd + '\n'
        for key, val in params:
            v = v + key + '=' + val + '\n'
        self.tncc_socket.send(v)

    def tncc_recv(self):
        print 'receiving...'
        ret = self.tncc_socket.recv(1024)
        return ret.splitlines()

    def tncc_init(self):
        class_names = ('net.juniper.tnc.NARPlatform.linux.LinuxHttpNAR',
                       'net.juniper.tnc.HttpNAR.HttpNAR')
        self.class_name = None

        self.tncc_jar = os.path.expanduser('~/.juniper_networks/tncc.jar')
        try:
            if zipfile.ZipFile(self.tncc_jar, 'r').testzip() is not None:
                raise Exception()
        except:
            print 'Downloading tncc.jar...'
            mkdir_p(os.path.expanduser('~/.juniper_networks'))
            urllib.urlretrieve('https://{}/dana-cached/hc/tncc.jar'.format(self.vpn_host), self.tncc_jar)

        with zipfile.ZipFile(self.tncc_jar, 'r') as jar:
            for name in class_names:
                try:
                    jar.getinfo(name.replace('.', '/') + '.class')
                    self.class_name = name
                    break
                except:
                    pass

        if self.class_name is None:
            raise Exception('Could not find class name for', self.tncc_jar)

        self.tncc_preload = \
            os.path.expanduser('~/.juniper_networks/tncc_preload.so')
        if not os.path.isfile(self.tncc_preload):
            raise Exception('Missing', self.tncc_preload)

    def tncc_stop(self):
        if self.tncc_process is not None:
            try:
                self.tncc_process.send_signal(signal.SIGINT)
                self.tncc_process.wait()
            except:
                pass
            self.tncc_socket = None

    def tncc_start(self):
        # tncc is the host checker app. It can check different
        # security policies of the host and report back. We have
        # to send it a preauth key (from the DSPREAUTH cookie)
        # and it sends back a new cookie value we submit.
        # After logging in, we send back another cookie to tncc.
        # Subsequently, it contacts https://<vpn_host:443 every
        # 10 minutes.

        if not self.tncc_jar:
            self.tncc_init()

        narport = os.path.expanduser('~/.juniper_networks/narport.txt')
        if os.path.isfile(narport):
            self._narport = read_narport(narport)
            try:
                self.tncc_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.tncc_socket.connect(('127.0.0.1', self._narport))
            except Exception as e:
                print 'WARNING: {} port {}: {}'.format(type(e).__name__, self._narport, e)
                os.remove(narport)
            else:
                return
        # self.tncc_socket, sock = socket.socketpair(
        #     socket.AF_UNIX, socket.SOCK_SEQPACKET)
        # null = open(os.devnull, 'w')

        self.tncc_process = subprocess.Popen(['java',
                                              '-classpath', self.tncc_jar + ':' + self.plugin_jar,
                                              self.class_name,
                                              'log_level', '2',
                                              'postRetries', '6',
                                              'ivehost', self.vpn_host,
                                              'home_dir', os.path.expanduser('~'),
                                              'Parameter0', '',
                                              'user_agent', self.user_agent,
                                              ])
        # , env={'LD_PRELOAD': self.tncc_preload}, stdin=sock, stdout=null,
        # time.sleep(3)

        while not os.path.isfile(narport):
            time.sleep(0.5)
        self._narport = read_narport(narport)
        self.tncc_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tncc_socket.connect(('127.0.0.1', self._narport))

    def ncsvc_init(self):
        ncLinuxApp_jar = os.path.expanduser(
            '~/.juniper_networks/ncLinuxApp.jar')
        self.ncsvc_bin = os.path.expanduser('~/.juniper_networks/ncsvc')
        self.ncsvc_preload = os.path.expanduser(
            '~/.juniper_networks/ncsvc_preload.so')
        try:
            if zipfile.ZipFile(ncLinuxApp_jar, 'r').testzip() is not None:
                raise Exception()
        except:
            # Note, we need the authenticated connection to download this jar
            print 'Downloading ncLinuxApp.jar...'
            mkdir_p(os.path.expanduser('~/.juniper_networks'))
            self.br.retrieve('https://' + self.vpn_host + '/dana-cached/nc/ncLinuxApp.jar',
                             ncLinuxApp_jar)

        with zipfile.ZipFile(ncLinuxApp_jar, 'r') as jar:
            jar.extract('ncsvc', os.path.expanduser('~/.juniper_networks/'))

        os.chmod(self.ncsvc_bin, 0755)

        if not os.path.isfile(self.ncsvc_preload):
            raise Exception('Missing', self.ncsvc_preload)

        # FIXME: This should really be form the webclient connection,
        # and the web client should verify the cert

        s = socket.socket()
        s.connect((self.vpn_host, 443))
        ss = ssl.wrap_socket(s)
        cert = ss.getpeercert(True)
        self.certfile = os.path.expanduser('~/.juniper_networks/{}.cert'.format(self.vpn_host))
        with open(self.certfile, 'w') as f:
            f.write(cert)

    def ncsvc_start(self):
        if self.ncsvc_bin is None:
            self.ncsvc_init()

        now = time.time()
        delay = 10.0 - (now - self.last_ncsvc)
        if delay > 0:
            print 'Waiting %.0f...' % (delay)
            time.sleep(delay)
        self.last_ncsvc = time.time()

        dsid_cookie = self.find_cookie('DSID')
        p = subprocess.Popen([self.ncsvc_bin,
                              '-h', self.vpn_host,
                              '-c', 'DSID=' + dsid_cookie.value,
                              '-f', self.certfile,
                              '-p', str(self.socks_port),
                              '-l', '0',
                              ], env={'LD_PRELOAD': self.ncsvc_preload})
        ret = p.wait()
        # 9 - certificate mismatch
        # 6 - closed after being open for a while
        #   - could not connect to host
        # 3 - incorrect DSID
        return ret

    def logout(self):
        print 'terminating...'
        self.tncc_stop()
        try:
            # self.cj.clear(self.vpn_host, '/', 'DSID')
            self.r = self.br.open("https://{}/dana-na/auth/logout.cgi".format(self.vpn_host))
            # self.r = self.br.open(self.r.geturl())
        except Exception as e:
            print 'WARNING: {} Logout call failed: {}'.format(type(e).__name__, e)

        if hasattr(self, 'ncsvc_process'):
            try:
                self.ncsvc_process.send_signal(signal.SIGINT)
                self.ncsvc_process.wait()
            except OSError as e:
                print 'Failed to terminate process: {}'.format(e)


def cleanup(jvpn):
    # os.killpg(0, signal.SIGTERM)
    jvpn.logout()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-h', '--host', type=str,
                        help='VPN host name')
    parser.add_argument('-l', '--url', type=str,
                        help='VPN url part', default='url_default')
    parser.add_argument('-u', '--user', type=str,
                        help='User name')
    parser.add_argument('-o', '--oath', type=str,
                        help='OATH key for two factor authentication (hex)')
    parser.add_argument('-p', '--socks_port', type=int, default=1080,
                        help='Socks proxy port (default: %(default))')
    parser.add_argument('-c', '--config', type=str,
                        help='Config file for the script')
    parser.add_argument('-H', '--host-checker',
                        help='Use build in host checker')

    args = parser.parse_args()
    password = None
    password2 = None
    oath = None

    if args.config is not None:
        config = ConfigParser.RawConfigParser()
        config.read(args.config)
        try:
            args.user = config.get('vpn', 'username')
        except:
            pass
        try:
            args.host = config.get('vpn', 'host')
        except:
            pass
        try:
            args.url = config.get('vpn', 'url')
        except:
            pass
        try:
            password = config.get('vpn', 'password')
        except:
            pass
        try:
            password2 = config.get('vpn', 'password2')
        except:
            pass
        try:
            oath = config.get('vpn', 'oath')
        except:
            pass
        try:
            args.socks_port = config.get('vpn', 'socks_port')
        except:
            pass
        try:
            val = config.get('vpn', 'host_checker').lower()
            if val in {'true', 'yes', 'on', 'enable', 'enabled', '1'}:
                args.host_checker = True
        except:
            pass

    if args.user is None or args.host is None:
        print "--user and --host are required parameters"
        sys.exit(1)

    jvpn = juniper_vpn_wrapper(
        args.host, args.url, args.user, password, password2, oath, args.socks_port, args.host_checker)
    atexit.register(cleanup, jvpn)
    jvpn.run()
