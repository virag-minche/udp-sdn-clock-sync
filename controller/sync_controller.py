#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
Network Time Synchronization Project - Ryu SDN Controller
================================================================================
Course: Computer Networks Mini Project
Target Environment:
  - OS: Ubuntu 22.04 LTS (Linux kernel 5.15+)
  - Python: 3.8 - 3.10
  - Ryu Framework: 4.34
  - Eventlet: 0.30.2
  - Open vSwitch (OVS): 2.17+
  - OpenFlow Protocol: 1.3
  - Mininet: 2.3.0

Topology Overview:
  [Client Hosts h1..hN] \
                         [s1 (dpid=1)] === (Primary Link: s1-eth2 <-> s2-eth2, 10 Mbps) === [s2 (dpid=2)] --- [Time Server h_server: 10.0.0.100]
  [Load Gen h_loadgen]  /               === (Backup Link:  s1-eth3 <-> s2-eth3, 10 Mbps) ===               --- [Load Sink h_sink]

Traffic Classes:
  1. Time Sync Traffic:
     - Transport: UDP port 9999
     - QoS Marking: DSCP 46 (Expedited Forwarding / EF, binary 101110)
     - Bidirectional: Client -> Server (dst_port 9999) & Server -> Client (src_port 9999)
     - Target Queue: Queue 1 (High priority / guaranteed low-latency QoS queue)
  2. Background / Load Traffic:
     - Transport: UDP port 5201 (e.g. iperf3), DSCP 0 (Best Effort)
     - Normal Mode Target Queue: Queue 2 (Best effort queue)
     - Congested Mode Target Queue: Queue 3 (Throttled / ratelimited queue)

Execution Examples:
  1. Basic startup (Default: priority OFF, dynamic monitoring passive):
     $ ryu-manager controller/sync_controller.py

  2. Startup with QoS Priority enabled:
     $ ryu-manager controller/sync_controller.py --priority

  3. Startup with Dynamic Congestion Handling enabled:
     $ ryu-manager controller/sync_controller.py --dynamic

  4. Startup with both Priority and Dynamic Congestion Handling:
     $ ryu-manager controller/sync_controller.py --priority --dynamic

REST API Usage Examples (Port 8080):
  1. Check SDN Controller Status:
     curl -X GET http://127.0.0.1:8080/sdn/status

  2. Toggle Priority and Dynamic Modes at Runtime:
     curl -X POST http://127.0.0.1:8080/sdn/mode \
          -H "Content-Type: application/json" \
          -d '{"priority": "on", "dynamic": "on"}'

  3. Submit Client Time Synchronization Quality Metrics:
     curl -X POST http://127.0.0.1:8080/sync/report \
          -H "Content-Type: application/json" \
          -d '{"client_id": "h1", "rtt_ms": 4.25, "offset_ms": 0.35, "error_ms": 1.2, "timestamp": 1697116800.0}'
