# Code guide: packet-loss detection and rerouting

This guide explains every program in the project, what it does, and how the pieces fit together. Read it in the order given: the network first, then the controller, then the sender and receiver, then the batch experiments, then the live demo.

## 1. How the pieces fit together

```
          h1 (sender, 10.0.0.1)                      h2 (receiver, 10.0.0.2)
          sender.py                                   receiver.py
              |  DATA (UDP 9000, DSCP marked on s1)        ^
              v                                             |
            s1 ---- primary: s2 ---- s4 ------------------ h2
              \                        ^
               \---- alternate: s3 ---/
              |
   controller (10.0.0.254, root namespace, on s4 port 4)
      pathguard.py  <-- flow stats + probes + REPORT (UDP 9100) from receiver
      run_controller.py  (starts pathguard under OS-Ken, listens on TCP 6633)
```

Two kinds of traffic and two kinds of information move through the network:

- **Data path:** sender.py sends numbered UDP datagrams to h2. s1 marks them with a DSCP value and forwards them along the active path. The receiver counts them per path using that mark.
- **Control information:** the receiver reports its counts to the controller, and the controller sends probe packets down both paths. The controller also reads s1's flow counters. From these it decides whether the active path is losing packets and whether to switch.

Two more programs sit outside the network: `live_demo.py` (an interactive session that runs everything above and lets you inject faults) and `dashboard.py` (a live view of the measurements). The batch scripts `run_experiment.py`, `run_validation.sh`, `analyze.py` and `summarize.py` do the same job non-interactively, for repeated runs.

## 2. topo.py: the network

`topo.py` defines the Mininet topology: two hosts and four switches.

- **Hosts:** `h1` at 10.0.0.1 and `h2` at 10.0.0.2.
- **Switches:** `s1`, `s2`, `s3`, `s4`, each running OpenFlow 1.3.
- **Links:** h1–s1, s1–s2, s2–s4 (primary), s1–s3, s3–s4 (alternate), s4–h2.

Two environment variables control how it runs:

- `MN_DATAPATH`: `kernel` by default, which needs the openvswitch kernel module. `user` uses the userspace datapath, which works on VMs without that module.
- `MN_NETEM`: when set to `0`, link delays are removed. Otherwise the primary links get 3 ms and the alternate links get 5 ms, using `tc netem`.

The controller depends on the port numbers. Mininet numbers switch ports in the order links are added, so the file lists them explicitly:

| Switch | Port 1 | Port 2 | Port 3 | Port 4 |
|---|---|---|---|---|
| s1 | h1 | s2 (primary) | s3 (alternate) | – |
| s2 | s1 | s4 | – | – |
| s3 | s1 | s4 | – | – |
| s4 | s2 | s3 | h2 | controller |

`run_experiment.py` and `live_demo.py` check these numbers at startup, so a wrong wiring fails immediately rather than producing silent errors.

*Correction to make:* the file's docstring calls the controller interface a "NAT interface". It is not NAT. It is a host in the root network namespace, which lets the controller process receive the receiver's reports.

## 3. run_controller.py: starting the controller

This is a launcher for the OS-Ken SDN framework. It does four things:

1. Patches the Python threading library so OS-Ken's green threads work with normal sockets (`hub.patch`).
2. Reads its configuration and sets the OpenFlow listening port to 6633, which is where the switches connect.
3. Loads two components: OS-Ken's own OpenFlow handler, and `pathguard`, the application in section 4.
4. Starts them and waits.

`PATHGUARD_STATIC=1` and `PATHGUARD_EVENTS=<file>` are read by `pathguard.py` itself, so they are passed through the environment when this program is started.

## 4. pathguard.py: the controller application

This is the core of the rerouting logic. It is an OS-Ken application, and the framework calls its methods when switches connect, when statistics arrive, and when ports change.

### 4.1 Constants (the tuning values)

