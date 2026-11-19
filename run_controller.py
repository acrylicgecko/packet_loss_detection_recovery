#!/usr/bin/env python3
"""run_controller.py - starts OS-Ken with the pathguard app.

OS-Ken's own manager command is not installed everywhere, so this launcher does
the same job: it loads the OpenFlow handler and pathguard.py and listens for
switches on TCP port 6633 (the port run_experiment.py connects to).

Usage (from the repo directory, in its own terminal):
    sudo python3 run_controller.py                 # SDN rerouting on
    sudo PATHGUARD_STATIC=1 python3 run_controller.py   # static baseline
"""
from os_ken.lib import hub

hub.patch(thread=True)  # green threads and sockets, as OS-Ken's own manager does

from os_ken import cfg  # noqa: E402
from os_ken.base import app_manager  # noqa: E402  (must come before controller.* imports)

CONF = cfg.CONF
CONF(args=[], project='os_ken', default_config_files=[])
CONF.set_override('ofp_tcp_listen_port', 6633)


def main():
    mgr = app_manager.AppManager.get_instance()
    mgr.load_apps(['os_ken.controller.ofp_handler', 'pathguard'])
    contexts = mgr.create_contexts()
    services = mgr.instantiate_apps(**contexts)
    hub.joinall(services)


if __name__ == '__main__':
    main()