================================================================================
"""

import os
import sys
import time
import json
import csv
from datetime import datetime

# Eventlet / Ryu hub green-thread primitives
from ryu.lib import hub

# Core Ryu controller classes
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, udp, arp
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response
from ryu import cfg

# ==============================================================================
# CONFIGURATION CONSTANTS
# ==============================================================================

# OpenFlow Switch DPIDs
DPID_S1 = 1  # Switch 1: Client side
DPID_S2 = 2  # Switch 2: Server side

# Inter-Switch Port Mapping (matches Mininet topology port assignment)
# Primary Link: s1-eth1 <-> s2-eth1 (OpenFlow Port 1)
# Backup Link:  s1-eth2 <-> s2-eth2 (OpenFlow Port 2)
PORT_S1_PRIMARY = 1
PORT_S1_BACKUP = 2
PORT_S2_PRIMARY = 1
PORT_S2_BACKUP = 2

# Network Addresses and Ports
TIME_SERVER_IP = "10.0.0.100"
SYNC_UDP_PORT = 9999
BG_UDP_PORT = 5201
DSCP_EF = 46  # Expedited Forwarding (Binary 101110, decimal 46)

# OpenFlow Flow Priorities
# Higher numerical value indicates higher evaluation priority in Flow Table 0
PRIORITY_SYNC_HIGH = 100    # Time sync traffic (QoS Queue 1)
PRIORITY_BG_DEFAULT = 50    # Background traffic (QoS Queue 2 or Queue 3)
PRIORITY_LEARNED_L2 = 20    # Standard L2 learned unicast forwarding
PRIORITY_TABLE_MISS = 0     # Catch-all table miss (Send to controller)

# QoS Queue IDs (Configured in OVS linux-htb QoS / tc)
QUEUE_SYNC_PRIORITY = 1     # High priority QoS queue (Guaranteed bandwidth, low latency)
QUEUE_BG_NORMAL = 2         # Best effort QoS queue
QUEUE_BG_THROTTLED = 3      # Constrained queue for congested state

# Bottleneck Link Configuration (10 Mbps primary inter-switch link)
BOTTLENECK_BW_BPS = 10.0 * 1000 * 1000  # 10 Mbps in bits per second

# Dynamic Congestion Thresholds & Hysteresis
CONGESTION_HIGH_UTIL_PCT = 80.0  # Utilization threshold to trigger congestion mode (>80%)
CONGESTION_LOW_UTIL_PCT = 50.0   # Utilization threshold to recover from congestion (<50%)
CONGESTION_HYSTERESIS_SAMPLES = 3 # Consecutive samples needed to change state

# Application-Aware Sync Quality Thresholds
SYNC_ERROR_ESCALATE_THRESH_MS = 20.0  # Error > 20ms triggers QoS escalation
SYNC_ERROR_ESCALATE_SAMPLES = 5       # 5 consecutive high error reports
SYNC_ERROR_RELAX_THRESH_MS = 5.0      # Error < 5ms recovers escalation
SYNC_ERROR_RELAX_SAMPLES = 5          # 5 consecutive good error reports

# Flow Timeouts (seconds)
FLOW_IDLE_TIMEOUT = 60
FLOW_HARD_TIMEOUT = 0

# Monitoring Interval (seconds)
STATS_MONITOR_INTERVAL = 1.0

# Log File Paths (relative to runtime working directory)
RESULTS_DIR = "results"
PORT_STATS_CSV = os.path.join(RESULTS_DIR, "port_stats.csv")
CONTROLLER_LOG_CSV = os.path.join(RESULTS_DIR, "controller_log.csv")

# REST API Instance Name for Ryu WSGI
SYNC_CONTROLLER_INSTANCE_NAME = "sync_controller_api_app"

# Command-line configuration flags for Ryu
CONF = cfg.CONF
CONF.register_cli_opts([
    cfg.BoolOpt("priority", default=False, help="Enable strict QoS priority for sync traffic at startup"),
    cfg.BoolOpt("dynamic", default=False, help="Enable dynamic congestion mitigation at startup"),
])


# ==============================================================================
# REST API CONTROLLER (ryu.app.wsgi ControllerBase)
# ==============================================================================

class SyncRestController(ControllerBase):
    """
    REST API handler exposing endpoints for network monitoring, runtime mode
    configuration, and application-aware sync health telemetry.
    """

    def __init__(self, req, link, data, **config):
        super(SyncRestController, self).__init__(req, link, data, **config)
        self.sync_app = data[SYNC_CONTROLLER_INSTANCE_NAME]

    @route("sync", "/sdn/status", methods=["GET"])
    def get_sdn_status(self, req, **kwargs):
        """
        GET /sdn/status
        Returns current controller operational mode, congestion status,
        recent port utilization, and active client telemetry.
        """
        status_data = self.sync_app.get_system_status()
        body = json.dumps(status_data, indent=2)
        return Response(content_type="application/json; charset=utf-8", body=body)

    @route("sync", "/sdn/mode", methods=["POST"])
    def set_sdn_mode(self, req, **kwargs):
        """
        POST /sdn/mode
        JSON Payload: {"priority": "on"|"off", "dynamic": "on"|"off"}
        Dynamically adjusts QoS classification and flow table entries.
        """
        try:
            payload = json.loads(req.body.decode("utf-8"))
        except Exception as e:
            return Response(status=400, content_type="application/json",
                            body=json.dumps({"error": f"Invalid JSON payload: {str(e)}"}))

        result = self.sync_app.update_operational_mode(payload)
        return Response(content_type="application/json; charset=utf-8",
                        body=json.dumps(result, indent=2))

    @route("sync", "/sync/report", methods=["POST"])
    def post_sync_report(self, req, **kwargs):
        """
        POST /sync/report
        JSON Payload: {
          "client_id": "h1",
          "rtt_ms": 3.82,
          "offset_ms": 0.12,
          "error_ms": 0.45,
          "timestamp": 1697116800.0
        }
        Records synchronization metrics from end-hosts and evaluates
        app-aware QoS escalation triggers.
        """
        try:
            payload = json.loads(req.body.decode("utf-8"))
        except Exception as e:
            return Response(status=400, content_type="application/json",
                            body=json.dumps({"error": f"Invalid JSON payload: {str(e)}"}))

        required_fields = ["client_id", "rtt_ms", "offset_ms", "error_ms", "timestamp"]
        for field in required_fields:
            if field not in payload:
                return Response(status=400, content_type="application/json",
                                body=json.dumps({"error": f"Missing required field: '{field}'"}))

        result = self.sync_app.process_client_sync_report(payload)
        return Response(content_type="application/json; charset=utf-8",
                        body=json.dumps(result, indent=2))


# ==============================================================================
# MAIN RYU SDN CONTROLLER APPLICATION
# ==============================================================================

class SyncController(app_manager.RyuApp):
    """
    OpenFlow 1.3 SDN Controller for Network Time Synchronization Project.

    Core Responsibilities:
      1. Baseline L2 Learning Switch with loop avoidance across redundant links.
      2. Priority classification for bidirectional UDP 9999 / DSCP 46 time sync packets.
      3. Periodic OpenFlow statistics polling and link utilization calculation.
      4. Closed-loop dynamic congestion mitigation with hysteresis.
      5. Application-aware QoS escalation informed by end-host sync precision telemetry.
      6. Automatic OpenFlow link-failure detection and rapid failover/failback.
      7. Persistent structured CSV logging for network metrics and controller events.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SyncController, self).__init__(*args, **kwargs)

        # Operational Modes (configurable via CLI flags or REST API)
        self.priority_mode = CONF.priority
        self.dynamic_mode = CONF.dynamic
        self.congested_state = False
        self.app_escalated_state = False

        self.logger.info(
            "Initializing SyncController: Priority Mode = %s, Dynamic Congestion = %s",
            self.priority_mode, self.dynamic_mode
        )

        # Connected OpenFlow switches (datapath objects keyed by DPID)
        self.datapaths = {}

        # MAC Learning Table: {dpid: {mac_address: in_port}}
        self.mac_to_port = {}

        # STP-like Loop Prevention & Link State Management:
        # Default active inter-switch link is Primary (s1:2 <-> s2:2).
        # Backup ports (s1:3, s2:3) are strictly BLOCKED in software to avoid L2 loops.
        self.primary_link_up = True
        self.active_is_port = {
            DPID_S1: PORT_S1_PRIMARY,
            DPID_S2: PORT_S2_PRIMARY,
        }
        self.blocked_ports = {
            DPID_S1: {PORT_S1_BACKUP},
            DPID_S2: {PORT_S2_BACKUP},
        }

        # Port Statistics Tracking:
        # {dpid: {port_no: {'tx_bytes': int, 'rx_bytes': int, 'timestamp': float}}}
        self.prev_port_stats = {}

        # Hysteresis counters for dynamic congestion detection
        self.high_util_streak = 0
        self.low_util_streak = 0

        # Application-Aware Sync Telemetry:
        # {client_id: {'reports': [list], 'high_error_streak': int, 'low_error_streak': int}}
        self.client_reports = {}

        # Initialize Results Directory and CSV Log Files
        self._init_csv_logs()

        # Register REST API Handler
        wsgi = kwargs.get("wsgi")
        if wsgi:
            wsgi.register(SyncRestController, {SYNC_CONTROLLER_INSTANCE_NAME: self})
            self.logger.info("Registered REST API handler at http://127.0.0.1:8080")

        # Spawn Background Monitoring Green Thread (Eventlet / Ryu hub)
        self.monitor_thread = hub.spawn(self._stats_monitor_loop)

        # Log Controller Initialization Event
        self._log_decision_event(
            "CONTROLLER_INIT",
            f"Controller started (priority_mode={self.priority_mode}, dynamic_mode={self.dynamic_mode})"
        )

    # --------------------------------------------------------------------------
    # CSV LOGGING SUBSYSTEM
    # --------------------------------------------------------------------------

    def _init_csv_logs(self):
        """Initializes output directories and CSV files with proper header schemas."""
        if not os.path.exists(RESULTS_DIR):
            try:
                os.makedirs(RESULTS_DIR, exist_ok=True)
            except Exception as e:
                self.logger.error("Failed to create results directory %s: %s", RESULTS_DIR, e)

        # 1. Port Statistics CSV
        if not os.path.exists(PORT_STATS_CSV):
            with open(PORT_STATS_CSV, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "dpid", "port", "rx_bytes", "tx_bytes",
                    "throughput_mbps", "utilization_pct"
                ])

        # 2. Controller Decisions and Events CSV
        if not os.path.exists(CONTROLLER_LOG_CSV):
            with open(CONTROLLER_LOG_CSV, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "event_type", "details"])

    def _log_decision_event(self, event_type, details):
        """Appends a timestamped controller decision event to controller_log.csv."""
        ts_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with open(CONTROLLER_LOG_CSV, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([ts_iso, event_type, details])
            self.logger.info("[EVENT %s] %s", event_type, details)
        except Exception as e:
            self.logger.error("Error writing to %s: %s", CONTROLLER_LOG_CSV, e)

    def _log_port_stats(self, dpid, port_no, rx_bytes, tx_bytes, throughput_mbps, util_pct):
        """Appends link utilization and throughput statistics to port_stats.csv."""
        ts_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with open(PORT_STATS_CSV, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    ts_iso, dpid, port_no, rx_bytes, tx_bytes,
                    f"{throughput_mbps:.4f}", f"{util_pct:.2f}"
                ])
        except Exception as e:
            self.logger.error("Error writing to %s: %s", PORT_STATS_CSV, e)

    # --------------------------------------------------------------------------
    # OPENFLOW SWITCH LIFECYCLE & BASELINE FORWARDING
    # --------------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        """Tracks active switches joining or leaving the OpenFlow domain."""
        datapath = ev.datapath
        dpid = datapath.id
        if ev.state == MAIN_DISPATCHER:
            if dpid not in self.datapaths:
                self.logger.info("Switch registered: DPID=%s", dpid)
                self.datapaths[dpid] = datapath
                self.mac_to_port.setdefault(dpid, {})
                # Ensure correct blocked ports state
                if dpid == DPID_S1:
                    self.blocked_ports[dpid] = {PORT_S1_BACKUP if self.primary_link_up else PORT_S1_PRIMARY}
                elif dpid == DPID_S2:
                    self.blocked_ports[dpid] = {PORT_S2_BACKUP if self.primary_link_up else PORT_S2_PRIMARY}
        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                self.logger.warning("Switch disconnected: DPID=%s", dpid)
                self.datapaths.pop(dpid, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Invoked upon completion of the OpenFlow 1.3 handshake.
        Installs Table-Miss flow entry (Priority 0) that forwards unclassified
        frames to the controller via OFPActionOutput(OFPP_CONTROLLER).
        """
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})
        self.logger.info("Configuring Switch Features for DPID=%s", dpid)

        # Delete all pre-existing flows to ensure a clean state upon reconnection
        self._clear_all_flows(datapath)

        # Install Table-Miss Entry (Priority 0):
        # Matches any packet (empty match), executes OFPActionOutput(OFPP_CONTROLLER)
        # buffer_id=OFP_NO_BUFFER instructs OVS to send the full packet payload to Ryu
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(
            datapath=datapath,
            priority=PRIORITY_TABLE_MISS,
            match=match,
            actions=actions,
            idle_timeout=0,
            hard_timeout=0
        )

        self._log_decision_event(
            "SWITCH_INITIALIZED",
            f"Installed table-miss rule on DPID {dpid}"
        )

        # Install high-priority drop rules on blocked backup ports to prevent L2 flood loops in OVS
        self._install_blocked_port_drops(datapath)

        # If priority mode is already active, install QoS classification flows
        if self.priority_mode:
            self._install_qos_flows(datapath)

    def _install_blocked_port_drops(self, datapath):
        """
        Installs high-priority (500) drop rules in OVS Table 0 for any currently
        blocked standby port on this switch so that broadcast/flood frames sent
        via OFPP_FLOOD are immediately dropped at the standby ingress in kernel space.
        """
        parser = datapath.ofproto_parser
        dpid = datapath.id
        for bport in self.blocked_ports.get(dpid, set()):
            match = parser.OFPMatch(in_port=bport)
            self._add_flow(datapath, priority=500, match=match, actions=[])

    def _clear_all_flows(self, datapath):
        """
        Removes all table entries from the switch using OFPFlowMod with OFPFC_DELETE.
        Preserves controller table-miss rule reinstantiation.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )
        datapath.send_msg(mod)

    def _add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        """
        Helper method to construct and send an OpenFlow 1.3 OFPFlowMod message.
        Applies instructions containing the specified actions.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout
        )
        datapath.send_msg(mod)

    # --------------------------------------------------------------------------
    # PACKET-IN & L2 LEARNING SWITCH WITH LOOP PREVENTION
    # --------------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Handles packets punted to the controller when no flow table match occurs.
        Implements MAC address learning and loop avoidance.

        LOOP AVOIDANCE EXPLANATION:
        --------------------------
        In topologies with redundant physical links (e.g. primary and backup links
        between s1 and s2), unmanaged broadcast frames (such as ARP requests) can
        cause catastrophic forwarding loops / broadcast storms.
        This controller enforces an active-standby topology model:
        1. At any given instant, only ONE inter-switch port is operational (active),
           while the alternate inter-switch port is BLOCKED.
        2. Any packet arriving on a currently blocked port is immediately dropped
           at the controller level without learning the MAC or installing flows.
        3. Broadcast/flooding packets (OFPP_FLOOD) are only emitted on non-blocked ports,
           guaranteeing loop-free spanning tree semantics without running full STP.
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]
        dpid = datapath.id

        # 1. Loop Prevention: Drop packets ingressing on blocked backup ports
        if in_port in self.blocked_ports.get(dpid, set()):
            # Silently drop packet from blocked port
            return

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        # Ignore LLDP / BDDP topology discovery packets
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst_mac = eth.dst
        src_mac = eth.src

        self.mac_to_port.setdefault(dpid, {})

        # 2. Learn source MAC address and ingress port
        self.mac_to_port[dpid][src_mac] = in_port

        # 3. Determine egress port based on destination MAC
        if dst_mac in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst_mac]
        else:
            # Destination unknown: Flood to all active, non-blocked ports
            out_port = ofproto.OFPP_FLOOD

        # 4. Define actions and install flow rule if unicast target is resolved
        actions = []

        if out_port != ofproto.OFPP_FLOOD:
            # Check if this frame is Time Sync Traffic (UDP 9999, DSCP 46)
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            udp_pkt = pkt.get_protocol(udp.udp)
            is_sync = False

            if ip_pkt and udp_pkt:
                # Check for DSCP 46 (IP ToS field upper 6 bits: ToS >> 2 == DSCP)
                dscp_val = (ip_pkt.tos >> 2)
                if (dscp_val == DSCP_EF) and (udp_pkt.dst_port == SYNC_UDP_PORT or udp_pkt.src_port == SYNC_UDP_PORT):
                    is_sync = True

            # Assign QoS Queues if Priority Mode is enabled
            if self.priority_mode:
                if is_sync:
                    # Enqueue into High Priority Queue 1
                    actions.append(parser.OFPActionSetQueue(queue_id=QUEUE_SYNC_PRIORITY))
                    flow_prio = PRIORITY_SYNC_HIGH
                else:
                    # Choose between normal background queue and throttled queue
                    bg_queue = QUEUE_BG_THROTTLED if (self.congested_state or self.app_escalated_state) else QUEUE_BG_NORMAL
                    actions.append(parser.OFPActionSetQueue(queue_id=bg_queue))
                    flow_prio = PRIORITY_BG_DEFAULT
            else:
                flow_prio = PRIORITY_LEARNED_L2

            actions.append(parser.OFPActionOutput(out_port))

            # Install precise flow rule for this host-to-host path
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
            self._add_flow(
                datapath=datapath,
                priority=flow_prio,
                match=match,
                actions=actions,
                idle_timeout=FLOW_IDLE_TIMEOUT,
                hard_timeout=FLOW_HARD_TIMEOUT
            )
        else:
            # Flooding case: emit frame on all ports except ingress and blocked ports
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        # 5. Send packet-out to handle the initial packet
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)

    # --------------------------------------------------------------------------
    # PRIORITY CLASSIFICATION & QOS FLOW MANAGEMENT
    # --------------------------------------------------------------------------

    def _install_qos_flows(self, datapath):
        """
        Installs proactive high-priority flow rules for directional Time Sync traffic
        and background traffic egressing onto the inter-switch bottleneck links.

        - On Switch s1 (DPID_S1, Client side):
            * Sync requests (Client -> Server: IPv4, UDP dst=9999, DSCP=46)
              -> SetQueue(1), Output(active_inter_switch_port)
            * Background load (lg -> ls: IPv4, UDP dst=5201)
              -> SetQueue(bg_queue), Output(active_inter_switch_port)
        - On Switch s2 (DPID_S2, Server side):
            * Sync replies (Server -> Client: IPv4, UDP src=9999, DSCP=46)
              -> SetQueue(1), Output(active_inter_switch_port)
            * Reverse background load (ls -> lg: IPv4, UDP src=5201)
              -> SetQueue(bg_queue), Output(active_inter_switch_port)
        - Local host delivery (s2 -> srv, s1 -> c1..cN) is learned dynamically via PacketIn.
        """
        parser = datapath.ofproto_parser
        dpid = datapath.id

        if dpid not in (DPID_S1, DPID_S2):
            return

        out_port = self.active_is_port.get(dpid)
        if not out_port:
            return

        bg_queue = QUEUE_BG_THROTTLED if (self.congested_state or self.app_escalated_state) else QUEUE_BG_NORMAL
        self.logger.info("Installing QoS Priority Flows on DPID %s (inter-switch out_port=%s)", dpid, out_port)

        if dpid == DPID_S1:
            # 1. Forward direction on s1: Client -> Server (UDP dst=9999, DSCP=46)
            match_sync_fwd = parser.OFPMatch(
                eth_type=0x0800,
                ip_proto=17,
                ip_dscp=DSCP_EF,
                udp_dst=SYNC_UDP_PORT
            )
            actions_sync_fwd = [
                parser.OFPActionSetQueue(queue_id=QUEUE_SYNC_PRIORITY),
                parser.OFPActionOutput(out_port)
            ]
            self._add_flow(datapath, PRIORITY_SYNC_HIGH, match_sync_fwd, actions_sync_fwd)

            # 2. Forward background load on s1: lg -> ls (UDP dst=5201)
            match_bg_dst = parser.OFPMatch(
                eth_type=0x0800,
                ip_proto=17,
                udp_dst=BG_UDP_PORT
            )
            actions_bg_dst = [
                parser.OFPActionSetQueue(queue_id=bg_queue),
                parser.OFPActionOutput(out_port)
            ]
            self._add_flow(datapath, PRIORITY_BG_DEFAULT, match_bg_dst, actions_bg_dst)

        elif dpid == DPID_S2:
            # 1. Reverse direction on s2: Server -> Client (UDP src=9999, DSCP=46)
            match_sync_rev = parser.OFPMatch(
                eth_type=0x0800,
                ip_proto=17,
                ip_dscp=DSCP_EF,
                udp_src=SYNC_UDP_PORT
            )
            actions_sync_rev = [
                parser.OFPActionSetQueue(queue_id=QUEUE_SYNC_PRIORITY),
                parser.OFPActionOutput(out_port)
            ]
            self._add_flow(datapath, PRIORITY_SYNC_HIGH, match_sync_rev, actions_sync_rev)

            # 2. Reverse background load on s2: ls -> lg (UDP src=5201)
            match_bg_src = parser.OFPMatch(
                eth_type=0x0800,
                ip_proto=17,
                udp_src=BG_UDP_PORT
            )
            actions_bg_src = [
                parser.OFPActionSetQueue(queue_id=bg_queue),
                parser.OFPActionOutput(out_port)
            ]
            self._add_flow(datapath, PRIORITY_BG_DEFAULT, match_bg_src, actions_bg_src)

        self._log_decision_event(
            "QOS_FLOWS_INSTALLED",
            f"Installed directional sync (Queue 1) and bg (Queue {bg_queue}) flows on DPID {dpid} -> port {out_port}"
        )

    def _reinstall_all_flows(self):
        """
        Cleans and reinstalls all forwarding and QoS rules across all switches.
        Triggered when switching priority/dynamic modes or upon link failover.
        """
        self.logger.info("Reinstalling flows across all datapaths...")
        for dpid, datapath in self.datapaths.items():
            self._clear_all_flows(datapath)
            # Reinstall table-miss rule
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
            self._add_flow(datapath, PRIORITY_TABLE_MISS, match, actions)

            # Reinstall blocked standby port drop rules
            self._install_blocked_port_drops(datapath)

            # Reinstall QoS flows if priority mode is enabled
            if self.priority_mode:
                self._install_qos_flows(datapath)

        # Reset MAC learning table cache to force fresh route establishment
        self.mac_to_port.clear()

    # --------------------------------------------------------------------------
    # PERIODIC STATISTICS POLLING & BOTTLENECK MONITORING
    # --------------------------------------------------------------------------

    def _stats_monitor_loop(self):
        """
        Green thread running indefinitely, issuing OFPPortStatsRequest
        and OFPFlowStatsRequest every STATS_MONITOR_INTERVAL seconds.
        """
        while True:
            for datapath in list(self.datapaths.values()):
                self._request_stats(datapath)
            hub.sleep(STATS_MONITOR_INTERVAL)

    def _request_stats(self, datapath):
        """Sends PortStatsRequest and FlowStatsRequest to the switch."""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # Port Statistics Request
        req_port = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req_port)

        # Flow Statistics Request
        req_flow = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req_flow)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        """
        Processes OpenFlow 1.3 PortStats replies.
        Calculates per-port throughput and link utilization percentage on the
        bottleneck link, logging to CSV and feeding dynamic congestion logic.
        """
        body = ev.msg.body
        datapath = ev.msg.datapath
        dpid = datapath.id
        current_time = time.time()

        self.prev_port_stats.setdefault(dpid, {})

        for stat in body:
            port_no = stat.port_no
            # Ignore local virtual port
            if port_no == ofproto_v1_3.OFPP_LOCAL:
                continue

            rx_bytes = stat.rx_bytes
            tx_bytes = stat.tx_bytes

            prev = self.prev_port_stats[dpid].get(port_no)
            self.prev_port_stats[dpid][port_no] = {
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "timestamp": current_time
            }

            if prev is not None:
                time_delta = current_time - prev["timestamp"]
                if time_delta > 0:
                    delta_tx_bytes = max(0, tx_bytes - prev["tx_bytes"])
                    delta_rx_bytes = max(0, rx_bytes - prev["rx_bytes"])
                    # Total bidirectional throughput or directional tx
                    # Here tx throughput represents traffic egressing onto the link
                    throughput_bps = (delta_tx_bytes * 8.0) / time_delta
                    throughput_mbps = throughput_bps / (1000.0 * 1000.0)

                    # Calculate utilization relative to 10 Mbps bottleneck bandwidth
                    utilization_pct = min(100.0, (throughput_bps / BOTTLENECK_BW_BPS) * 100.0)

                    # Log to CSV
                    self._log_port_stats(dpid, port_no, rx_bytes, tx_bytes, throughput_mbps, utilization_pct)

                    # Dynamic Congestion Detection on Bottleneck Switch Port (s1 primary/backup)
                    active_port = self.active_is_port.get(dpid)
                    if dpid == DPID_S1 and port_no == active_port:
                        self._evaluate_congestion(utilization_pct)

    # --------------------------------------------------------------------------
    # DYNAMIC CONGESTION HANDLING WITH HYSTERESIS
    # --------------------------------------------------------------------------

    def _evaluate_congestion(self, bottleneck_util_pct):
        """
        Applies hysteresis logic over consecutive utilization samples:
          - If utilization > 80% for 3 consecutive samples: Enter Congested State
          - If utilization < 50% for 3 consecutive samples: Recover to Normal State
        Adjusts queuing for background traffic between Queue 2 and Queue 3.

        Note on OpenFlow Meters vs Queues:
        ---------------------------------
        OpenFlow 1.3 meters (OFPMeterMod) provide rate-limiting, but require
        kernel datapath support (OVS 2.13+ compiled with meter support or userspace OVS).
        Linux HTB / tc QoS queues (OFPActionSetQueue) are natively supported on
        standard Linux TC/OVS datapaths and provide predictable strict-priority
        and bandwidth-guarantee queuing across all kernel environments.
        This controller uses Queue-based QoS as the primary robust mechanism.
        """
        if not self.dynamic_mode:
            return

        if bottleneck_util_pct >= CONGESTION_HIGH_UTIL_PCT:
            self.high_util_streak += 1
            self.low_util_streak = 0
        elif bottleneck_util_pct <= CONGESTION_LOW_UTIL_PCT:
            self.low_util_streak += 1
            self.high_util_streak = 0
        else:
            # In hysteresis band (50% - 80%): maintain streaks without reset
            pass

        # Trigger Congestion Escalation
        if not self.congested_state and self.high_util_streak >= CONGESTION_HYSTERESIS_SAMPLES:
            self.congested_state = True
            self.high_util_streak = 0
            self._log_decision_event(
                "CONGESTION_DETECTED",
                f"Bottleneck utilization exceeded {CONGESTION_HIGH_UTIL_PCT}% for {CONGESTION_HYSTERESIS_SAMPLES} samples. "
                f"Throttling background traffic to Queue {QUEUE_BG_THROTTLED}."
            )
            self._reinstall_all_flows()

        # Trigger Congestion Recovery
        elif self.congested_state and self.low_util_streak >= CONGESTION_HYSTERESIS_SAMPLES:
            self.congested_state = False
            self.low_util_streak = 0
            self._log_decision_event(
                "CONGESTION_RECOVERED",
                f"Bottleneck utilization dropped below {CONGESTION_LOW_UTIL_PCT}% for {CONGESTION_HYSTERESIS_SAMPLES} samples. "
                f"Restoring background traffic to Queue {QUEUE_BG_NORMAL}."
            )
            self._reinstall_all_flows()

    # --------------------------------------------------------------------------
    # APPLICATION-AWARE SYNC QUALITY TELEMETRY
    # --------------------------------------------------------------------------

    def process_client_sync_report(self, report):
        """
        Processes client sync metrics: {client_id, rtt_ms, offset_ms, error_ms, timestamp}.
        If synchronization error exceeds 20ms for 5+ reports, escalates QoS throttling.
        If error normalizes below 5ms for 5+ reports, relaxes throttling.
        """
        client_id = report["client_id"]
        error_ms = float(report["error_ms"])

        if client_id not in self.client_reports:
            self.client_reports[client_id] = {
                "reports": [],
                "high_error_streak": 0,
                "low_error_streak": 0
            }

        client_entry = self.client_reports[client_id]
        client_entry["reports"].append(report)
        # Keep only the last 50 reports in memory
        if len(client_entry["reports"]) > 50:
            client_entry["reports"].pop(0)

        # Update streak counters
        if error_ms > SYNC_ERROR_ESCALATE_THRESH_MS:
            client_entry["high_error_streak"] += 1
            client_entry["low_error_streak"] = 0
        elif error_ms < SYNC_ERROR_RELAX_THRESH_MS:
            client_entry["low_error_streak"] += 1
            client_entry["high_error_streak"] = 0
        else:
            client_entry["high_error_streak"] = 0
            client_entry["low_error_streak"] = 0

        # Evaluate Escalation Trigger
        if not self.app_escalated_state and client_entry["high_error_streak"] >= SYNC_ERROR_ESCALATE_SAMPLES:
            self.app_escalated_state = True
            self._log_decision_event(
                "APP_AWARE_ESCALATION",
                f"Client {client_id} sync error > {SYNC_ERROR_ESCALATE_THRESH_MS}ms for "
                f"{SYNC_ERROR_ESCALATE_SAMPLES} consecutive reports. Enforcing strict queue isolation."
            )
            self._reinstall_all_flows()

        # Evaluate Relaxation Trigger
        elif self.app_escalated_state and client_entry["low_error_streak"] >= SYNC_ERROR_RELAX_SAMPLES:
            # Check if all clients have normalized
            all_healthy = all(
                c["low_error_streak"] >= SYNC_ERROR_RELAX_SAMPLES
                for c in self.client_reports.values()
            )
            if all_healthy:
                self.app_escalated_state = False
                self._log_decision_event(
                    "APP_AWARE_RELAXATION",
                    f"Sync error normalized < {SYNC_ERROR_RELAX_THRESH_MS}ms. Relaxing escalation."
                )
                self._reinstall_all_flows()

        return {
            "status": "success",
            "client_id": client_id,
            "escalated": self.app_escalated_state,
            "high_streak": client_entry["high_error_streak"],
            "low_streak": client_entry["low_error_streak"]
        }

    # --------------------------------------------------------------------------
    # LINK FAILURE DETECTION AND FAILOVER / FAILBACK
    # --------------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """
        Handles OpenFlow 1.3 OFPPortStatus events when a physical link or port
        transitions UP or DOWN.

        Failover Behavior:
          - If Primary link port goes DOWN:
            1. Unblock Backup link ports (s1-eth3, s2-eth3).
            2. Mark Primary link ports as blocked.
            3. Switch active egress port to Backup link.
            4. Reinstall flows and log recovery duration.
          - If Primary link port returns UP:
            1. Switch active egress port back to Primary link.
            2. Re-block Backup link ports.
            3. Reinstall flows and log failback completion.
        """
        msg = ev.msg
        reason = msg.reason
        port = msg.desc
        dpid = msg.datapath.id
        port_no = port.port_no
        ofproto = msg.datapath.ofproto

        reason_str = "UNKNOWN"
        if reason == ofproto.OFPPR_ADD:
            reason_str = "ADD"
        elif reason == ofproto.OFPPR_DELETE:
            reason_str = "DELETE"
        elif reason == ofproto.OFPPR_MODIFY:
            reason_str = "MODIFY"

        # Check port link status from config and state bitmasks
        port_down = (port.state & ofproto.OFPPS_LINK_DOWN) or (port.config & ofproto.OFPPC_PORT_DOWN)
        t_start = time.time()

        self.logger.warning(
            "Port Status Event: DPID=%s, Port=%s, Reason=%s, LinkDown=%s",
            dpid, port_no, reason_str, bool(port_down)
        )

        # Primary Link Down Detection
        if (dpid == DPID_S1 and port_no == PORT_S1_PRIMARY) or (dpid == DPID_S2 and port_no == PORT_S2_PRIMARY):
            if port_down and self.primary_link_up:
                self.primary_link_up = False
                # Switch active ports to backup
                self.active_is_port[DPID_S1] = PORT_S1_BACKUP
                self.active_is_port[DPID_S2] = PORT_S2_BACKUP

                # Block primary, unblock backup
                self.blocked_ports[DPID_S1] = {PORT_S1_PRIMARY}
                self.blocked_ports[DPID_S2] = {PORT_S2_PRIMARY}

                self._reinstall_all_flows()
                duration_ms = (time.time() - t_start) * 1000.0

                self._log_decision_event(
                    "LINK_FAILOVER",
                    f"Primary link s1-eth{PORT_S1_PRIMARY} DOWN. Failover to Backup link (s1:{PORT_S1_BACKUP}<->s2:{PORT_S2_BACKUP}) "
                    f"completed in {duration_ms:.2f} ms"
                )

            elif not port_down and not self.primary_link_up:
                # Primary Link Recovery (Failback)
                self.primary_link_up = True
                self.active_is_port[DPID_S1] = PORT_S1_PRIMARY
                self.active_is_port[DPID_S2] = PORT_S2_PRIMARY

                # Block backup, unblock primary
                self.blocked_ports[DPID_S1] = {PORT_S1_BACKUP}
                self.blocked_ports[DPID_S2] = {PORT_S2_BACKUP}

                self._reinstall_all_flows()
                duration_ms = (time.time() - t_start) * 1000.0

                self._log_decision_event(
                    "LINK_FAILBACK",
                    f"Primary link restored UP. Failback from backup to primary "
                    f"completed in {duration_ms:.2f} ms"
                )

    # --------------------------------------------------------------------------
    # RUNTIME CONTROL & STATUS ACCESSORS (FOR REST API)
    # --------------------------------------------------------------------------

    def get_system_status(self):
        """Returns snapshot of current controller operational state and stats."""
        recent_clients = {}
        for cid, data in self.client_reports.items():
            recent_clients[cid] = {
                "latest_report": data["reports"][-1] if data["reports"] else None,
                "high_streak": data["high_error_streak"],
                "low_streak": data["low_error_streak"]
            }

        return {
            "priority_mode": self.priority_mode,
            "dynamic_mode": self.dynamic_mode,
            "congested_state": self.congested_state,
            "app_escalated_state": self.app_escalated_state,
            "primary_link_up": self.primary_link_up,
            "active_is_port": self.active_is_port,
            "connected_switches": list(self.datapaths.keys()),
            "client_sync_reports": recent_clients,
            "timestamp": datetime.now().isoformat()
        }

    def update_operational_mode(self, mode_dict):
        """Updates priority and dynamic modes dynamically and refreshes flows."""
        changes = []
        if "priority" in mode_dict:
            new_prio = mode_dict["priority"].lower() in ("on", "true", "1")
            if new_prio != self.priority_mode:
                self.priority_mode = new_prio
                changes.append(f"priority_mode={'on' if new_prio else 'off'}")

        if "dynamic" in mode_dict:
            new_dyn = mode_dict["dynamic"].lower() in ("on", "true", "1")
            if new_dyn != self.dynamic_mode:
                self.dynamic_mode = new_dyn
                changes.append(f"dynamic_mode={'on' if new_dyn else 'off'}")

        if changes:
            desc = ", ".join(changes)
            self._log_decision_event("MODE_CHANGE", f"Mode updated: {desc}")
            self._reinstall_all_flows()

        return {
            "status": "success",
            "priority": "on" if self.priority_mode else "off",
            "dynamic": "on" if self.dynamic_mode else "off",
            "applied_changes": changes
        }
