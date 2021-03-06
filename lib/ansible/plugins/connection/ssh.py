# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import gettext
import fcntl
import hmac
import os
import pipes
import pty
import pwd
import random
import re
import select
import shlex
import subprocess
import time

from hashlib import sha1

from ansible import constants as C
from ansible.errors import AnsibleError, AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connection import ConnectionBase
from ansible.utils.path import unfrackpath, makedirs_safe

class Connection(ConnectionBase):
    ''' ssh based connections '''

    has_pipelining = True
    become_methods = frozenset(C.BECOME_METHODS).difference(['runas'])

    def __init__(self, *args, **kwargs):
        # SSH connection specific init stuff
        self._common_args = []
        self.HASHED_KEY_MAGIC = "|1|"

        super(Connection, self).__init__(*args, **kwargs)

        self.host = self._play_context.remote_addr
        self.ssh_extra_args = ''
        self.ssh_args = ''

    def set_host_overrides(self, host):
        v = host.get_vars()
        if 'ansible_ssh_extra_args' in v:
            self.ssh_extra_args = v['ansible_ssh_extra_args']
        if 'ansible_ssh_args' in v:
            self.ssh_args = v['ansible_ssh_args']

    @property
    def transport(self):
        ''' used to identify this connection object from other classes '''
        return 'ssh'

    def _split_args(self, argstring):
        """
        Takes a string like '-o Foo=1 -o Bar="foo bar"' and returns a
        list ['-o', 'Foo=1', '-o', 'Bar=foo bar'] that can be added to
        the argument list. The list will not contain any empty elements.
        """
        return [x.strip() for x in shlex.split(argstring) if x.strip()]

    def add_args(self, explanation, args):
        """
        Adds the given args to _common_args and displays a
        caller-supplied explanation of why they were added.
        """
        self._common_args += args
        self._display.vvvvv('SSH: ' + explanation + ': (%s)' % ')('.join(args), host=self._play_context.remote_addr)

    def _connect(self):
        ''' connect to the remote host '''

        self._display.vvv("ESTABLISH SSH CONNECTION FOR USER: {0}".format(self._play_context.remote_user), host=self._play_context.remote_addr)

        if self._connected:
            return self

        # We start with ansible_ssh_args from the inventory if it's set,
        # or [ssh_connection]ssh_args from ansible.cfg, or the default
        # Control* settings.

        if self.ssh_args:
            args = self._split_args(self.ssh_args)
            self.add_args("inventory set ansible_ssh_args", args)
        elif C.ANSIBLE_SSH_ARGS:
            args = self._split_args(C.ANSIBLE_SSH_ARGS)
            self.add_args("ansible.cfg set ssh_args", args)
        else:
            args = (
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=60s"
            )
            self.add_args("default arguments", args)

        # If any of the above have set ControlPersist but not a
        # ControlPath, add one ourselves.

        cp_in_use = False
        cp_path_set = False
        for arg in self._common_args:
            if "ControlPersist" in arg:
                cp_in_use = True
            if "ControlPath" in arg:
                cp_path_set = True

        if cp_in_use and not cp_path_set:
            self._cp_dir = unfrackpath('$HOME/.ansible/cp')

            args = ("-o", "ControlPath=\"{0}\"".format(
                C.ANSIBLE_SSH_CONTROL_PATH % dict(directory=self._cp_dir))
            )
            self.add_args("found only ControlPersist; added ControlPath", args)

            # The directory must exist and be writable.
            makedirs_safe(self._cp_dir, 0o700)
            if not os.access(self._cp_dir, os.W_OK):
                raise AnsibleError("Cannot write to ControlPath %s" % self._cp_dir)

        if not C.HOST_KEY_CHECKING:
            self.add_args(
                "ANSIBLE_HOST_KEY_CHECKING/host_key_checking disabled",
                ("-o", "StrictHostKeyChecking=no")
            )

        if self._play_context.port is not None:
            self.add_args(
                "ANSIBLE_REMOTE_PORT/remote_port/ansible_ssh_port set",
                ("-o", "Port={0}".format(self._play_context.port))
            )

        key = self._play_context.private_key_file
        if key:
            self.add_args(
                "ANSIBLE_PRIVATE_KEY_FILE/private_key_file/ansible_ssh_private_key_file set",
                ("-o", "IdentityFile=\"{0}\"".format(os.path.expanduser(key)))
            )

        if not self._play_context.password:
            self.add_args(
                "ansible_password/ansible_ssh_pass not set", (
                    "-o", "KbdInteractiveAuthentication=no",
                    "-o", "PreferredAuthentications=gssapi-with-mic,gssapi-keyex,hostbased,publickey",
                    "-o", "PasswordAuthentication=no"
                )
            )

        user = self._play_context.remote_user
        if user and user != pwd.getpwuid(os.geteuid())[0]:
            self.add_args(
                "ANSIBLE_REMOTE_USER/remote_user/ansible_ssh_user/user/-u set",
                ("-o", "User={0}".format(self._play_context.remote_user))
            )

        self.add_args(
            "ANSIBLE_TIMEOUT/timeout set",
            ("-o", "ConnectTimeout={0}".format(self._play_context.timeout))
        )

        # If any extra SSH arguments are specified in the inventory for
        # this host, or specified as an override on the command line,
        # add them in.

        if self._play_context.ssh_extra_args:
            args = self._split_args(self._play_context.ssh_extra_args)
            self.add_args("command-line added --ssh-extra-args", args)
        elif self.ssh_extra_args:
            args = self._split_args(self.ssh_extra_args)
            self.add_args("inventory added ansible_ssh_extra_args", args)

        self._connected = True

        return self

    def _run(self, cmd, indata):
        if indata:
            # do not use pseudo-pty
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdin = p.stdin
        else:
            # try to use upseudo-pty
            try:
                # Make sure stdin is a proper (pseudo) pty to avoid: tcgetattr errors
                master, slave = pty.openpty()
                p = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdin = os.fdopen(master, 'w', 0)
                os.close(slave)
            except:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdin = p.stdin

        return (p, stdin)

    def _password_cmd(self):
        if self._play_context.password:
            try:
                p = subprocess.Popen(["sshpass"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p.communicate()
            except OSError:
                raise AnsibleError("to use the 'ssh' connection type with passwords, you must install the sshpass program")
            (self.rfd, self.wfd) = os.pipe()
            return ["sshpass", "-d{0}".format(self.rfd)]
        return []

    def _send_password(self):
        if self._play_context.password:
            os.close(self.rfd)
            os.write(self.wfd, "{0}\n".format(self._play_context.password))
            os.close(self.wfd)

    def _communicate(self, p, stdin, indata, sudoable=True):
        fcntl.fcntl(p.stdout, fcntl.F_SETFL, fcntl.fcntl(p.stdout, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        fcntl.fcntl(p.stderr, fcntl.F_SETFL, fcntl.fcntl(p.stderr, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        # We can't use p.communicate here because the ControlMaster may have stdout open as well
        stdout = ''
        stderr = ''
        rpipes = [p.stdout, p.stderr]
        if indata:
            try:
                stdin.write(indata)
                stdin.close()
            except:
                raise AnsibleConnectionFailure('SSH Error: data could not be sent to the remote host. Make sure this host can be reached over ssh')
        # Read stdout/stderr from process
        while True:
            rfd, wfd, efd = select.select(rpipes, [], rpipes, 1)

            # fail early if the become password is wrong
            if self._play_context.become and sudoable:
                if self._play_context.become_pass:
                    self.check_incorrect_password(stdout)
                elif self.check_password_prompt(stdout):
                    raise AnsibleError('Missing %s password' % self._play_context.become_method)

            if p.stderr in rfd:
                dat = os.read(p.stderr.fileno(), 9000)
                stderr += dat
                if dat == '':
                    rpipes.remove(p.stderr)
            elif p.stdout in rfd:
                dat = os.read(p.stdout.fileno(), 9000)
                stdout += dat
                if dat == '':
                    rpipes.remove(p.stdout)

            # only break out if no pipes are left to read or
            # the pipes are completely read and
            # the process is terminated
            if (not rpipes or not rfd) and p.poll() is not None:
                break
            # No pipes are left to read but process is not yet terminated
            # Only then it is safe to wait for the process to be finished
            # NOTE: Actually p.poll() is always None here if rpipes is empty
            elif not rpipes and p.poll() == None:
                p.wait()
                # The process is terminated. Since no pipes to read from are
                # left, there is no need to call select() again.
                break
        # close stdin after process is terminated and stdout/stderr are read
        # completely (see also issue #848)
        stdin.close()
        return (p.returncode, stdout, stderr)

    def lock_host_keys(self, lock):

        # lock around the initial SSH connectivity so the user prompt about
        # whether to add the host to known hosts is not intermingled with
        # multiprocess output.
        #
        # This is a noop for now, pending further investigation. The lock file
        # should be opened in TaskQueueManager and passed down through the
        # PlayContext.

        pass

    def exec_command(self, *args, **kwargs):
        """
        Wrapper around _exec_command to retry in the case of an ssh failure

        Will retry if:
        * an exception is caught
        * ssh returns 255
        Will not retry if
        * remaining_tries is <2
        * retries limit reached
        """

        remaining_tries = int(C.ANSIBLE_SSH_RETRIES) + 1
        cmd_summary = "%s..." % args[0]
        for attempt in xrange(remaining_tries):
            try:
                return_tuple = self._exec_command(*args, **kwargs)
                # 0 = success
                # 1-254 = remote command return code
                # 255 = failure from the ssh command itself
                if return_tuple[0] != 255 or attempt == (remaining_tries - 1):
                    break
                else:
                    raise AnsibleConnectionFailure("Failed to connect to the host via ssh.")
            except (AnsibleConnectionFailure, Exception) as e:
                if attempt == remaining_tries - 1:
                    raise e
                else:
                    pause = 2 ** attempt - 1
                    if pause > 30:
                        pause = 30

                    if isinstance(e, AnsibleConnectionFailure):
                        msg = "ssh_retry: attempt: %d, ssh return code is 255. cmd (%s), pausing for %d seconds" % (attempt, cmd_summary, pause)
                    else:
                        msg = "ssh_retry: attempt: %d, caught exception(%s) from cmd (%s), pausing for %d seconds" % (attempt, e, cmd_summary, pause)

                    self._display.vv(msg)

                    time.sleep(pause)
                    continue


        return return_tuple

    def _exec_command(self, cmd, tmp_path, in_data=None, sudoable=True):
        ''' run a command on the remote host '''

        super(Connection, self).exec_command(cmd, tmp_path, in_data=in_data, sudoable=sudoable)

        ssh_cmd = self._password_cmd()
        ssh_cmd += ("ssh", "-C")
        if not in_data:
            # we can only use tty when we are not pipelining the modules. piping data into /usr/bin/python
            # inside a tty automatically invokes the python interactive-mode but the modules are not
            # compatible with the interactive-mode ("unexpected indent" mainly because of empty lines)
            ssh_cmd.append("-tt")
        if self._play_context.verbosity > 3:
            ssh_cmd.append("-vvv")
        else:
            ssh_cmd.append("-q")
        ssh_cmd += self._common_args

        ssh_cmd.append(self.host)

        ssh_cmd.append(cmd)
        self._display.vvv("EXEC {0}".format(' '.join(ssh_cmd)), host=self.host)

        self.lock_host_keys(True)

        # create process
        (p, stdin) = self._run(ssh_cmd, in_data)

        self._send_password()

        no_prompt_out = ''
        no_prompt_err = ''

        if self._play_context.prompt:
            '''
                Several cases are handled for privileges with password
                * NOPASSWD (tty & no-tty): detect success_key on stdout
                * without NOPASSWD:
                  * detect prompt on stdout (tty)
                  * detect prompt on stderr (no-tty)
            '''

            self._display.debug("Handling privilege escalation password prompt.")


            fcntl.fcntl(p.stdout, fcntl.F_SETFL, fcntl.fcntl(p.stdout, fcntl.F_GETFL) | os.O_NONBLOCK)
            fcntl.fcntl(p.stderr, fcntl.F_SETFL, fcntl.fcntl(p.stderr, fcntl.F_GETFL) | os.O_NONBLOCK)

            become_output = ''
            become_errput = ''
            passprompt = False
            while True:
                self._display.debug('Waiting for Privilege Escalation input')

                if self.check_become_success(become_output + become_errput):
                    self._display.debug('Succeded!')
                    break
                elif self.check_password_prompt(become_output) or self.check_password_prompt(become_errput):
                    self._display.debug('Password prompt!')
                    passprompt = True
                    break

                self._display.debug('Read next chunks')
                rfd, wfd, efd = select.select([p.stdout, p.stderr], [], [p.stdout], self._play_context.timeout)
                if not rfd:
                    # timeout. wrap up process communication
                    stdout, stderr = p.communicate()
                    raise AnsibleError('Connection error waiting for privilege escalation password prompt: %s' % become_output)

                elif p.stderr in rfd:
                    chunk = p.stderr.read()
                    become_errput += chunk
                    self._display.debug('stderr chunk is: %s' % chunk)
                    self.check_incorrect_password(become_errput)

                elif p.stdout in rfd:
                    chunk = p.stdout.read()
                    become_output += chunk
                    self._display.debug('stdout chunk is: %s' % chunk)


                if not chunk:
                    break
                    #raise AnsibleError('Connection closed waiting for privilege escalation password prompt: %s ' % become_output)

            if passprompt:
                self._display.debug("Sending privilege escalation password.")
                stdin.write(self._play_context.become_pass + '\n')
            else:
                no_prompt_out = become_output
                no_prompt_err = become_errput


        (returncode, stdout, stderr) = self._communicate(p, stdin, in_data, sudoable=sudoable)

        self.lock_host_keys(False)

        controlpersisterror = 'Bad configuration option: ControlPersist' in stderr or 'unknown configuration option: ControlPersist' in stderr

        if C.HOST_KEY_CHECKING:
            if ssh_cmd[0] == "sshpass" and p.returncode == 6:
                raise AnsibleError('Using a SSH password instead of a key is not possible because Host Key checking is enabled and sshpass does not support this.  Please add this host\'s fingerprint to your known_hosts file to manage this host.')

        if p.returncode != 0 and controlpersisterror:
            raise AnsibleError('using -c ssh on certain older ssh versions may not support ControlPersist, set ANSIBLE_SSH_ARGS="" (or ssh_args in [ssh_connection] section of the config file) before running again')
        # FIXME: module name isn't in runner
        #if p.returncode == 255 and (in_data or self.runner.module_name == 'raw'):
        if p.returncode == 255 and in_data:
            raise AnsibleConnectionFailure('SSH Error: data could not be sent to the remote host. Make sure this host can be reached over ssh')

        return (p.returncode, '', no_prompt_out + stdout, no_prompt_err + stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        self._display.vvv("PUT {0} TO {1}".format(in_path, out_path), host=self.host)
        if not os.path.exists(in_path):
            raise AnsibleFileNotFound("file or module does not exist: {0}".format(in_path))
        cmd = self._password_cmd()

        # scp and sftp require square brackets for IPv6 addresses, but
        # accept them for hostnames and IPv4 addresses too.
        host = '[%s]' % self.host

        if C.DEFAULT_SCP_IF_SSH:
            cmd.append('scp')
            cmd.extend(self._common_args)
            cmd.extend([in_path, '{0}:{1}'.format(host, pipes.quote(out_path))])
            indata = None
        else:
            cmd.append('sftp')
            cmd.extend(self._common_args)
            cmd.append(host)
            indata = "put {0} {1}\n".format(pipes.quote(in_path), pipes.quote(out_path))

        (p, stdin) = self._run(cmd, indata)

        self._send_password()

        (returncode, stdout, stderr) = self._communicate(p, stdin, indata)

        if returncode != 0:
            raise AnsibleError("failed to transfer file to {0}:\n{1}\n{2}".format(out_path, stdout, stderr))

    def fetch_file(self, in_path, out_path):
        ''' fetch a file from remote to local '''

        super(Connection, self).fetch_file(in_path, out_path)

        self._display.vvv("FETCH {0} TO {1}".format(in_path, out_path), host=self.host)
        cmd = self._password_cmd()


        if C.DEFAULT_SCP_IF_SSH:
            cmd.append('scp')
            cmd.extend(self._common_args)
            cmd.extend(['{0}:{1}'.format(self.host, in_path), out_path])
            indata = None
        else:
            cmd.append('sftp')
            # sftp batch mode allows us to correctly catch failed transfers,
            # but can be disabled if for some reason the client side doesn't
            # support the option
            if C.DEFAULT_SFTP_BATCH_MODE:
                cmd.append('-b')
                cmd.append('-')
            cmd.extend(self._common_args)
            cmd.append(self.host)
            indata = "get {0} {1}\n".format(in_path, out_path)

        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._send_password()
        stdout, stderr = p.communicate(indata)

        if p.returncode != 0:
            raise AnsibleError("failed to transfer file from {0}:\n{1}\n{2}".format(in_path, stdout, stderr))

    def close(self):

        if self._connected:

            # TODO: reenable once winrm issues are fixed
            # temporarily disabled as we are forced to currently close connections after every task because of winrm
            #if and 'ControlMaster' in self._common_args:
            #    cmd = ['ssh','-O','stop']
            #    cmd.extend(self._common_args)
            #    cmd.append(self._play_context.remote_addr)

            #    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            #    stdout, stderr = p.communicate()

            self._connected = False

