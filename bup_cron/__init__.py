#!/usr/bin/python3

"""
simple wrapper around bup index and save designed to be ran in cron
jobs, with support for filesystem snapshots, logging and configuration
files.

options can also be specified, without --, in %s. an
arbitrary configuration file may also be supplied on the
commandline with a @ prefix (e.g. @foo.conf).

at least path and repo need to be specified."""

__version_info__ = ("2", "0")
__version__ = ".".join(__version_info__)
__author__ = "Antoine Beaupré"
__email__ = "anarcat@debian.org"
__copyright__ = "(C) 2013-2020 %s <%s>" % (__author__, __email__)
__warranty__ = """This program comes with ABSOLUTELY NO WARRANTY.  This is free
software, and you are welcome to redistribute it under certain
conditions; see `--copyright` for details."""
__license__ = """This program is free software: you can redistribute
it and/or modify it under the terms of the GNU Affero General Public
License as published by the Free Software Foundation, either version 3
of the License.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public
License along with this program. If not, see
<http://www.gnu.org/licenses/>.
"""

import argparse
import datetime
import errno
import locale
import logging
import logging.handlers
import os
import platform
import re
import socket
import stat
import subprocess
import sys
import traceback

global_logger = None


class ArgumentConfigParser(argparse.ArgumentParser):
    configs = ["/etc/bup-cron.conf", "~/.bup-cron.conf", "~/.config/bup-cron.conf"]
    snapshot_names = ["LVM", "NO"]
    pidfile = ".bup-cron.pid"

    def __init__(self):
        if sys.platform.startswith("cygwin"):
            self.snapshot_names += ["VSS"]

        """various settings for the argument parser"""
        argparse.ArgumentParser.__init__(
            self,
            description=__copyright__ + "\n" + __warranty__,
            epilog=__doc__ % " or ".join(self.configs),
            fromfile_prefix_chars="@",
        )
        group = self.add_argument_group(
            "Bup configuration options",
            """Those options allow you to
               configure how bup will behave""",
        )
        group.add_argument("paths", nargs="*", help="list of paths to backup")
        # different list because dest=paths doesn't work, it gets
        # overwritten by the next one
        group.add_argument(
            "-p",
            "--path",
            action="append",
            help="""add path to list of paths to backup,
                    mostly useful for the configuration
                    file""",
        )
        if "BUP_DIR" in os.environ:
            defdir = os.environ["BUP_DIR"]
        else:
            defdir = None
        group.add_argument(
            "-d",
            "--repository",
            default=defdir,
            help="""the directory to backup to, defaults
                    to $BUP_DIR (%s)"""
            % defdir,
        )
        group.add_argument(
            "-n",
            "--name",
            default=None,
            help="""base of the branch name passed to bup,
                    defaults to the hostname""",
        )
        group.add_argument(
            "-b",
            "--branch-name",
            default=None,
            help="""full name of the branch name passed to bup,
                    overrides --name""",
        )
        group.add_argument(
            "-r",
            "--remote",
            default=None,
            help="""a SSH address to save the backup remotely
                    (example: bup@example.com:repos/repo.bup)""",
        )
        group.add_argument(
            "-x",
            "--exclude",
            action="append",
            help="""exclude path from the backup,
                    will be passed as --exclude to bup""",
        )
        group.add_argument(
            "--exclude-rx",
            action="append",
            help="""exclude path matching regex pattern,
                    will be passed as --exclude-rx to bup""",
        )
        group.add_argument(
            "--exclude-from",
            action="append",
            help="""read --exclude paths from filename,
                    will be passed as --exclude-from to bup""",
        )
        group.add_argument(
            "--exclude-rx-from",
            action="append",
            help="""read --exclude-rx patterns from filename,
                    will be passed as --exclude-rx-from to bup""",
        )
        group = self.add_argument_group(
            "Extra jobs",
            """Those are extra features that
               bup-cron will run before or after
               the backup""",
        )
        group.add_argument(
            "--clear",
            action="store_true",
            help="""redo a full backup
                    (runs bup index --clear before starting)""",
        )
        group.add_argument(
            "--parity",
            action="store_true",
            help="""generate recovery blocks after backup.
                    runs bup fsck -g after the backup,
                    requires par2(1).""",
        )
        group.add_argument(
            "--check", action="store_true", help="""run fsck --quick after backup"""
        )
        group.add_argument(
            "--repair",
            action="store_true",
            help="""run fsck -r if fsck fails after backup, implies --check""",
        )
        group.add_argument(
            "-s",
            "--snapshot",
            nargs="?",
            default="NO",
            const="LVM",
            choices=self.snapshot_names,
            type=str.upper,
            help="""snapshot filesystem before backup.
                    this will automatically guess the
                    path to the logical volume, create a
                    snapshot, mount it, then remove it
                    when it is done, default: %(default)s,
                    LVM if -s specified without argument""",
        )
        group.add_argument(
            "-z",
            "--size",
            action="store",
            default=Snapshot.size,
            help="""size of the LVM snapshot,
                    defaults to %(default)s""",
        )
        group.add_argument(
            "-m",
            "--mountpoint",
            action="store",
            default=Snapshot.mountpattern,
            help="""mountpoint of the snapshot device.
                    should contain two %%s patterns, for
                    the VG and LV names, default:
                    %(default)s)""",
        )
        group.add_argument(
            "--stats",
            action="store_true",
            help="""save statistics about backups
                    as a git note""",
        )
        group = self._optionals
        group.title = "Daemon and logging"
        group.description = """Those options define how bup-cron
                               itself will behave"""
        group.add_argument(
            "--copyright",
            action="store_true",
            help="""display copyright notice and exit""",
        )
        group.add_argument(
            "--version", action="store_true", help="""display version number and exit"""
        )
        group.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="""output more information on console.
                    tries to be silent if not specified.
                    -v implies explaining what we do,
                    -vv shows output of commands,
                    -vvv passes verbose to those commands""",
        )
        group.add_argument(
            "-D",
            "--debug",
            action="store_true",
            help="""print debug backtrace on unhandled exceptions\
                    - by default only the message is printed""",
        )
        group.add_argument(
            "-l",
            "--logfile",
            default=sys.stdout,
            help="""file where logs should be written,
                    defaults to stdout""",
        )
        levels = sorted([v for k, v in logging._levelToName.items() if v != "NOTSET"])
        group.add_argument(
            "--syslog",
            nargs="?",
            default=None,
            type=str.upper,
            action="store",
            const="INFO",
            choices=levels,
            help="""log to syslog facility, default: no
                    logging, INFO if --syslog is specified without
                    argument""",
        )
        group.add_argument(
            "--pidfile",
            default=None,
            action="store",
            help="""lockfile to write to avoid
                    simultanous execution, defaults to
                    $BUP_DIR/%s"""
            % self.pidfile,
        )

    def convert_arg_line_to_args(self, arg_line):
        """parse a config file"""
        # skip whitespace and commented lines
        if re.match(r"^(#|[\s]*$)", arg_line):
            return []
        else:
            # all lines are assumed to be options
            return ["--" + arg_line]

    def parse_args(self):
        """Process arguments list

        Inject system and user config files and cleanup various
        arguments and defaults that couldn't be done otherwise."""
        configs = map(lambda x: os.path.expanduser(x), self.configs)
        for conf in configs:
            try:
                with open(conf, "r"):
                    sys.argv.insert(1, "@" + conf)
            except IOError:
                pass
        args = argparse.ArgumentParser.parse_args(self)
        if args.copyright:
            self.exit(0, __license__)
        if args.version:
            self.exit(0, __version__ + "\n")
        if "BUP_DIR" not in os.environ and not args.repository:
            self.error("argument -d/--repository is required")

        if args.name and args.branch_name:
            self.error(
                "The options --name and --branch-name cannot " "be used together."
            )

        # merge the path and paths arguments
        if args.path:
            args.paths += args.path
        # remove this one to avoid ambiguity
        del args.path
        if len(args.paths) < 1:
            self.error("argument paths is required")
        os.environ["BUP_DIR"] = args.repository
        # remove this one to avoid ambiguity
        del args.repository
        if args.pidfile is None:
            args.pidfile = os.path.join(os.environ["BUP_DIR"], self.pidfile)
        # repair implies check
        args.check |= args.repair
        return args


