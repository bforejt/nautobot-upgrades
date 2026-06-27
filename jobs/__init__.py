"""Job registration for the Cisco IOS-XE upgrade Git repository.

Nautobot only imports the ``jobs`` module of a Git repository, so every Job class
must be registered here (see Nautobot issue #5971 — Jobs structured purely as
submodules are not discovered unless registration is wired through
``jobs/__init__.py``).
"""

from nautobot.apps.jobs import register_jobs

from .iosxe_upgrade import IOSXEUpgrade
from .register_image import RegisterImage

register_jobs(IOSXEUpgrade, RegisterImage)
