# Copyright (C) 2010-2014 Cuckoo Sandbox Developers.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

from datetime import datetime
import os
import time
import socket
import logging
import xmlrpclib
import threading
import json
from threading import Timer, Event
from StringIO import StringIO
from zipfile import ZipFile, ZIP_STORED

from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.constants import CUCKOO_GUEST_PORT, CUCKOO_GUEST_INIT
from lib.cuckoo.common.constants import CUCKOO_GUEST_COMPLETED
from lib.cuckoo.common.constants import CUCKOO_GUEST_FAILED
from lib.cuckoo.common.constants import STOP_EVENT
from lib.cuckoo.common.exceptions import CuckooGuestError
from lib.cuckoo.common.utils import create_dir_safe, TimeoutServer, sanitize_filename, ResumableTimer

log = logging.getLogger(__name__)

class GuestManager:
    """Guest Mananager.

    This class handles the communications with the agents running in the
    machines.
    """

    def __init__(self, vm_id, ip, platform="windows"):
        """@param ip: guest's IP address.
        @param platform: guest's operating system type.
        """
        self.id = vm_id
        self.ip = ip
        self.platform = platform

        self.cfg = Config()
        self.timeout = self.cfg.timeouts.critical

        url = "http://{0}:{1}".format(ip, CUCKOO_GUEST_PORT)
        self.server = TimeoutServer(url, allow_none=True,
                                    timeout=self.timeout)

    def wait(self, status):
        """Waiting for status.
        @param status: status.
        @return: always True.
        """
        log.debug("%s: waiting for status 0x%.04x", self.id, status)

        # Create an event that will invoke a function to stop the loop when
        # the critical timeout is h it.
        abort = Event()
        abort.clear()

        def die():
            abort.set()

        # Initialize the timer.
        timer = Timer(self.timeout, die)
        timer.start()
        self.server._set_timeout(self.timeout)

        while True:
            # Check if the timer was hit and the abort event was set.
            if abort.is_set():
                raise CuckooGuestError("{0}: the guest initialization hit the "
                                       "critical timeout, analysis "
                                       "aborted".format(self.id))

            try:
                # If the server returns the given status, break the loop
                # and return.
                if self.server.get_status() == status:
                    log.debug("%s: status ready", self.id)
                    break
            except:
                pass

            log.debug("%s: not ready yet", self.id)
            time.sleep(1)

        self.server._set_timeout(None)
        return True

    def upload_analyzer(self):
        """Upload analyzer to guest.
        @return: operation status.
        """
        zip_data = StringIO()
        zip_file = ZipFile(zip_data, "w", ZIP_STORED)

        # Select the proper analyzer's folder according to the operating
        # system associated with the current machine.
        root = os.path.join("analyzer", self.platform)
        root_len = len(os.path.abspath(root))

        if not os.path.exists(root):
            log.error("No valid analyzer found at path: %s", root)
            return False

        # Walk through everything inside the analyzer's folder and write
        # them to the zip archive.
        for root, dirs, files in os.walk(root):
            archive_root = os.path.abspath(root)[root_len:]
            for name in files:
                path = os.path.join(root, name)
                archive_name = os.path.join(archive_root, name)
                zip_file.write(path, archive_name)

        zip_file.close()
        data = xmlrpclib.Binary(zip_data.getvalue())
        zip_data.close()

        log.debug("Uploading analyzer to guest (id=%s, ip=%s)",
                  self.id, self.ip)

        # Send the zip containing the analyzer to the agent running inside
        # the guest.
        try:
            self.server.add_analyzer(data)
        except socket.timeout:
            raise CuckooGuestError("{0}: guest communication timeout: unable "
                                   "to upload agent, check networking or try "
                                   "to increase timeout".format(self.id))

    def start_analysis(self, options):
        """Start analysis.
        @param options: options.
        @return: operation status.
        """
        log.info("Starting analysis on guest (id=%s, ip=%s)", self.id, self.ip)

        # TODO: deal with unicode URLs.
        if options["category"] == "file":
            options["file_name"] = sanitize_filename(options["file_name"])

        try:
            # Wait for the agent to respond. This is done to check the
            # availability of the agent and verify that it's ready to receive
            # data.
            self.wait(CUCKOO_GUEST_INIT)
            # Invoke the upload of the analyzer to the guest.
            self.upload_analyzer()
            # Give the analysis options to the guest, so it can generate the
            # analysis.conf inside the guest.
            try:
                self.server.add_config(options)
            except:
                raise CuckooGuestError("{0}: unable to upload config to "
                                       "analysis machine".format(self.id))

            # If the target of the analysis is a file, upload it to the guest.
            if options["category"] == "file":
                try:
                    file_data = open(options["target"], "rb").read()
                except (IOError, OSError) as e:
                    raise CuckooGuestError("Unable to read {0}, error: "
                                           "{1}".format(options["target"], e))

                data = xmlrpclib.Binary(file_data)

                try:
                    self.server.add_malware(data, options["file_name"])
                except MemoryError as e:
                    raise CuckooGuestError("{0}: unable to upload malware to "
                                           "analysis machine, not enough "
                                           "memory".format(self.id))

            pid = self.server.execute()
            log.debug("%s: analyzer started with PID %d", self.id, pid)
        # If something goes wrong when establishing the connection, raise an
        # exception and abort the analysis.
        except (socket.timeout, socket.error):
            raise CuckooGuestError("{0}: guest communication timeout, check "
                                   "networking or try to increase "
                                   "timeout".format(self.id))

    def take_mem_dump(self, dumps_dir, machine, machinery, json_obj):
	"""
	Takes memory dump and dumps json info file.
	"""
	listdir = sorted(os.listdir(dumps_dir), key=int, reverse=True)
	if listdir == []:
		i = 1
	else:
		i = int(listdir[0]) + 1
	dump_dir = os.path.join(dumps_dir, str(i))
        os.mkdir(dump_dir)
        machinery.dump_memory(machine.label, os.path.join(dump_dir,"memory.dmp" ))
        json.dump(json_obj, file(os.path.join(dump_dir, "info.json"),"wb"), sort_keys=False, indent=4)

    def wait_for_completion(self, machine, storage, machinery):
        """Wait for analysis completion.
        @return: operation status.
        """
        log.debug("%s: waiting for completion", self.id)

        # Same procedure as in self.wait(). Just look at the comments there.
        abort = Event()
        abort.clear()

        def die():
            abort.set()

	# CHANGED: Added time-based dumps here.
	resumableTimer = ResumableTimer(self.timeout, die)
	resumableTimer.start()
	sec_counter = 0
	mem_analysis_conf = Config(os.path.join(CUCKOO_ROOT, "conf", "memoryanalysis.conf"))
	time_to_sleep = int(mem_analysis_conf.time_based.time_to_sleep_before_dump_in_seconds)
        number_of_dumps = int(mem_analysis_conf.basic.max_number_of_dumps)
        memory_results_dir = os.path.join(storage, "memory")
        dumps_dir = os.path.join(memory_results_dir, "dumps")
        create_dir_safe(memory_results_dir)
        create_dir_safe(dumps_dir)
        while True:
	    if abort.is_set():
		info_dict = {"trigger": {"name" : "End", "args": {}}, "time" : str(sec_counter)}
		log.info("Taking dump before termination...")
		self.take_mem_dump(dumps_dir, machine, machinery, info_dict)
		raise CuckooGuestError("The analysis hit the critical timeout, terminating")
	    while Event(STOP_EVENT).is_set():
	    	time.sleep(0.005)
            time.sleep(1)
	    sec_counter += 1
	    if mem_analysis_conf.basic.time_based and sec_counter % time_to_sleep == 0:
                resumableTimer.stop()
		info_dict = {"trigger": {"name" : "Time", "args": {"interval" : time_to_sleep}}}
		self.take_mem_dump(dumps_dir, machine, machinery, info_dict)	
		resumableTimer.resume()
            try:
                status = self.server.get_status()
            except Exception as e:
                log.debug("%s: error retrieving status: %s", self.id, e)
                continue

            # React according to the returned status.
            if status == CUCKOO_GUEST_COMPLETED:
                log.info("%s: analysis completed successfully", self.id)
                break
            elif status == CUCKOO_GUEST_FAILED:
                error = self.server.get_error()
                if not error:
                    error = "unknown error"
		info_dict = {"trigger": {"name" : "End", "args": {}}}
		log.info("Taking dump before termination...")
	        self.take_mem_dump(dumps_dir, machine, machinery, info_dict)
                raise CuckooGuestError("Analysis failed: {0}".format(error))
		# TODO: suspend machine and take dump
            else:
                log.debug("%s: analysis not completed yet (status=%s)",
                          self.id, status)
        self.server._set_timeout(None)
	log.info("Taking dump before termination...")
	info_dict = {"trigger": {"name" : "End", "args": {}}}
        self.take_mem_dump(dumps_dir, machine, machinery, info_dict)