class Snapshot(object):
    """abstract class to handle filesystem snapshots"""

    """default snapshot size"""
    size = "1GB"

    """default location the snapshot is mounted on"""
    mountpattern = "/media/bup/%s-%s"

    def __init__(
        self,
        path,
        size,
        log=sys.stdout.write,
        warn=sys.stderr.write,
        verbose=0,
        call=subprocess.check_call,
        mountpattern=None,
    ):
        """initialise the snapshot array

        path is expected to be part of the target filesystem; log and warn are
        logging utilities; call is a way to call processes that will return
        true on success or false otherwise"""
        self.src_path = path
        self.path = path
        self.size = size
        self.log = log
        self.warn = warn
        self.verbose = verbose
        self.call = call
        # if the snapshot has been created
        self.exists = False
        if mountpattern is not None:
            self.mountpattern = mountpattern

    def __enter__(self):
        """this should be reimplemented by subclasses

        this should:

        1. create a snapshot
        2. mount it in a specific location
        3. set that location in self.path, or leave the original path
        in place otherwise"""
        return self

    def __exit__(self, t, e, tb):
        # return false to raise, true to pass
        self.cleanup()
        self.exists = False
        return t is None

    def cleanup(self):
        """this function should undo all that __enter__() did"""
        pass

    @staticmethod
    def select(name):
        """Returns the class who handles name"""
        for cls in Snapshot.__subclasses__():
            if name.lower() in cls.__name__.lower():
                return cls
        raise TypeError("Unknown type: %s" % name)


