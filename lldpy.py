""" Python wrapper for lldpd (http://vincentbernat.github.io/lldpd/) """
# -*- coding: utf-8 -*-
from __future__ import print_function

import cffi
from contextlib import contextmanager
import logging
import threading

import liblldpctl

FFI = cffi.FFI()
LIB = liblldpctl.load(FFI)
LOGGER = logging.getLogger(__name__)

class Atom(object):
    """ Atoms are the primary datatype in lldpctl. Possible keys are listed
        in an enum, every key has a `lldpctl_k_` prefix.
    """
    prefix = 'lldpctl_k_'
    keys = [(k, getattr(LIB, k)) for k in dir(LIB) if k.startswith(prefix)]

    # By omission values are imported as strings. This dict keeps track of atoms which
    # refer to lists, and should be decoded as specific class types.
    key_to_type = dict()

    def __init__(self, ptr):
        """ Given a ctype pointer we should decode as much as possible. """
        for keyname, keynum in self.keys:
            if keyname in self.key_to_type:
                func = self._decode_as_atom
            else:
                func = self._decode_as_string

            val = func(keyname, keynum, ptr)
            if val is not None:
                setattr(self, keyname.replace(self.prefix, ''), val)

    @classmethod
    def _decode_as_atom(cls, keyname, keynum, ptr):
        """ Get a key field from pointer and extract it as an atom. """
        raw = LIB.lldpctl_atom_get(ptr, keynum)
        cls_ = cls.key_to_type[keyname]
        return [cls_(i) for i in Atom.walk(raw)] if raw != FFI.NULL else []

    @classmethod
    def _decode_as_string(cls, _, keynum, ptr):
        """ Get a key field from pointer and extract it as a string. """
        raw = LIB.lldpctl_atom_get_str(ptr, keynum)
        return FFI.string(raw) if raw != FFI.NULL else None

    def _enabled(self, flag):
        """ Check to see if capability field matches flag. """
        value = int(getattr(self, 'chassis_cap_enabled', 0))
        return bool(value & flag)

    @property
    def repeater_enabled(self):
        return self._enabled(0x02)

    @property
    def bridge_enabled(self):
        return self._enabled(0x04)

    @property
    def wlan_enabled(self):
        return self._enabled(0x08)

    @property
    def router_enabled(self):
        return self._enabled(0x10)

    def __repr__(self):
        return str(self.__dict__)

    @classmethod
    def walk(cls, ptr):
        """ Given an atom list ptr, iterate over contained atoms. """
        iterator = LIB.lldpctl_atom_iter(ptr)
        while iterator != FFI.NULL:
            value = LIB.lldpctl_atom_iter_value(ptr, iterator)
            yield value
            iterator = LIB.lldpctl_atom_iter_next(ptr, iterator)
            LIB.lldpctl_atom_dec_ref(value)


class Port(Atom):
    key_to_type = dict(lldpctl_k_chassis_mgmt=Atom)


class Ports(Atom):
    key_to_type = dict(lldpctl_k_port_neighbors=Port)


class Interface(Atom):
    def __init__(self, ptr):
        super(Interface, self).__init__(ptr)
        self.port = Ports(LIB.lldpctl_get_port(ptr))

    @classmethod
    def iterator(cls, conn):
        intfs = LIB.lldpctl_get_interfaces(conn)
        if intfs == FFI.NULL:
            raise StopIteration
        for i in Interface.walk(intfs):
            yield Interface(i)


class Watcher(threading.Thread):
    """ A thread which can be subclassed to watch for LLDP events. """
    def __init__(self):
        threading.Thread.__init__(self)
        LIB.lldpctl_log_callback(self.log)
        self.stop = threading.Event()
        self.daemon = True

    @contextmanager
    def connect(self):
        """ Connect to lldpd synchronously for now.
            We'd like to release connection once done, but in practice thread
            is blocking on watch, so there's no clean way of stopping it.
        """
        conn = LIB.lldpctl_new(FFI.NULL, FFI.NULL, FFI.NULL)
        # XXX: error handling
        selfptr = FFI.new_handle(self)
        LIB.lldpctl_watch_callback(conn, self.process, selfptr)
        yield conn
        LIB.lldpctl_release(conn)

    @staticmethod
    @FFI.callback("void (int, const char *)")
    def log(severity, msg):
        """ Callback for tracking logging in application rather than
            directly to STDERR. Useful for debugging library.
        """
        if severity == 5:
            logger = LOGGER.info
        elif severity == 4:
            logger = LOGGER.warning
        elif severity < 4:
            logger = LOGGER.error
        else:
            logger = LOGGER.debug
        logger(FFI.string(msg))

    @staticmethod
    @FFI.callback("lldpctl_change_callback")
    def process(_, cbtype, local, remote, selfptr):
        """ Called on every change. Dispatch to correct
            event callback.
        """
        self = FFI.from_handle(selfptr)
        args = Atom(local), Port(remote)
        if cbtype == LIB.lldpctl_c_added:
            self.on_add(*args)
        elif cbtype == LIB.lldpctl_c_deleted:
            self.on_delete(*args)
        elif cbtype == LIB.lldpctl_c_updated:
            self.on_update(*args)

    def on_add(self, local, remote):
        """ Should be subclassed. """
        pass

    def on_delete(self, local, remote):
        """ Should be subclassed. """
        pass

    def on_update(self, local, remote):
        """ Should be subclassed. """
        pass

    @property
    def running(self):
        """ Check if thread should still be running. """
        return not self.stop.is_set()

    def run(self):
        while self.running:
            with self.connect() as conn:
                self.load(conn)
                self.loop(conn)

    def load(self, conn):
        """ Load LLDP neighbors by pretending they've just been
            added.
        """
        for interface in Interface.iterator(conn):
            for port in interface.port.port_neighbors:
                self.on_add(interface, port)

    def loop(self, conn):
        """ Wait and react to events. """
        while self.running:
            err = LIB.lldpctl_watch(conn)
            if not err:
                continue
            msg = LIB.lldpctl_strerror(err)
            LOGGER.error(FFI.string(msg))
            break

