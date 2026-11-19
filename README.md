# Packet Loss Detection and Recovery (UDP + SDN + Mininet)

A UDP application that detects packet loss with sequence numbers, recovers the lost
packets with NACK and timeout retransmission, and cooperates with an OS-Ken SDN
controller that moves traffic to a second path when the first path degrades or fails.

## Repository layout

| File | Runs on | Purpose |
|---|---|---|
| `topo.py` | Mininet | Two-path topology (primary via s2, alternate via s3) |
| `sender.py` | h1 | UDP sender: sequence numbers, ACK/NACK handling, retransmission |
| `receiver.py` | h2 | UDP receiver: gap detection, ACK/NACK, per-path counts, reports to the controller |
| `pathguard.py` | OS-Ken app | Base forwarding, path monitoring (flow counters + probes), dynamic rerouting, failback |
| `run_controller.py` | host shell | Starts OS-Ken with `pathguard.py` on TCP 6633 |
| `run_experiment.py` | host shell (root) | Builds the topology, runs sender and receiver, injects one fault, saves logs |
| `analyze.py` | host shell | Computes loss and recovery metrics for one run (`metrics.json`) |
| `run_validation.sh` | host shell (root) | Runs every fault in SDN and static mode, then summarises |
| `summarize.py` | host shell | Averages `metrics.json` over repeats |

## Requirements