class NoSnapshot(Snapshot):
    """special class to skip snapshotting

    basically a noop"""

    pass


class LvmSnapshot(Snapshot):
    def __enter__(self):
        """set the LVM and mount it"""
        self.vg_lv = None
        mountpoint = self.find_mountpoint()
        if mountpoint:
            device = self.find_device(mountpoint)
            if device:
                # vg, lv
                self.vg_lv = LvmSnapshot.find_vg_lv(device)
            if device and self.vg_lv:
                # forced cleanup
                self.cleanup(True)
                cmd = [
                    "lvcreate",
                    "--size",
                    self.size,
                    "--snapshot",
                    "--permission",
                    "r",
                    "--name",
                    self.snapname(),
                    device,
                ]
                if self.verbose <= 0:
                    cmd += ["--quiet"]
                if self.verbose >= 3:
                    cmd += ["--verbose"]
                logging.debug("creating snapshot %s" % self.snapname())
                if self.call(cmd):
                    logging.debug(
                        "making sure mountpoint %s exists" % self.mountpoint()
                    )
                    if make_dirs_helper(self.mountpoint()):
                        logging.debug("mountpoint %s created" % self.mountpoint())
                    self.exists = True
                    if self.call(
                        ["mount", "-o", "ro", self.device(), self.mountpoint()]
                    ):
                        relpath = os.path.relpath(self.path, mountpoint)
                        self.path = os.path.join(self.mountpoint(), relpath)
                    else:
                        logging.warning(
                            """failed to mount snapshot %s on %s,
skipping snapshotting"""
                            % (self.snapname(), self.mountpoint())
                        )
                        self.cleanup()
                else:
                    logging.warning(
                        """failed to create snapshot %s/%s,
skipping snapshooting"""
                        % self.vg_lv
                    )
            else:
                # XXX: we could try to find the parent mountpoint...
                # see https://github.com/pfrouleau/bup/commit/1244a2da0bf480591b19b9b6123a51ab8662ab56
                logging.warning(
                    "%s is not a LVM mountpoint, skipping snapshotting" % self.path
                )
        else:
            logging.warning(
                "Could not find mountpoint for %s, skipping snapshotting" % self.path
            )
        return self

    def find_mountpoint(self):
        path = os.path.realpath(self.path)
        while not os.path.ismount(path):
            dirname = os.path.dirname(path)
            if dirname == path:
                return None
            path = dirname
        return path

    def find_device(self, mountpoint):
        """find device based on mountpoint path

        returns the device or False if none found"""

        mounts = subprocess.check_output(["mount"])
        try:
            return re.match(
                r".*^(/[^ ]*) on %s .*" % mountpoint, mounts, re.MULTILINE | re.DOTALL
            ).group(1)
        except:  # noqa
            return False

    @staticmethod
    def find_vg_lv(device):
        """find the volume group and logical volume of the specified device"""
        try:
            lvs = subprocess.check_output(["lvs", device], close_fds=True)
        except subprocess.CalledProcessError:
            # not a LVM
            return False
        # second line of output, second and third fields, backwards
        return tuple(re.split(r" +", re.split("\n", lvs)[1])[2:0:-1])

    def snapname(self):
        """the name of the snapshot volume to be created

        pattern should have two string wildcards, one for vg, the
        other for lv"""
        return "snap%s" % self.vg_lv[1]

    def mountpoint(self):
        """where to mount the snapshot device"""
        return self.mountpattern % self.vg_lv

    def device(self):
        """path to the device of the snapshot LV"""
        return "/dev/%s/%s" % (self.vg_lv[0], self.snapname())

    def cleanup(self, force=False):
        """cleanup everything we did here"""
        if not self.exists and not force:
            return
        self.exists = False
        m = self.mountpoint()
        # wait for bup to finish
        try:
            os.wait()
        except OSError as e:
            if e.errno == errno.ECHILD:  # no child process
                pass
            else:
                raise
        if os.path.ismount(m):
            logging.debug("umounting %s" % m)
            if not self.call(["umount", m]):
                logging.warning("failed to umount %s" % m)
        logging.debug("removing directory %s" % m)
        try:
            os.removedirs(m)
        except:  # noqa
            pass
        device = self.device()
        try:
            # --force is required to avoid confirmation
            cmd = ["lvremove", "--force", device]
            if self.verbose <= 0:
                cmd += ["--quiet"]
            if self.verbose >= 3:
                cmd += ["--verbose"]
            if stat.S_ISBLK(os.stat(device).st_mode):
                logging.debug("dropping snapshot %s" % device)
                if not self.call(cmd):
                    logging.warning("failed to drop snapshot %s" % device)
        except OSError:
            # normal: the device doesn't exist, moving on
            return