| Name | Value | Meaning |
|---|---|---|
| `POLL` | 0.5 s | how often the controller measures |
| `WINDOW` | 8 polls (4 s) | loss is averaged over this many polls |
| `LAG` | 2 polls (1 s) | the newest polls are skipped, because packets sent in them may not have arrived yet |
| `MIN_SAMPLES` | 24 | a loss estimate needs at least this many packets in the window |
| `THRESH` | 0.20 | loss above 20% counts as a bad poll |
| `CONSEC` | 4 polls (2 s) | bad polls in a row before rerouting |
| `PROBE_WINDOW` | 8 polls | probes are averaged over this many polls |
| `PROBE_OK` | 0.8 | a path is healthy if at least 80% of its probes arrive |
| `FAILBACK_AFTER` | 6 s | minimum time on the alternate path before switching back |
| `PRIO_BASE` | 10 | priority of the normal data flow on s1 |
| `PRIO_REROUTE` | 200 | priority of the reroute flows, which override the normal ones |
| `DSCP` | P=10, A=20 | the DSCP mark that identifies each path |

### 4.2 Startup: `__init__`

The controller keeps several dictionaries:

- `active`: which path data uses now (`'P'` or `'A'`).
- `sent` and `rx`: cumulative packet counts per path. `sent` comes from s1's flow counters. `rx` comes from the receiver's reports.
- `hist` and `phist`: per poll, the packets sent and received in that poll, for data and for probes.
- `link_down`: whether each path's port is reported down.

