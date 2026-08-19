"""Job registration for the Cisco IOS-XE upgrade Git repository.

Nautobot only imports the ``jobs`` module of a Git repository, so every Job class
must be registered here (see Nautobot issue #5971 — Jobs structured purely as
submodules are not discovered unless registration is wired through
``jobs/__init__.py``).
"""

import os

from nautobot.apps.jobs import register_jobs

from .c9800_upgrade import C9800Upgrade
from .cancel_run import CancelUpgradeRun
from .constants import DEVTOOLS_ENV, JOB_VERSION
from .iosxe_upgrade import IOSXEUpgrade
from .register_image import RegisterImage

__version__ = JOB_VERSION

_jobs = [C9800Upgrade, CancelUpgradeRun, IOSXEUpgrade, RegisterImage]
if os.environ.get(DEVTOOLS_ENV, "").strip().lower() in ("1", "true", "yes"):
    # Dev tooling is OPT-IN PER ENVIRONMENT: the RESTCONF Dev Tester is a
    # bench instrument (docs/devtools.md), not part of the upgrader. With
    # the flag unset the module is never imported, so production instances
    # never list the job at all.
    from .restconf_dev_tester import RestconfDevTester

    _jobs.append(RestconfDevTester)

register_jobs(*_jobs)