if sys.platform.startswith("cygwin"):

    class VssSnapshot(Snapshot):
        """Handle VSS snapshot, under Cygwin"""

        shadow_id = None
        winpath = None

        def __enter__(self):
            if " " in self.mountpattern:
                raise ValueError("mountpattern cannot contain a space")
            device, fs_root = self.find_device()
            if self.create_snapshot(device):
                self.mount(fs_root)
            else:
                logging.warning(
                    """failed to create snapshot for %s, skipping snapshotting"""
                    % self.path
                )
            return self

        def cleanup(self, force=False):
            if self.shadow_id is not None:
                device = self._convert2dos(self.src_path)
                logging.debug("dropping snapshot on %s" % device)
                if self.call(["vshadow", "-ds=%s" % self.shadow_id]):
                    self.shadow_id = None
                    self.exits = False
                else:
                    logging.warning("failed to drop snapshot %s" % device)
            if os.path.exists(self.mountpattern):
                self._fail_if_mounted()
                logging.debug("removing directory %s" % self.mountpattern)
                os.rmdir(self.mountpattern)

        def _convert_path(self, path, spec):
            return subprocess.check_output(["cygpath", spec, path]).replace("\n", "")

        def _convert2dos(self, linux_path):
            return self._convert_path(linux_path, "-aw")

        def _convert2linux(self, dos_path):
            return self._convert_path(dos_path, "-a").rstrip("/")

        def create_snapshot(self, device):
            self.cleanup(True)
            try:
                logging.debug("creating snapshot on %s" % device)
                # Note: Windows XP does not supports permanent shadows (-p)
                output = subprocess.check_output(["vshadow", "-p", device])
                # * SNAPSHOT ID = {5a698842-f325-404a-83e7-6a7fa08760a1}
                self.shadow_id = re.search(
                    r"\* SNAPSHOT ID = (\{[0-9A-Fa-f-]{36}\})", output
                ).group(1)
                logging.debug("shadow copy created: %s" % self.shadow_id)
                self.exists = True
                return True
            except Exception as e:
                logging.warning("vss snapshot failed, id=%s: %s" % self.shadow_id, e)
                return False

        def _fail_if_mounted(self):
            """throw if the mount point is already used

            The only way to unmount a mounted shadow copy is to erase
            it, so leave that decision to the user.
            """
            output = subprocess.check_output(["vshadow", "-q"])
            winmount = self._convert2dos(self.mountpattern).replace("\\", "\\\\")
            mounted = re.search(
                r"^   - Exposed locally as: (%s)." % winmount,
                output,
                re.MULTILINE | re.IGNORECASE,
            )
            if mounted is not None:
                raise AlreadyMountedException(winmount)

        def find_device(self):
            # XXX: handle devices that are mounted through a directory.
            self.winpath = self._convert2dos(self.path)
            device = self.winpath[0:2]
            return (device, self._convert2linux(device))

        def mount(self, fs_root):
            """mountpattern must be a path in linux format"""
            logging.debug("making sure mountpoint %s exists" % self.mountpattern)
            if make_dirs_helper(self.mountpattern):
                logging.debug("mountpoint %s created" % self.mountpattern)
            winmount = self._convert2dos(self.mountpattern)
            if len(winmount) == 3:  # if it is a drive letter,
                winmount = winmount[:-1]  # remove the trailing backslash
            if self.call(["vshadow", "-el=%s,%s" % (self.shadow_id, winmount)]):
                self.path = self.path.replace(fs_root, self.mountpattern)
            else:
                logging.warning(
                    """failed to mount snapshot %s on %s, skipping snapshotting"""
                    % (self.shadow_id, self.mountpattern)
                )
                self.cleanup(True)