It opens the event log, then starts two background loops: `report_listener` (receives the receiver's reports) and `monitor_loop` (polls every 0.5 s).

### 4.3 Switch setup: `on_features`

When a switch connects, this method installs its forwarding rules at priority 5, and s1 gets an extra rule:

- **s1:** the rule at priority 10 matches UDP traffic to h2 port 9000, sets the DSCP mark to 10, and forwards to port 2 (primary). This is the rule that marks data for the primary path. Everything else to h2 goes to port 2, and everything to h1 goes to port 1.
- **s2 and s3:** forward traffic to h2 out port 2, and to h1 out port 1.
- **s4:** forwards traffic to h2 out port 3, to h1 out port 1, and to the controller at 10.0.0.254 out port 4.

`add_flow` builds an OpenFlow flow-add (or delete) message and sends it to the switch.

### 4.4 Counting: `on_flow_stats` and `report_listener`

- **`on_flow_stats`** handles s1's flow statistics. It reads the packet counter of the priority-10 flow (primary) and the priority-200 flow (alternate). A counter that goes down means the flow was deleted and re-added, so the code handles that case by counting from zero again. The result is the number of packets s1 has sent on each path.
- **`report_listener`** listens on UDP 9100 for the receiver's JSON reports. It stores, per path, how many data packets h2 has received (`rx`) and how many probes it has received (`probe_rx`).

The difference between what s1 sent and what h2 received is the loss. This is why the meter fault shows up: the meter drops packets after s1 has counted them.

### 4.5 Probes: `send_probe`

Every poll, the controller sends one probe packet down each path. It builds an Ethernet/IP/UDP packet from the controller's address to h2 on port 9002, and sets the DSCP mark for the path being tested. The probe is sent as a *packet-out* message through s1 to the chosen port, so it goes down the path without the controller having to install a flow for it.

The receiver counts probes by DSCP on port 9002 and reports them. If a path is broken or heavily degraded, few probes arrive on that path.

### 4.6 The monitor loop: `monitor_loop` and `poll`

Every 0.5 s, `poll()` runs:

1. Ask s1 for its flow statistics.
2. Send one probe down each path.
3. For each path, work out how many packets were sent, received, probed and probe-received *since the last poll*, and add that pair to the history.
4. Call `decide()`.

Keeping per-poll differences rather than totals makes the loss calculation a sliding window.

### 4.7 Loss and health: `settled`, `loss`, `healthy`

- **`settled(hist, n)`** returns the `n` polls that ended `LAG` polls ago. The lag avoids judging packets that are still in flight.
- **`loss(path)`** adds up sent and received over the settled window and returns `1 − received/sent`. It returns `None` if fewer than 24 packets were sent, because a small sample is unreliable.
- **`healthy(path)`** returns `False` if the port is reported down. Otherwise it checks the probes over the settled probe window: the path is healthy if at least 80% of the probes arrived. If there is not enough history yet, it returns `True`, which means a new path is assumed healthy until shown otherwise.

### 4.8 The decision: `decide()`

This is the rerouting rule, and it runs every poll:

1. Compute loss on the active path and on the other path. Log a `poll` event with both values, the probe health and the raw counters.
2. If the controller is in static mode, stop here. Monitoring continues, but nothing is changed.
3. A poll is bad if the active path's loss is above 20%. Count consecutive bad polls. On the fourth one, log `degraded`.
4. Once there are `CONSEC` (4) bad polls in a row:
   - If the other path is healthy, switch to it (`set_path`, reason `loss`).
   - If not, log `no_alternate` and reset the count, so the controller doesn't keep trying.
5. Otherwise, if the controller is on the alternate path, has been there for at least 6 s, the primary's probes are settled and healthy, switch back (`set_path`, reason `failback`).

### 4.9 Link events: `on_port_status`

When OpenFlow reports that a port on s1 has gone down or come back up, this method updates `link_down` for that path and logs it. If the active path's port has gone down, it reroutes immediately, without waiting for the loss window to fill. That is why a broken link is detected in milliseconds, while gradual loss takes about 4 seconds.

### 4.10 Switching paths: `set_path`

This is where the reroute actually happens:

1. Record the new active path, the time of the switch, and reset the bad-poll count.
2. Clear the history for the new path, so its loss window starts fresh.
3. Log a `reroute` or `failback` event.
4. On s1, install a priority-200 flow for UDP to h2 port 9000. For the alternate path, this flow sets DSCP 20 and outputs on port 3. For the primary path, it deletes the priority-200 flow, so the normal priority-10 flow takes over again.
5. On s4, do the same for the ACKs coming back from h2 to h1 (UDP port 9001). For the alternate path, ACKs go out port 2 toward s3. This keeps the return path on the same side of the network as the data.
6. Log `flowmod_sent`.

The result is that both data and ACKs move together, and the receiver sees the new DSCP mark on arrivals, which is how the dashboard detects "first packet on alternate."

## 5. sender.py: the sending side (h1)

The sender implements the reliable protocol on top of UDP. It runs on h1.

### 5.1 Wire formats

Every message starts with a one-byte type field, so the receiver can tell message kinds apart:

| Type | Name | Sent by | Layout | Purpose |
|---|---|---|---|---|
| 0 | DATA | sender | `!BIddB` (type, seq, first-send time, this-send time, attempt) | a numbered datagram |
| 1 | ACK | receiver | `!BIH` (type, cumulative seq, count) then count × seq | everything up to `cum` has arrived; the listed seqs arrived out of order |
| 2 | NACK | receiver | `!BH` (type, count) then count × seq | these seqs are missing |
| 4 | HELLO | sender | `!B` | session request |
| 5 | WELCOME | receiver | `!B` | session accepted |

`!` means network byte order. The sender includes the first-send time in each DATA packet, so the receiver can report latency without any extra exchange.

### 5.2 Arguments

The main options are: `--rate` (datagrams per second, default 8), `--duration` (seconds of new traffic, default 60), `--rto` (base retransmission timeout, default 3 s), `--max-retry` (default 5), `--drain` (how long to wait for ACKs after sending stops, default 10 s), `--events` (where to write the event log), and `--status-every` (print a status line every N seconds, used by the live demo).

### 5.3 Session setup: the HELLO handshake

Before any data, the sender sends HELLO every 0.5 s until WELCOME arrives, for up to 15 s. Each attempt is logged. Once WELCOME is received, the session is established and data starts.

This is the application-level stand-in for the connection setup that TCP does. UDP itself has no such step.

### 5.4 Sending data

For each new datagram:

- Assign the next sequence number and record its first-send time.
- Add it to `unacked` with a retry count of 0.
- Send it with `transmit()`, which packs the DATA header and pads the datagram to the payload size (64 bytes by default).

Sending is paced: each send is scheduled `1/rate` seconds after the last one, so the rate stays steady even if the loop is delayed.

### 5.5 Retransmission

There are two triggers:

1. **Timeout:** each unacknowledged packet has a timer. If `now − last send > min(8 s, rto × 1.4^retries)`, the packet is retransmitted. With the defaults, the waits are 3 s, 4.2 s, 5.9 s, 8 s, 8 s.
2. **NACK:** when a NACK arrives listing a missing seq, the packet is retransmitted immediately. This is ignored if the packet was sent less than 0.5 s earlier, so one burst of NACKs does not cause repeated copies.

`retransmit()` increments the retry count, sends a new copy with `attempt` set to the retry number, and logs it. Once a packet has been retried `max-retry` (5) times, it is abandoned and counted in the statistics.

### 5.6 Processing ACKs and NACKs

- An **ACK** removes everything up to the cumulative seq from `unacked`, and removes the individual seqs listed in the selective-ACK part.
- A **NACK** triggers retransmission as described above.

Malformed datagrams are ignored, using the `struct.error` check.

### 5.7 Ending

The loop stops when new sending has finished and nothing is unacknowledged, or when the hard stop (sending time plus drain) is reached. The sender then writes `sender_log.json` with its statistics and the first-send time of every sequence number. The analysis scripts read this file.

## 6. receiver.py: the receiving side (h2)

The receiver runs on h2. It has three jobs: accept the session, reconstruct the data stream, and report what it sees.

### 6.1 Sockets

- A **data socket** on UDP 9000 receives DATA and HELLO messages. It has the IP_RECVTOS option turned on, so the kernel attaches the IP header's TOS byte to each datagram. The receiver reads the DSCP from this byte (`recv_with_dscp`). This is how it knows which path a packet took.
- A **probe socket** on UDP 9002 receives the controller's probes, also with DSCP.
- An **outgoing socket** sends ACKs, NACKs, WELCOME and reports. It uses an ephemeral port.

`select()` waits on the two incoming sockets at once, so neither blocks the other.

### 6.2 Handling messages

- **HELLO:** send WELCOME back to the sender's ACK port and log it.
- **DATA:** parse the header and log an `rx` event with the sequence number, attempt and DSCP. Count it per path.
  - If the sequence number has been seen before, count a duplicate and stop.
  - If it is new, mark it as received and count it as delivered. If the number is higher than any seen so far, every number in between is a *gap*. Each gap records the time it was first noticed, and a `gap` event is logged.
  - Advance the cumulative pointer `cum` while the next number is present. `cum` is the highest number such that everything up to it has arrived.
- **Probes:** count them per DSCP.

### 6.3 Sending ACKs, NACKs and reports

Four things happen on a timer:

- **ACK every 0.5 s:** the cumulative pointer, plus up to 24 selectively acknowledged numbers above it (the ones received out of order).
- **NACK:** for each missing number between `cum` and the highest seen, a NACK is sent once the gap is at least 0.25 s old (the reorder wait), and again every 1.8 s until it arrives. At most 16 numbers go in one NACK.
- **Report every 0.5 s:** a JSON message to the controller at 10.0.0.254:9100 with cumulative counts per path, probe counts and the highest sequence seen.
- **Status line every 2 s** (live demo only): delivered count, missing count, duplicates, and arrivals per path.

The 0.25 s reorder wait exists because packets can arrive slightly out of order on a healthy network. Without the wait, the receiver would NACK packets that were merely delayed.

### 6.4 Ending

When the duration runs out, the receiver writes `receiver_log.json`, which includes every arrival with its sequence number, attempt, DSCP and timestamps. The analysis reads this.

## 7. run_experiment.py: one batch run

This script runs one complete experiment without the live interface. It assumes `run_controller.py` is already running in another terminal.

Steps:

1. Build the topology (`topo.py`), connect to the controller on port 6633, and check the port numbers.
2. Configure the controller's address on the root-namespace host (10.0.0.254), and turn off checksum offload on the hosts, because the virtual Ethernet devices otherwise leave UDP checksums unfinished for the switch.
3. Add a static ARP entry on each host for the controller, so the reports reach it.
4. Start `receiver.py` on h2, wait one second, then start `sender.py` on h1.
5. Wait `--fault-at` seconds (default 15), then inject the fault:
   - `loss`: `tc netem` loss on the primary link (needs sch_netem).
   - `meter`: an OpenFlow meter on s2 that drops packets above a rate. This is the fallback when netem isn't available, and it is what the live demo uses.
   - `down`: take the primary link down.
   - `none`: no fault, for the baseline.
6. Record the fault time in `fault.json`, wait for both processes to finish, and write `run_info.json`.

Each run writes its logs into its own folder.

## 8. run_validation.sh: the full set of experiments

This shell script runs every fault in both SDN and static mode, several times each. For each run it:

1. Stops any old controller and cleans Mininet state (the order matters, because `mn -c` also kills controllers).
2. Starts a fresh controller with `PATHGUARD_STATIC` set for the mode and its own event log.
3. Runs `run_experiment.py`.
4. Stops the controller and runs `analyze.py` on the folder.

At the end it runs `summarize.py`. The defaults are: 3 repeats, 60 seconds of sending, a fault at 15 seconds, and the fault list `none meter7 meter3 down`. Environment variables override each of these.

## 9. analyze.py: metrics for one run

This reads the sender, receiver, fault and controller logs from one folder and writes `metrics.json`. The definitions it uses are written at the top of the file, and the important ones are:

- **First-attempt loss in a window:** of the packets first sent in that window, the share whose first copy never arrived. This is the loss the controller reacts to.
- **Loss before, during and after:** windows of [start, fault), [fault, fault + 15 s), and [fault + 15 s, end of sending).
- **Detect:** time from the fault to the first `degraded` event.
- **Reroute:** time from the fault to the first `reroute` event.
- **First on alternate:** time from the fault to the first arrival marked DSCP 20 after the reroute.
- **Backlog cleared:** time at which the last packet that was lost on its first copy during the fault window finally arrived.
- **Overhead:** retransmissions divided by packets sent.
- **Goodput after:** packets delivered per second after the fault window.

## 10. summarize.py: averages across repeats

This reads every `metrics.json`, groups the runs by mode and fault (folder names like `sdn_meter3_r1`), and prints the average of each measure per group. Averages skip runs where a value is missing.

## 11. Live demo: live_demo.py, live_metrics.py, dashboard.py

### 11.1 live_demo.py

This is the interactive session. Its `start()` method does, in order:

1. Clear the session folder and write a `start` mark to `session.jsonl`.
2. Start the controller as a subprocess, with its event log in the session folder, and wait 3 seconds.
3. Build the same topology as the batch script, connect the switches, check the ports, and configure the controller host and ARP.
4. Start the receiver on h2, wait one second, then start the sender on h1. Both write their event logs.
5. Start a background thread that prints controller events as they happen.
6. Wait until the handshake and first data have arrived, then wait 6.5 seconds for the controller's probe window to fill.

Then it runs the command loop. Each command that changes the network also writes a mark to `session.jsonl`, which the metrics use to know when faults and repairs happened:

- `break` and `restore` take the primary link down and up.
- `meter <pps>` and `meter off` apply and remove the loss fault.
- `loss <pct>` applies netem loss through `tc`. It calls `tc` directly, because Mininet's own method does not report failure.
- `show setup` prints the programs, addresses, links, sockets, handshake times and OpenFlow rules.
- `status` and `report` print the current numbers.
- `demo` runs the eight-step walkthrough, pausing for Enter at each step. Step 5 applies the meter fault and waits for a reroute. Step 6 waits for the backlog to clear. Step 7 removes the meter and waits for failback. Step 8 waits for the fault window to close, then prints the results.

On `quit`, `stop()` shuts down the sender, receiver and controller, stops Mininet, and writes `report.txt`.

### 11.2 live_metrics.py

This computes the same measures as `analyze.py`, but from the logs while the session is still running, so the dashboard can show them. The differences from `analyze.py` are:

- It reads the session marks from `session.jsonl`, instead of `fault.json`.
- It reports a window as **final** only once the 15-second window has closed plus a 3-second allowance. Before that, the loss values are live, because packets still in transit would otherwise count as lost.
- It builds the sequence window (the last 48 numbers, marked as first-time arrival, retransmitted, missing, or not yet sent) and the merged event log.

It can also be run on its own to print the metrics for a folder.

### 11.3 dashboard.py

This redraws a text screen twice a second from `live_metrics.compute()`. It shows the topology with the active path marked, the controller state, the fault, the packet counters, the loss windows, the recovery clock, the sequence window and the event log. It only reads files, so it can be stopped and restarted without affecting the session. Ctrl+C leaves the last frame on screen, which is how the screenshots were taken.

## 12. The files the project writes

| File | Written by | Contains |
|---|---|---|
| `controller_events.jsonl` | pathguard.py | start, poll, degraded, reroute, failback, no_alternate, port_status, flowmod_sent |
| `sender_events.jsonl` | sender.py (live demo) | hello, welcome, tx, ack, nack, abandon, done |
| `receiver_events.jsonl` | receiver.py (live demo) | listen, welcome, rx, gap, ack, nack |
| `session.jsonl` | live_demo.py | start, phase, fault, clear |
| `sender_log.json` | sender.py | statistics, first-send time of every sequence number |
| `receiver_log.json` | receiver.py | every arrival, counts per path |
| `fault.json`, `run_info.json` | run_experiment.py | fault type and time, run arguments |
| `metrics.json` | analyze.py | the measures in section 9 |
| `report.txt` | live_demo.py | the results summary |

## 13. Things to correct before the presentation

These are inconsistencies I found while reading the code. None of them changes the results, but a careful examiner could notice them.

1. **Docstring mismatch in pathguard.py.** The docstring says rerouting happens on loss above 20% "for two consecutive polls." The code uses `CONSEC = 4`, which is four polls, or two seconds. The code and the README are correct; the docstring should say four polls.
2. **Repeat count.** `run_validation.sh` defaults to 3 repeats. The report describes 2 repeats. Make them match.
3. **"Three fixed seeds" claim.** The live lab page says runs use fixed seeds so the numbers can be reproduced. The code contains no random numbers at all, so there are no seeds. Remove that claim.
4. **"NAT interface" wording** in topo.py's docstring (see section 2).
5. **Scalability note for the report.** The receiver checks every missing number between `cum` and the highest seen, on every loop. If one packet is never recovered, `cum` stops advancing and this list grows. For 60-second runs this is harmless, but it is a real limitation worth stating.
6. **Loss counts the meter's victims.** The controller counts packets when they leave s1, before the meter drops them. So loss is measured at the bottleneck, which is the intended behaviour, but explain it in the report so nobody assumes the sender is at fault.
7. **Static mode still logs `poll` events** with the full loss figures, but never reroutes. This is intended, because the baseline shows what the controller sees without acting.

## 14. Running each part

- **Full experiment set (batch):** `sudo ./run_validation.sh`
- **One batch run:** start `sudo python3 run_controller.py` in one terminal, then `sudo python3 run_experiment.py --out results/test --fault meter --meter-pps 3` in another, then `python3 analyze.py results/test`.
- **Interactive demo:** `sudo python3 live_demo.py`, with `python3 dashboard.py results/live` in a second terminal. Type `demo`.
- **Static baseline:** add `--static` to the `live_demo.py` command.
