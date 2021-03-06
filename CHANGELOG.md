bup-cron (2.0)

  * initial support for Python 3.7+
    * requires bup 0.30+, which has preliminary support for Python 3
  * repository cleanup

 -- Bas Stottelaar <basstottelaar@gmail.com> Sat, 25 Apr 2020 12:00:00 +0200

bup-cron (1.3)

  * logging output improvements:
   * display longer stats only one per run, and only the diff per path
   * silence a bunch of messages in 'info' mode, explain a policy
   * do not always log the (generally empty) git note output
  * arguments can now be passed to bup during tests
  * remove hand-made wrapper script, now that setuptools does it for us

 -- Antoine Beaupré <anarcat@debian.org>  Fri, 12 Dec 2014 17:18:49 -0500

bup-cron (1.2)

  * display the proper process name in syslog (closes #2)
  * log metadata as well as saving it to a git note
  * proper return code and debugging for note stats save (closes #3)
  * Fix exclude_rx not working, thanks alFReD-NSH

 -- Antoine Beaupré <anarcat@debian.org>  Mon, 01 Dec 2014 22:37:36 -0500

bup-cron (1.1)

  * make this a package installable as bup_cron on PyPI
  * run fsck and fail when it doesn't succeed - fsck supports both --check
    and --repair
  * properly return failure to the shell if key components fail
  * --parity test
  * various fixes and improvements to the test suite

 -- Antoine Beaupré <anarcat@debian.org>  Sun, 09 Nov 2014 17:49:30 -0500

bup-cron (1.0)

  * first official release, result of around 6 months of intermittent
    development
  * features LVM and VSS snapshot support, syslog and file logging, remote
    repositories, exclude patterns, --clear and --parity, filing stats as
    git notes

 -- Antoine Beaupré <anarcat@debian.org>  Wed, 05 Nov 2014 23:19:18 -0500