class Bup:
    """helper to call bup's operations

    The methods assume that BUP_DIR is set."""

    @staticmethod
    def init(remote_rep):
        logging.info("initializing bup's dir %s" % quote(os.environ["BUP_DIR"]))
        cmd = ["bup", "init"]
        if remote_rep:
            cmd += ["-r", remote_rep]
        return global_logger.check_call(cmd)

    @staticmethod
    def clear_index():
        logging.info("clearing the index")
        return global_logger.check_call(["bup", "index", "--clear"])

    @staticmethod
    def fsck(remote_rep, parity=False, repair=False):
        base_cmd = ["bup", "fsck"]
        if remote_rep:
            # XXX: maybe bup-fsck could learn to work on remote repository
            addr, path = remote_rep.split(":")
            base_cmd = ["ssh", addr, "bup", "-d", path, "fsck"]

        if global_logger.verbose >= 3:
            base_cmd += ["--verbose"]

        if parity:
            cmd = base_cmd + ["--par2-ok"]
            if not global_logger.check_call(cmd):
                logging.warning(
                    "bup reports par2(1) as not working,no recovery blocks written"
                )
                return False
            cmd = base_cmd + ["--generate"]
            logging.info("generating par2(1) recovery blocks")
        elif repair:
            cmd = base_cmd + ["--repair"]
            logging.info("repairing repository")
        else:  # this is --check
            # XXX: always use --quick for now
            cmd = base_cmd + ["--quick"]
            logging.info("verifying bup repository")
        return global_logger.check_call(cmd)

    @staticmethod
    def index(
        path, excludes, excludes_rx, excludes_from, excludes_rx_from, one_file_system
    ):
        logging.info("indexing %s" % quote(path))
        # XXX: should be -q(uiet) unless verbose > 0 - but bup
        # index has no -q
        cmd = ["bup", "index"]
        if global_logger.verbose >= 3:
            cmd += ["--verbose"]
        if excludes:
            cmd += map((lambda ex: "--exclude=" + ex), excludes)
        if excludes_rx:
            cmd += map((lambda ex: "--exclude-rx=" + ex), excludes_rx)
        if excludes_from:
            cmd += map((lambda ex: "--exclude-from=" + ex), excludes_from)
        if excludes_rx_from:
            cmd += map((lambda ex: "--exclude-rx-from=" + ex), excludes_rx_from)
        if one_file_system:
            cmd += ["--one-file-system"]
        cmd += [path]
        return global_logger.check_call(cmd)

    @staticmethod
    def save(paths, branch, graft, remote_rep):
        logging.info("saving %s" % quotes(paths))
        cmd = ["bup", "save"]
        if global_logger.verbose <= 0:
            cmd += ["--quiet"]
        elif global_logger.verbose >= 3:
            cmd += ["--verbose"]
        if remote_rep:
            cmd += ["-r", remote_rep]
        cmd += ["--name", branch]
        if "=" in graft:
            cmd += ["--graft", graft]
        else:
            cmd += ["--strip-path", graft]
        #  -t and -c are apparently useful in case of disaster;
        # unfortunately, they are useless if we don't show or log the output
        if global_logger.verbose >= 2:
            cmd += ["--tree", "--commit"]
        cmd += paths
        return global_logger.check_call(cmd)