Ubuntu 22.04 or 24.04 (a VM is fine), root access, Python 3.

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iproute2 ethtool python3-pip
pip3 install -r requirements.txt      # os-ken
```

Recommended: the kernel module `sch_netem` (used by `tc netem` for packet loss). It is in
the `linux-modules-extra` package on some kernels. If it is missing, the `meter` faults
still work (see below). If Open vSwitch cannot use its kernel datapath, set
`MN_DATAPATH=user` before running the experiments.

Link delays (3 ms primary, 5 ms alternate) also use `tc netem`. If `sch_netem` is missing,
set `MN_NETEM=0` to build the same topology without delays. Port numbers and path roles are
unchanged. Without netem, `loss` cannot run; use the `meter` faults instead.
The sandbox used for the test runs needed `MN_DATAPATH=user MN_NETEM=0`.

If a run cannot connect the switches to the controller, `run_experiment.py` stops with an error
and `runner.out` says which switches are missing. Check `runner.out` before you trust a run.

## Run one experiment

Terminal 1 (controller, keep it running):

```bash
cd <repo>
sudo PATHGUARD_EVENTS=results/demo/controller_events.jsonl python3 run_controller.py
```

Terminal 2 (experiment):

```bash
sudo python3 run_experiment.py --out results/demo --fault loss --loss 60 --duration 60 --fault-at 15
python3 analyze.py results/demo
```

Faults:

| `--fault` | What it does |
|---|---|
| `none` | No fault (control run; checks for false reroutes) |
| `loss` | `tc netem` loss of `--loss` % on both directions of s1-s2 (needs `sch_netem`) |
| `meter` | OpenFlow meter on s2 that passes only `--meter-pps` packets/s (fallback; drops most traffic) |
| `down` | Link s1-s2 goes down (`configLinkStatus`) |

To run the static baseline (no rerouting), restart the controller with `PATHGUARD_STATIC=1`
and keep everything else the same.

## Run all scenarios

```bash
sudo OUT=results/validation REPEATS=3 ./run_validation.sh
```

This runs `none meter7 meter3 down` in `sdn` and `static` mode, 3 repeats each, and prints a
summary table. Set `FAULTS="none loss60 down"` to include netem loss. Each run takes about
70 seconds.

## What each run writes

| File | Contents |
|---|---|
| `sender_log.json` | Sender counters and the first-send time of every sequence number |
| `receiver_log.json` | Every data arrival (time, seq, path mark, attempt number) and probe counts |
| `fault.json` | Time and type of the injected fault |
| `controller_events.jsonl` | Controller events: polls with loss estimates, degradation, reroute, failback, port status |
| `metrics.json` | Metrics computed by `analyze.py` (definitions in the docstring of `analyze.py`) |

## How it works

**Sequence tracking and recovery.** Each DATA datagram carries `type, seq, first-send time,
this-send time, attempt`. The receiver keeps the set of sequence numbers it has, the highest
seen, and the cumulative pointer. A hole below the highest seq is a gap; it is NACKed after a
0.25 s reorder wait and re-NACKed every 1.8 s. ACKs carry the cumulative pointer and up to 24
selective seqs every 0.5 s. The sender retransmits on NACK and on timeout (3.0 s × 1.4ⁿ, cap
8 s) and abandons a packet after 5 retries.

**Path marking.** Switch s1 writes DSCP 10 on data sent via the primary path and DSCP 20 on
data sent via the alternate path. The receiver reads the DSCP with `IP_RECVTOS`, so it can
count arrivals per path.

**Path monitoring.** Every 0.5 s the controller:
1. asks s1 for flow statistics. The primary flow's counter is what left s1 towards s2; the
   alternate flow's counter is what left towards s3;
2. injects one probe per path (packet-out at s1, with the path's DSCP) and counts probe
   arrivals at h2;
3. receives the receiver's report of arrivals per DSCP every 0.5 s.

Loss on the active path = 1 − arrivals / sent over the last 4 s, skipping the newest 1 s (those
packets may still be in flight). A loss estimate is used only with at least 24 packets in the
window. A path is healthy when at least 80 % of its probes in the last 4 s arrived.

**Rerouting.** Two consecutive polls above 20 % loss, and the other path healthy, cause a
reroute. A PORT_STATUS link-down on the active primary port causes an immediate reroute. A
reroute installs priority-200 flows: s1 sends data out port 3 with DSCP 20, and s4 sends ACKs
back out port 2 (towards s3). Failback to the primary path happens after 6 s on the alternate
path if the primary probes are healthy; it removes the priority-200 flows.

**Base forwarding.** Every switch has priority-5 flows for the 10.0.0.1 and 10.0.0.2 hosts
(and 10.0.0.254 on s4). Priority-10 data flows on s1 mark DSCP 10 and send to the primary.

## Port map (checked at start-up by `run_experiment.py`)

    s1: port 1 = h1, port 2 = s2 (primary), port 3 = s3 (alternate)
    s2: port 1 = s1, port 2 = s4
    s3: port 1 = s1, port 2 = s4
    s4: port 1 = s2, port 2 = s3, port 3 = h2, port 4 = controller address (10.0.0.254)

## Notes and limits

- The controller's report address (10.0.0.254) is a root-namespace node, so the OS-Ken process
  can receive reports from h2 without running inside Mininet.
- Hosts have checksum offload disabled (`ethtool -K ... tx off rx off`). Without that, the
  switch forwards UDP datagrams with unfinished checksums and the receiver drops them.
- Loss estimates come from counters, not from packet traces, so they are approximate. The
  receiver's reports arrive in batches; the 4-second window and 1-second lag absorb that.
- Alternate-path health is judged from probes only while it is not carrying traffic. Probes are
  small, so their estimates are coarse.
- If the `meter` fault is used, probes to h2 through s2 are dropped too, as they would be with
  netem. The `loss` fault applies to both directions of s1-s2, as the report describes.

## Live demo (separate from the HTML lab)

`live_demo.py` is an interactive terminal session on the same Mininet topology. You start traffic,
then inject and remove faults while it runs, and the controller's decisions are printed as they happen.
It does not use the HTML page, and the page does not use it.

Terminal (as root, with `MN_DATAPATH=user MN_NETEM=0` on VMs without openvswitch or sch_netem):

```bash
sudo MN_DATAPATH=user MN_NETEM=0 python3 live_demo.py            # SDN rerouting on
sudo MN_DATAPATH=user MN_NETEM=0 python3 live_demo.py --static   # baseline: controller only observes
```

| Command | Effect |
|---|---|
| `status` | sender and receiver counters, last controller poll |
| `break` / `restore` | take the primary link s1-s2 down / up |
| `meter <pps>` / `meter off` | OpenFlow meter on s2 (fault used in the report) |
| `loss <pct>` | netem loss on s1-s2 (needs sch_netem; `loss 0` clears it) |
| `quit` | stop and keep the logs in `results/live/` |

The session waits about 10 s after start before accepting commands, so the controller has
filled its probe window and can judge the alternate path. The `sender.py` and `receiver.py`
files gained a `--status-every` option, which is off by default and used only by the live demo.
`run_validation.sh` and `run_experiment.py` behave as before.

## Guided demo with a live dashboard

Two terminals, both in this folder, both as root (`sudo`).

Terminal 1, the network (with `MN_DATAPATH=user MN_NETEM=0` on VMs without openvswitch or sch_netem):

```bash
sudo MN_DATAPATH=user MN_NETEM=0 python3 live_demo.py
```

Terminal 2, the dashboard (read-only, can be started and stopped at any time):

```bash
python3 dashboard.py results/live
```

In terminal 1, type `demo`. It runs eight steps and waits for Enter between them:

1. **Set up the network:** programs, addresses, links with interface names, UDP sockets (`ss`), OpenFlow rules.
2. **Connection:** UDP has no connection setup, so the sender sends HELLO and the receiver answers WELCOME. Data starts only after that.
3. **Path viability:** the controller probes both paths before any fault.
4. **Traffic and sequence tracking:** datagrams are numbered; the receiver detects gaps; baseline loss is measured.
5. **Fault:** `meter 3` limits the primary path to 3 packets/s. The controller detects the loss and reroutes.
6. **Recovery:** the receiver sends NACKs for missing sequence numbers, the sender retransmits them, and the backlog clears.
7. **Repair:** the meter is removed and the controller fails back to the primary path.
8. **Results:** loss before, during and after the fault, and recovery times. Saved to `results/live/report.txt`.

Other commands: `show setup`, `status`, `report`, `break` / `restore` (link failure instead of loss), `meter <pps>` / `meter off`, `loss <pct>` (netem only), `quit`.

Files written in `results/live/`: `session.jsonl` (phase, fault and repair marks), `sender_events.jsonl` (hello, welcome, tx, ack, nack, abandon), `receiver_events.jsonl` (welcome, rx, gap, ack, nack), `controller_events.jsonl` (poll, degraded, reroute, failback), and `report.txt`.

Measurement notes: the "during 15 s" loss is labelled live until the window closes plus 3 s, because copies still in transit would otherwise count as lost. The guided results step waits for that before printing.
