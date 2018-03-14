# -*- coding: utf-8 -*-
#
# Copyright (C) 2018 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
TraceNG VM management  in order to run a TraceNG VM.
"""

import os
import sys
import socket
import subprocess
import signal
import asyncio
import shutil

from gns3server.utils.asyncio import wait_for_process_termination
from gns3server.utils.asyncio import monitor_process
from gns3server.utils import parse_version

from .traceng_error import TraceNGError
from ..adapters.ethernet_adapter import EthernetAdapter
from ..nios.nio_udp import NIOUDP
from ..nios.nio_tap import NIOTAP
from ..base_node import BaseNode


import logging
log = logging.getLogger(__name__)


class TraceNGVM(BaseNode):
    module_name = 'traceng'

    """
    TraceNG VM implementation.

    :param name: TraceNG VM name
    :param node_id: Node identifier
    :param project: Project instance
    :param manager: Manager instance
    :param console: TCP console port
    """

    def __init__(self, name, node_id, project, manager, console=None):

        super().__init__(name, node_id, project, manager, console=console, wrap_console=True)
        self._process = None
        self._started = False
        self._traceng_stdout_file = ""
        self._local_udp_tunnel = None
        self._ethernet_adapter = EthernetAdapter()  # one adapter with 1 Ethernet interface

    @property
    def ethernet_adapter(self):
        return self._ethernet_adapter

    @asyncio.coroutine
    def close(self):
        """
        Closes this TraceNG VM.
        """

        if not (yield from super().close()):
            return False

        nio = self._ethernet_adapter.get_nio(0)
        if isinstance(nio, NIOUDP):
            self.manager.port_manager.release_udp_port(nio.lport, self._project)

        if self._local_udp_tunnel:
            self.manager.port_manager.release_udp_port(self._local_udp_tunnel[0].lport, self._project)
            self.manager.port_manager.release_udp_port(self._local_udp_tunnel[1].lport, self._project)
            self._local_udp_tunnel = None

        yield from self._stop_ubridge()

        if self.is_running():
            self._terminate_process()

        return True

    @asyncio.coroutine
    def _check_requirements(self):
        """
        Check if TraceNG is available.
        """

        path = self._traceng_path()
        if not path:
            raise TraceNGError("No path to a TraceNG executable has been set")

        # This raise an error if ubridge is not available
        self.ubridge_path

        if not os.path.isfile(path):
            raise TraceNGError("TraceNG program '{}' is not accessible".format(path))

        if not os.access(path, os.X_OK):
            raise TraceNGError("TraceNG program '{}' is not executable".format(path))

    def __json__(self):

        return {"name": self.name,
                "node_id": self.id,
                "node_directory": self.working_path,
                "status": self.status,
                "console": self._console,
                "console_type": "telnet",
                "project_id": self.project.id,
                "command_line": self.command_line}

    def _traceng_path(self):
        """
        Returns the TraceNG executable path.

        :returns: path to TraceNG
        """

        search_path = self._manager.config.get_section_config("TraceNG").get("traceng_path", "traceng")
        path = shutil.which(search_path)
        # shutil.which return None if the path doesn't exists
        if not path:
            return search_path
        return path

    @asyncio.coroutine
    def start(self):
        """
        Starts the TraceNG process.
        """

        yield from self._check_requirements()
        if not self.is_running():
            nio = self._ethernet_adapter.get_nio(0)
            command = self._build_command()
            try:
                log.info("Starting TraceNG: {}".format(command))
                self._traceng_stdout_file = os.path.join(self.working_dir, "traceng.log")
                log.info("Logging to {}".format(self._traceng_stdout_file))
                flags = 0
                #if sys.platform.startswith("win32"):
                #    flags = subprocess.CREATE_NEW_PROCESS_GROUP
                with open(self._traceng_stdout_file, "w", encoding="utf-8") as fd:
                    self.command_line = ' '.join(command)
                    self._process = yield from asyncio.create_subprocess_exec(*command,
                                                                              stdout=fd,
                                                                              stderr=subprocess.STDOUT,
                                                                              cwd=self.working_dir,
                                                                              creationflags=flags)
                    monitor_process(self._process, self._termination_callback)

                yield from self._start_ubridge()
                if nio:
                    yield from self.add_ubridge_udp_connection("TraceNG-{}".format(self._id), self._local_udp_tunnel[1], nio)

                yield from self.start_wrap_console()

                log.info("TraceNG instance {} started PID={}".format(self.name, self._process.pid))
                self._started = True
                self.status = "started"
            except (OSError, subprocess.SubprocessError) as e:
                traceng_stdout = self.read_traceng_stdout()
                log.error("Could not start TraceNG {}: {}\n{}".format(self._traceng_path(), e, traceng_stdout))
                raise TraceNGError("Could not start TraceNG {}: {}\n{}".format(self._traceng_path(), e, traceng_stdout))

    def _termination_callback(self, returncode):
        """
        Called when the process has stopped.

        :param returncode: Process returncode
        """

        if self._started:
            log.info("TraceNG process has stopped, return code: %d", returncode)
            self._started = False
            self.status = "stopped"
            self._process = None
            if returncode != 0:
                self.project.emit("log.error", {"message": "TraceNG process has stopped, return code: {}\n{}".format(returncode, self.read_traceng_stdout())})

    @asyncio.coroutine
    def stop(self):
        """
        Stops the TraceNG process.
        """

        yield from self._stop_ubridge()
        if self.is_running():
            self._terminate_process()
            if self._process.returncode is None:
                try:
                    yield from wait_for_process_termination(self._process, timeout=3)
                except asyncio.TimeoutError:
                    if self._process.returncode is None:
                        try:
                            self._process.kill()
                        except OSError as e:
                            log.error("Cannot stop the TraceNG process: {}".format(e))
                        if self._process.returncode is None:
                            log.warning('TraceNG VM "{}" with PID={} is still running'.format(self._name, self._process.pid))

        self._process = None
        self._started = False
        yield from super().stop()

    @asyncio.coroutine
    def reload(self):
        """
        Reloads the TraceNG process (stop & start).
        """

        yield from self.stop()
        yield from self.start()

    def _terminate_process(self):
        """
        Terminate the process if running
        """

        log.info("Stopping TraceNG instance {} PID={}".format(self.name, self._process.pid))
        #if sys.platform.startswith("win32"):
        #    self._process.send_signal(signal.CTRL_BREAK_EVENT)
        #else:
        try:
            self._process.terminate()
        # Sometime the process may already be dead when we garbage collect
        except ProcessLookupError:
            pass

    def read_traceng_stdout(self):
        """
        Reads the standard output of the TraceNG process.
        Only use when the process has been stopped or has crashed.
        """

        output = ""
        if self._traceng_stdout_file:
            try:
                with open(self._traceng_stdout_file, "rb") as file:
                    output = file.read().decode("utf-8", errors="replace")
            except OSError as e:
                log.warning("Could not read {}: {}".format(self._traceng_stdout_file, e))
        return output

    def is_running(self):
        """
        Checks if the TraceNG process is running

        :returns: True or False
        """

        if self._process and self._process.returncode is None:
            return True
        return False

    @asyncio.coroutine
    def port_add_nio_binding(self, port_number, nio):
        """
        Adds a port NIO binding.

        :param port_number: port number
        :param nio: NIO instance to add to the slot/port
        """

        if not self._ethernet_adapter.port_exists(port_number):
            raise TraceNGError("Port {port_number} doesn't exist in adapter {adapter}".format(adapter=self._ethernet_adapter,
                                                                                           port_number=port_number))

        if self.is_running():
            yield from self.add_ubridge_udp_connection("TraceNG-{}".format(self._id), self._local_udp_tunnel[1], nio)

        self._ethernet_adapter.add_nio(port_number, nio)
        log.info('TraceNG "{name}" [{id}]: {nio} added to port {port_number}'.format(name=self._name,
                                                                                     id=self.id,
                                                                                     nio=nio,
                                                                                     port_number=port_number))

        return nio

    @asyncio.coroutine
    def port_update_nio_binding(self, port_number, nio):
        if not self._ethernet_adapter.port_exists(port_number):
            raise TraceNGError("Port {port_number} doesn't exist in adapter {adapter}".format(adapter=self._ethernet_adapter,
                                                                                              port_number=port_number))
        if self.is_running():
            yield from self.update_ubridge_udp_connection("TraceNG-{}".format(self._id), self._local_udp_tunnel[1], nio)

    @asyncio.coroutine
    def port_remove_nio_binding(self, port_number):
        """
        Removes a port NIO binding.

        :param port_number: port number

        :returns: NIO instance
        """

        if not self._ethernet_adapter.port_exists(port_number):
            raise TraceNGError("Port {port_number} doesn't exist in adapter {adapter}".format(adapter=self._ethernet_adapter,
                                                                                              port_number=port_number))

        if self.is_running():
            yield from self._ubridge_send("bridge delete {name}".format(name="TraceNG-{}".format(self._id)))

        nio = self._ethernet_adapter.get_nio(port_number)
        if isinstance(nio, NIOUDP):
            self.manager.port_manager.release_udp_port(nio.lport, self._project)
        self._ethernet_adapter.remove_nio(port_number)

        log.info('TraceNG "{name}" [{id}]: {nio} removed from port {port_number}'.format(name=self._name,
                                                                                         id=self.id,
                                                                                         nio=nio,
                                                                                         port_number=port_number))
        return nio

    @asyncio.coroutine
    def start_capture(self, port_number, output_file):
        """
        Starts a packet capture.

        :param port_number: port number
        :param output_file: PCAP destination file for the capture
        """

        if not self._ethernet_adapter.port_exists(port_number):
            raise TraceNGError("Port {port_number} doesn't exist in adapter {adapter}".format(adapter=self._ethernet_adapter,
                                                                                              port_number=port_number))

        nio = self._ethernet_adapter.get_nio(0)

        if not nio:
            raise TraceNGError("Port {} is not connected".format(port_number))

        if nio.capturing:
            raise TraceNGError("Packet capture is already activated on port {port_number}".format(port_number=port_number))

        nio.startPacketCapture(output_file)

        if self.ubridge:
            yield from self._ubridge_send('bridge start_capture {name} "{output_file}"'.format(name="TraceNG-{}".format(self._id),
                                                                                               output_file=output_file))

        log.info("TraceNG '{name}' [{id}]: starting packet capture on port {port_number}".format(name=self.name,
                                                                                                 id=self.id,
                                                                                                 port_number=port_number))

    @asyncio.coroutine
    def stop_capture(self, port_number):
        """
        Stops a packet capture.

        :param port_number: port number
        """

        if not self._ethernet_adapter.port_exists(port_number):
            raise TraceNGError("Port {port_number} doesn't exist in adapter {adapter}".format(adapter=self._ethernet_adapter,
                                                                                              port_number=port_number))

        nio = self._ethernet_adapter.get_nio(0)

        if not nio:
            raise TraceNGError("Port {} is not connected".format(port_number))

        nio.stopPacketCapture()

        if self.ubridge:
            yield from self._ubridge_send('bridge stop_capture {name}'.format(name="TraceNG-{}".format(self._id)))

        log.info("TraceNG '{name}' [{id}]: stopping packet capture on port {port_number}".format(name=self.name,
                                                                                                 id=self.id,
                                                                                                 port_number=port_number))

    def _build_command(self):
        """
        Command to start the TraceNG process.
        (to be passed to subprocess.Popen())
        """

        command = [self._traceng_path()]

        # TODO: remove when testing with  executable
        command.extend(["-p", str(self._internal_console_port)])  # listen to console port
        command.extend(["-m", "1"])   # the unique ID is used to set the MAC address offset
        command.extend(["-i", "1"])  # option to start only one VPC instance
        command.extend(["-F"])  # option to avoid the daemonization of VPCS

        # use the local UDP tunnel to uBridge instead
        if not self._local_udp_tunnel:
            self._local_udp_tunnel = self._create_local_udp_tunnel()
        nio = self._local_udp_tunnel[0]
        if nio and isinstance(nio, NIOUDP):
            # UDP tunnel
            command.extend(["-s", str(nio.lport)])  # source UDP port
            command.extend(["-c", str(nio.rport)])  # destination UDP port
            try:
                command.extend(["-t", socket.gethostbyname(nio.rhost)])  # destination host, we need to resolve the hostname because TraceNG doesn't support it
            except socket.gaierror as e:
                raise TraceNGError("Can't resolve hostname {}: {}".format(nio.rhost, e))

        return command