class Pidfile:
    """this class is designed to be used with the "with" construct

    it will create an exclusive lockfile, detect existing ones and
    remove stale files (with invalid pids or that the process
    disappeared)

    it will also cleanup after itself

    this shouldn't have been implemented and should instead use the
    more common lockfile package:

    https://pypi.python.org/pypi/lockfile
    """

    def __init__(self, path):
        """setup various parameters"""
        self.pidfile = path

    def __enter__(self):
        """wrapper around create() to work with the 'with' statement"""
        return self.create()

    def __exit__(self, t, e, tb):
        """remove the pid file, unless we detected another process"""
        # return false to raise, true to pass
        if t is None:
            # normal condition, no exception
            self.remove()
            return True
        elif t is ProcessRunningException:
            # do not remove the other process lockfile
            return False
        else:
            # other exception
            if self.pidfd:
                # this was our lockfile, removing
                self.remove()
            return False

    def create(self):
        """initialise pid file"""
        try:
            logging.debug("locking pidfile %s" % self.pidfile)
            self.pidfd = os.open(self.pidfile, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
        except OSError as e:
            if e.errno == errno.EEXIST:
                pid = self._check()
                if pid:
                    self.pidfd = None
                    raise ProcessRunningException(self.pidfile, pid)
                else:
                    try:
                        os.remove(self.pidfile)
                        logging.warning("removed stale lockfile %s" % (self.pidfile))
                        self.pidfd = os.open(
                            self.pidfile, os.O_CREAT | os.O_WRONLY | os.O_EXCL
                        )
                    except OSError as e:
                        if e.errno == errno.EACCES:
                            # we can't write to the file, most likely
                            # we weren't able to deliver the signal
                            # because it's running as a different user
                            # play it safe and abort
                            with open(self.pidfile, "r") as f:
                                raise ProcessRunningException(self.pidfile, f.read())
            else:
                raise

        os.write(self.pidfd, str(os.getpid()).encode())
        os.close(self.pidfd)
        return self

    def remove(self):
        """helper function to actually remove the pid file"""
        logging.debug("removing pidfile %s" % self.pidfile)
        os.remove(self.pidfile)

    def _check(self):
        """check if a process is still running

        the process id is expected to be in pidfile, which should
        exist.

        if it is still running, returns the pid, if not, return
        False.

        this assumes we have privileges to send a signal to that
        process, but if we can't we're likely to be unable to
        overwrite the pidfile anyways."""
        with open(self.pidfile, "r") as f:
            try:
                pidstr = f.read()
                pid = int(pidstr)
            except ValueError:
                # not an integer
                logging.debug("not an integer: %s" % pidstr)
                return False

            # First check the proc filesystem, which may not be available.
            if os.path.exists("/proc/%d" % pid):
                return pid

            try:
                os.kill(pid, 0)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    # Not running
                    logging.debug("process %d is not running" % pid)
                    return False
                elif e.errno == errno.EPERM:
                    # No permission to signal this process!
                    logging.debug("can't deliver signal to process %d" % pid)
                    return pid
            else:
                return pid


class AlreadyMountedException(Exception):
    def __init__(self, path):
        """override parent constructor to keep path"""
        self.path = path
        return Exception.__init__(
            self,
            """
A snapshot is already mounted at that location:
  %s
Use "vshadow -q | grep -iB9 '%s'" to identify it and
use "vshadow -ds={SNAPSHOT_ID}" to unmount it and *erase* it."""
            % (path, path),
        )


class ProcessRunningException(Exception):
    """an exception yielded by the Pidfile class when a process is
    detected using the pid file"""

    def __init__(self, path, pid):
        """override parent constructor to keep path and pid"""
        self.path = path
        self.pid = pid
        return Exception.__init__(
            self, "process already running in %s as pid %s" % (path, pid)
        )


class GlobalLogger(object):
    """convenient executer with support for logging as well

        ERROR: things that make us exit
        WARNING: we show only errors (default)
        INFO: broad steps of what is happening ("saving", "fsck"...), same as -v
        DEBUG: we explain each step and display commands, same as -vv and -vvv

        we try to be mostly silent by default, and terse when we talk,
    even in info and debug
    """

    def __init__(self, args=None):
        """initialise the singleton, only if never initialised"""
        self.verbose = args.verbose
        self._log = args.logfile
        self._warn = sys.stderr

        # setup python logging facilities
        if args.syslog:
            sl = logging.handlers.SysLogHandler(address="/dev/log")
            sl.setFormatter(logging.Formatter("bup-cron[%(process)d]: %(message)s"))
            # convert syslog argument to a numeric value
            loglevel = getattr(logging, args.syslog.upper(), None)
            if not isinstance(loglevel, int):
                raise ValueError("Invalid log level: %s" % loglevel)
            sl.setLevel(loglevel)
            logging.getLogger("").addHandler(sl)
            logging.debug("configured syslog level %s" % loglevel)
        # log everything in main logger
        logging.getLogger("").setLevel(logging.DEBUG)
        if args.logfile == sys.stdout or args.logfile == "/dev/stdout":
            sh = logging.StreamHandler()
            if args.verbose > 1:
                sh.setLevel(logging.DEBUG)
            elif args.verbose > 0:
                sh.setLevel(logging.INFO)
            else:
                sh.setLevel(logging.WARNING)
            self._log = sh.stream
            logging.getLogger("").addHandler(sh)
            logging.debug("configured stdout level %s" % sh.level)
        else:
            # keep 52 weeks of logs
            fh = logging.handlers.TimedRotatingFileHandler(
                args.logfile, when="W6", backupCount=52
            )
            # serve back the stream to other processes
            self._log = fh.stream
            logging.getLogger("").addHandler(fh)
            logging.debug(
                "configured file output to %s, level %s" % (args.logfile, fh.level)
            )

    def check_call(self, cmd):
        """call a process, log it to the logfile

        return false if it fails, otherwise true"""
        try:
            logging.debug("calling command `%s`" % " ".join(cmd))
            if self.verbose >= 2:
                stdout = self._log
            else:
                stdout = subprocess.DEVNULL
            subprocess.check_call(cmd, stdout=stdout, stderr=self._warn, close_fds=True)
        except subprocess.CalledProcessError:
            logging.warning("command failed")
            return False
        return True


class Timer(object):
    """this class is to track time and resources passed"""

    def __init__(self):
        """initialize the timstamp"""
        self.stamp = datetime.datetime.now()

    def times(self):
        """return a string designing resource usage"""
        return "user %s system %s chlduser %s chldsystem %s" % os.times()[:4]

    def diff(self):
        """a datediff between the creation of the object and now"""
        return datetime.datetime.now() - self.stamp

    def __str__(self):
        """return a string representing the time passed and resources used"""
        return "elasped: %s (%s)" % (str(self.diff()), self.times())


_quotable = re.compile(r"\s")  # Any white space char (respects UNICODE)


def quote(str):
    """Quote str if it contains white spaces"""
    if _quotable.search(str):
        return "'" + str + "'"
    return str


def quotes(parts):
    """Quote the individual strings in parts if it contains white spaces and
    return them as a single string"""
    return " ".join(quote(p) for p in parts)


def make_dirs_helper(path):
    """Create the directory if it does not exist

    Return True if the directory was created, false if it was already
    present, throw an OSError exception if it cannot be created"""
    try:
        os.makedirs(path)
        return True
    except OSError as ex:
        if ex.errno != errno.EEXIST or not os.path.isdir(path):
            raise
        return False


class BupCronMetaData(object):
    """class to store metadata about a backup run"""

    du_cmd = ["du", "-bs", "--exclude=*.midx", "--exclude=*.bloom", "--exclude=*.par2"]

    def __init__(self, remote=None):
        self.remote = remote
        self.sizes = []
        self.versions()
        self.disk_usage()

    def versions(self):
        self.local_bup = (
            subprocess.check_output(["bup", "--version"]).decode().rstrip("\n")
        )
        git_output = subprocess.check_output(["git", "--version"]).decode().rstrip("\n")
        self.local_git = re.match(r"git version (.*)", git_output).group(1)
        self.local_python = platform.python_version()
        if self.remote:
            server, repo_path = self.remote.split(":")
            cmd = "bup --version ;" "git --version ;" "python --version 2>&1"
            cmd = ["ssh", "-T", server, cmd]
            logging.debug("calling command `%s`" % cmd)
            bup, git, python = subprocess.check_output(cmd).decode().split("\n", 2)
            self.remote_bup = bup
            self.remote_git = re.match(r"git version (.*)", git).group(1)
            self.remote_python = re.match(r"Python (.*)", python).group(1)

    def disk_usage(self):
        if not self.remote:
            obj_path = os.path.join(os.environ["BUP_DIR"], "objects/pack")
            cmd = self.du_cmd + [obj_path]
        else:
            server, repo_path = self.remote.split(":")
            obj_path = os.path.join(repo_path, "objects/pack")
            cmd = ["ssh", "-T", server, " ".join(self.du_cmd), "'%s'" % obj_path]
        logging.debug("calling command `%s`" % cmd)
        self.sizes.append(int(subprocess.check_output(cmd).decode().split("\t")[0]))

    @staticmethod
    def format_bytes(num, suffix="B"):
        """format the given number as a human-readable size

        inspired by http://stackoverflow.com/a/1094933/1174784"""
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return "%3.1f%s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%3.1f%s%s" % (num, "Yi", suffix)

    def __str__(self):
        size_diff = self.sizes[-1] - self.sizes[-2]
        str = """Repository size

* Before: %s (%s bytes)
* After: %s (%s bytes)
* Diff: %s (%s bytes)

Local versions

*    bup: %s
*    git: %s
* python: %s
""" % (
            self.format_bytes(self.sizes[-2]),
            self.sizes[-2],
            self.format_bytes(self.sizes[-1]),
            self.sizes[-1],
            self.format_bytes(size_diff),
            size_diff,
            self.local_bup,
            self.local_git,
            self.local_python,
        )
        if self.remote:
            str += """
Remote versions

*    bup: %s
*    git: %s
* python: %s
""" % (
                self.remote_bup,
                self.remote_git,
                self.remote_python,
            )
        return str

    def last_diff(self):
        size_diff = self.sizes[-1] - self.sizes[-2]
        return "repository size change: %s" % self.format_bytes(size_diff)

    def summary(self):
        size_diff = self.sizes[-1] - self.sizes[0]
        str = (
            "total repository size (before/after/diff): %s/%s/%s (%s/%s/%s), version (bup/git/python): %s/%s/%s"
            % (
                self.format_bytes(self.sizes[0]),
                self.format_bytes(self.sizes[-1]),
                self.format_bytes(size_diff),
                self.sizes[0],
                self.sizes[-1],
                size_diff,
                self.local_bup,
                self.local_git,
                self.local_python,
            )
        )
        if self.remote:
            str += ", remote versions (bup/git/python): %s/%s/%s" % (
                self.remote_bup,
                self.remote_git,
                self.remote_python,
            )
        return str

    def save(self):
        self.disk_usage()
        # We must use a temporary file otherwise the EOL are not written
        # correctly in the note.
        if not self.remote:
            cmd = [
                "git",
                "--git-dir",
                os.environ["BUP_DIR"],
                "notes",
                "add",
                "-F",
                "-",
                self.branch,
            ]
        else:
            server, repo_path = self.remote.split(":")
            cmd = [
                "ssh",
                "-T",
                server,
                "git --git-dir='{0}' notes add -F - '{1}'".format(
                    repo_path, self.branch
                ),
            ]
        logging.debug("calling command `%s`" % cmd)
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        (out, err) = process.communicate(str(self).encode())
        if process.returncode != 0:
            logging.warning(
                "failed to save bup note: `%s%s` (%d)" % (out, err, process.returncode)
            )
        return process.returncode == 0


def process(args):
    """main processing loop"""
    success = True
    if args.stats:
        args.stats = BupCronMetaData(args.remote)
    # current lvm object to cleanup in exception handlers
    for path in args.paths:
        with Snapshot.select(args.snapshot)(
            path,
            args.size,
            logging.info,
            logging.warning,
            global_logger.verbose,
            global_logger.check_call,
            args.mountpoint,
        ) as snapshot:
            # XXX: this shouldn't be in the loop like this, bup index should be
            # able to index multiple paths
            #
            # unfortunately, `bup index -x / /var` skips /var...
            if not Bup.index(
                snapshot.path,
                args.exclude,
                args.exclude_rx,
                args.exclude_from,
                args.exclude_rx_from,
                True,
            ):
                logging.error("Skipping save because index failed!")
                success = False
                continue

            if args.branch_name:
                branch = args.branch_name
            else:
                branch = "%s-%s" % (
                    args.name if args.name else socket.gethostname(),
                    snapshot.src_path.replace("/", "_"),
                )
            if not Bup.save([snapshot.path], branch, snapshot.path, args.remote):
                logging.error("bup save failed on %s" % snapshot.path)
                success = False

            if args.check and not Bup.fsck(args.remote, repair=args.repair):
                # it could have found an error and fixed it, check again
                # XXX: we could check if fsck returns 100 (which means
                # success) but that would mean refactoring all of
                # check_call()
                if not Bup.fsck(args.remote):
                    logging.warning(
                        "fsck determined there was an error and could not fix it"
                    )
                    success = False

            if args.parity and not Bup.fsck(args.remote, parity=True):
                logging.warning("could not generate par2 parity blocks")

            if args.stats:
                args.stats.branch = branch
                args.stats.save()
                logging.info(args.stats.last_diff())

    if args.stats:
        logging.info(args.stats.summary())
    return success


def bail(status, timer, msg=None):
    """cleanup on exit"""
    if msg:
        logging.warning(msg)
    logging.info("bup-cron %s completed, %s" % (__version__, timer))
    sys.exit(status)


def main():
    """main entry point, sets up error handlers and parses arguments"""

    global global_logger

    locale.setlocale(locale.LC_ALL, "")
    args = ArgumentConfigParser().parse_args()
    timer = Timer()

    # initialize GlobalLogger singleton
    global_logger = GlobalLogger(args)

    logging.info("bup-cron %s starting" % __version__)
    try:
        initialised = False
        if not os.path.exists(os.environ["BUP_DIR"]):
            if not Bup.init(args.remote):
                bail(3, timer, "failed to initialize bup repo")
            initialised = True

        with Pidfile(args.pidfile):
            if args.clear and not initialised:
                if not Bup.clear_index():
                    logging.warninging("failed to clear the index")

            success = process(args)
    except SystemExit:
        return
    except:  # noqa
        raise
        # Get exception type and error, but print the traceback in debug only.
        t, e, b = sys.exc_info()
        if args.debug:
            logging.warning(traceback.print_tb(b))
        bail(2, timer, "aborted with unhandled exception %s: %s" % (t.__name__, e))

    if success:
        bail(0, timer)
    else:
        bail(1, timer, "one or more backups failed to complete")


if __name__ == "__main__":
    main()
