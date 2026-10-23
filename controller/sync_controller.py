#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# sync_controller.py
# Ryu SDN controller for time synchronization project
# Computer Networks Mini Project

import os
import sys
import time
import json
import csv
from datetime import datetime

from ryu.lib import hub
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

# switch DPIDs
DPID_S1 = 1  # switch 1 (client side)
DPID_S2 = 2  # switch 2 (server side)

# inter-switch ports
PORT_S1_PRIMARY = 1
PORT_S1_BACKUP = 2
PORT_S2_PRIMARY = 1
PORT_S2_BACKUP = 2

# IPs and ports
TIME_SERVER_IP = "10.0.0.100"
SYNC_UDP_PORT = 9999
BG_UDP_PORT = 5201
DSCP_EF = 46

# flow priorities
PRIORITY_SYNC_HIGH = 100
PRIORITY_BG_DEFAULT = 50
PRIORITY_LEARNED_L2 = 20
PRIORITY_TABLE_MISS = 0

# QoS queues
QUEUE_SYNC_PRIORITY = 1
QUEUE_BG_NORMAL = 2
QUEUE_BG_THROTTLED = 3

# bottleneck link bandwidth (10 Mbps)
BOTTLENECK_BW_BPS = 10.0 * 1000 * 1000

# dynamic congestion thresholds
CONGESTION_HIGH_UTIL_PCT = 80.0
CONGESTION_LOW_UTIL_PCT = 50.0
CONGESTION_HYSTERESIS_SAMPLES = 3

# sync error thresholds for escalation
SYNC_ERROR_ESCALATE_THRESH_MS = 20.0
SYNC_ERROR_ESCALATE_SAMPLES = 5
SYNC_ERROR_RELAX_THRESH_MS = 5.0
SYNC_ERROR_RELAX_SAMPLES = 5

# timeouts
FLOW_IDLE_TIMEOUT = 60
FLOW_HARD_TIMEOUT = 0

# monitor interval in seconds
STATS_MONITOR_INTERVAL = 1.0

# log paths
RESULTS_DIR = "results"
PORT_STATS_CSV = os.path.join(RESULTS_DIR, "port_stats.csv")
CONTROLLER_LOG_CSV = os.path.join(RESULTS_DIR, "controller_log.csv")

SYNC_CONTROLLER_INSTANCE_NAME = "sync_controller_api_app"

# CLI options for Ryu
CONF = cfg.CONF
CONF.register_cli_opts([
    cfg.BoolOpt("priority", default=False, help="Enable strict QoS priority for sync traffic at startup"),
    cfg.BoolOpt("dynamic", default=False, help="Enable dynamic congestion mitigation at startup"),
])


class SyncRestController(ControllerBase):
    # REST API endpoints for monitoring and mode control

    def __init__(self, req, link, data, **config):
        super(SyncRestController, self).__init__(req, link, data, **config)
        self.sync_app = data[SYNC_CONTROLLER_INSTANCE_NAME]

    @route("sync", "/sdn/status", methods=["GET"])
    def get_sdn_status(self, req, **kwargs):
        # returns current controller status and stats
        status_data = self.sync_app.get_system_status()
        body = json.dumps(status_data, indent=2)
        return Response(content_type="application/json; charset=utf-8", body=body)

    @route("sync", "/sdn/mode", methods=["POST"])
    def set_sdn_mode(self, req, **kwargs):
        # update priority and dynamic modes at runtime
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
        # receives sync telemetry report from client
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


class SyncController(app_manager.RyuApp):
    # OpenFlow 1.3 controller for time sync and dynamic QoS

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SyncController, self).__init__(*args, **kwargs)

        self.priority_mode = CONF.priority
        self.dynamic_mode = CONF.dynamic
        self.congested_state = False
        self.app_escalated_state = False

        self.logger.info(
            "Initializing SyncController: Priority Mode = %s, Dynamic Congestion = %s",
            self.priority_mode, self.dynamic_mode
        )

        # connected switches
        self.datapaths = {}

        # mac learning table: {dpid: {mac: port}}
        self.mac_to_port = {}

        # active/blocked ports to prevent loops
        self.primary_link_up = True
        self.active_is_port = {
            DPID_S1: PORT_S1_PRIMARY,
            DPID_S2: PORT_S2_PRIMARY,
        }
        self.blocked_ports = {
            DPID_S1: {PORT_S1_BACKUP},
            DPID_S2: {PORT_S2_BACKUP},
        }

        # port stats tracking
        self.prev_port_stats = {}

        # hysteresis counters
        self.high_util_streak = 0
        self.low_util_streak = 0

        # client sync reports
        self.client_reports = {}

        self._init_csv_logs()

        # setup REST API
        wsgi = kwargs.get("wsgi")
        if wsgi:
            wsgi.register(SyncRestController, {SYNC_CONTROLLER_INSTANCE_NAME: self})
            self.logger.info("Registered REST API handler at http://127.0.0.1:8080")

        # start background stats monitoring thread
        self.monitor_thread = hub.spawn(self._stats_monitor_loop)

        self._log_decision_event(
            "CONTROLLER_INIT",
            f"Controller started (priority_mode={self.priority_mode}, dynamic_mode={self.dynamic_mode})"
        )

    def _init_csv_logs(self):
        # create results dir and csv files with headers if they don't exist
        if not os.path.exists(RESULTS_DIR):
            try:
                os.makedirs(RESULTS_DIR, exist_ok=True)
            except Exception as e:
                self.logger.error("Failed to create results directory %s: %s", RESULTS_DIR, e)

        if not os.path.exists(PORT_STATS_CSV):
            with open(PORT_STATS_CSV, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "dpid", "port", "rx_bytes", "tx_bytes",
                    "throughput_mbps", "utilization_pct"
                ])

        if not os.path.exists(CONTROLLER_LOG_CSV):
            with open(CONTROLLER_LOG_CSV, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "event_type", "details"])

    def _log_decision_event(self, event_type, details):
        # log controller event to csv
        ts_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with open(CONTROLLER_LOG_CSV, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([ts_iso, event_type, details])
            self.logger.info("[EVENT %s] %s", event_type, details)
        except Exception as e:
            self.logger.error("Error writing to %s: %s", CONTROLLER_LOG_CSV, e)

    def _log_port_stats(self, dpid, port_no, rx_bytes, tx_bytes, throughput_mbps, util_pct):
        # log port stats to csv
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

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        # track switch connections and disconnections
        datapath = ev.datapath
        dpid = datapath.id
        if ev.state == MAIN_DISPATCHER:
            if dpid not in self.datapaths:
                self.logger.info("Switch registered: DPID=%s", dpid)
                self.datapaths[dpid] = datapath
                self.mac_to_port.setdefault(dpid, {})
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
        # initial switch setup - install table-miss rule
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})
        self.logger.info("Configuring Switch Features for DPID=%s", dpid)

        # clear old flows first
        self._clear_all_flows(datapath)

        # table-miss flow entry sends unknown packets to controller
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

        # drop packets on blocked backup ports to prevent loops
        self._install_blocked_port_drops(datapath)

        if self.priority_mode:
            self._install_qos_flows(datapath)

    def _install_blocked_port_drops(self, datapath):
        # install drop rules on blocked backup ports
        parser = datapath.ofproto_parser
        dpid = datapath.id
        for bport in self.blocked_ports.get(dpid, set()):
            match = parser.OFPMatch(in_port=bport)
            self._add_flow(datapath, priority=500, match=match, actions=[])

    def _clear_all_flows(self, datapath):
        # remove all flows from switch
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
        # helper to install flow rule on switch
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

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        # handles incoming packets, mac learning, and flow installation
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]
        dpid = datapath.id

        # drop packets from blocked backup ports
        if in_port in self.blocked_ports.get(dpid, set()):
            return

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        # ignore LLDP packets
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst_mac = eth.dst
        src_mac = eth.src

        self.mac_to_port.setdefault(dpid, {})

        # learn source MAC
        self.mac_to_port[dpid][src_mac] = in_port

        # find out which port to send to
        if dst_mac in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst_mac]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = []

        if out_port != ofproto.OFPP_FLOOD:
            # check if time sync packet (UDP 9999 + DSCP 46)
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            udp_pkt = pkt.get_protocol(udp.udp)
            is_sync = False

            if ip_pkt and udp_pkt:
                dscp_val = (ip_pkt.tos >> 2)
                if (dscp_val == DSCP_EF) and (udp_pkt.dst_port == SYNC_UDP_PORT or udp_pkt.src_port == SYNC_UDP_PORT):
                    is_sync = True

            # assign queue based on priority mode
            if self.priority_mode:
                if is_sync:
                    actions.append(parser.OFPActionSetQueue(queue_id=QUEUE_SYNC_PRIORITY))
                    flow_prio = PRIORITY_SYNC_HIGH
                else:
                    bg_queue = QUEUE_BG_THROTTLED if (self.congested_state or self.app_escalated_state) else QUEUE_BG_NORMAL
                    actions.append(parser.OFPActionSetQueue(queue_id=bg_queue))
                    flow_prio = PRIORITY_BG_DEFAULT
            else:
                flow_prio = PRIORITY_LEARNED_L2

            actions.append(parser.OFPActionOutput(out_port))

            # install flow rule
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
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        # send packet out
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

    def _install_qos_flows(self, datapath):
        # install QoS rules on switches for sync and background traffic
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
            # client -> server sync traffic (UDP dst 9999, DSCP 46)
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

            # background traffic (UDP dst 5201)
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
            # server -> client sync traffic (UDP src 9999, DSCP 46)
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

            # reverse background traffic (UDP src 5201)
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
        # clears and reinstalls all flows on all switches
        self.logger.info("Reinstalling flows across all datapaths...")
        for dpid, datapath in self.datapaths.items():
            self._clear_all_flows(datapath)
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
            self._add_flow(datapath, PRIORITY_TABLE_MISS, match, actions)

            self._install_blocked_port_drops(datapath)

            if self.priority_mode:
                self._install_qos_flows(datapath)

        self.mac_to_port.clear()

    def _stats_monitor_loop(self):
        # poll port and flow stats periodically
        while True:
            for datapath in list(self.datapaths.values()):
                self._request_stats(datapath)
            hub.sleep(STATS_MONITOR_INTERVAL)

    def _request_stats(self, datapath):
        # send port and flow stats requests
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        req_port = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req_port)

        req_flow = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req_flow)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        # handle port stats reply and calculate throughput/utilization
        body = ev.msg.body
        datapath = ev.msg.datapath
        dpid = datapath.id
        current_time = time.time()

        self.prev_port_stats.setdefault(dpid, {})

        for stat in body:
            port_no = stat.port_no
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
                    throughput_bps = (delta_tx_bytes * 8.0) / time_delta
                    throughput_mbps = throughput_bps / (1000.0 * 1000.0)

                    # utilization based on 10 Mbps bottleneck
                    utilization_pct = min(100.0, (throughput_bps / BOTTLENECK_BW_BPS) * 100.0)

                    self._log_port_stats(dpid, port_no, rx_bytes, tx_bytes, throughput_mbps, utilization_pct)

                    # check congestion on the active inter-switch port
                    active_port = self.active_is_port.get(dpid)
                    if dpid == DPID_S1 and port_no == active_port:
                        self._evaluate_congestion(utilization_pct)

    def _evaluate_congestion(self, bottleneck_util_pct):
        # check if link is congested using hysteresis thresholds
        if not self.dynamic_mode:
            return

        if bottleneck_util_pct >= CONGESTION_HIGH_UTIL_PCT:
            self.high_util_streak += 1
            self.low_util_streak = 0
        elif bottleneck_util_pct <= CONGESTION_LOW_UTIL_PCT:
            self.low_util_streak += 1
            self.high_util_streak = 0
        else:
            pass

        # switch to throttled queue if utilization stays high
        if not self.congested_state and self.high_util_streak >= CONGESTION_HYSTERESIS_SAMPLES:
            self.congested_state = True
            self.high_util_streak = 0
            self._log_decision_event(
                "CONGESTION_DETECTED",
                f"Bottleneck utilization exceeded {CONGESTION_HIGH_UTIL_PCT}% for {CONGESTION_HYSTERESIS_SAMPLES} samples. "
                f"Throttling background traffic to Queue {QUEUE_BG_THROTTLED}."
            )
            self._reinstall_all_flows()

        # recover back to normal queue when utilization drops
        elif self.congested_state and self.low_util_streak >= CONGESTION_HYSTERESIS_SAMPLES:
            self.congested_state = False
            self.low_util_streak = 0
            self._log_decision_event(
                "CONGESTION_RECOVERED",
                f"Bottleneck utilization dropped below {CONGESTION_LOW_UTIL_PCT}% for {CONGESTION_HYSTERESIS_SAMPLES} samples. "
                f"Restoring background traffic to Queue {QUEUE_BG_NORMAL}."
            )
            self._reinstall_all_flows()

    def process_client_sync_report(self, report):
        # track client sync error and escalate QoS if error stays high
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
        # keep only last 50 reports
        if len(client_entry["reports"]) > 50:
            client_entry["reports"].pop(0)

        if error_ms > SYNC_ERROR_ESCALATE_THRESH_MS:
            client_entry["high_error_streak"] += 1
            client_entry["low_error_streak"] = 0
        elif error_ms < SYNC_ERROR_RELAX_THRESH_MS:
            client_entry["low_error_streak"] += 1
            client_entry["high_error_streak"] = 0
        else:
            client_entry["high_error_streak"] = 0
            client_entry["low_error_streak"] = 0

        # escalate if error is high for consecutive reports
        if not self.app_escalated_state and client_entry["high_error_streak"] >= SYNC_ERROR_ESCALATE_SAMPLES:
            self.app_escalated_state = True
            self._log_decision_event(
                "APP_AWARE_ESCALATION",
                f"Client {client_id} sync error > {SYNC_ERROR_ESCALATE_THRESH_MS}ms for "
                f"{SYNC_ERROR_ESCALATE_SAMPLES} consecutive reports. Enforcing strict queue isolation."
            )
            self._reinstall_all_flows()

        # relax if all clients are back to normal
        elif self.app_escalated_state and client_entry["low_error_streak"] >= SYNC_ERROR_RELAX_SAMPLES:
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

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        # handles link up/down events for failover and failback
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

        # check if port is down
        port_down = (port.state & ofproto.OFPPS_LINK_DOWN) or (port.config & ofproto.OFPPC_PORT_DOWN)
        t_start = time.time()

        self.logger.warning(
            "Port Status Event: DPID=%s, Port=%s, Reason=%s, LinkDown=%s",
            dpid, port_no, reason_str, bool(port_down)
        )

        # failover: primary link went down
        if (dpid == DPID_S1 and port_no == PORT_S1_PRIMARY) or (dpid == DPID_S2 and port_no == PORT_S2_PRIMARY):
            if port_down and self.primary_link_up:
                self.primary_link_up = False
                # switch to backup link
                self.active_is_port[DPID_S1] = PORT_S1_BACKUP
                self.active_is_port[DPID_S2] = PORT_S2_BACKUP

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
                # failback: primary link came back up
                self.primary_link_up = True
                self.active_is_port[DPID_S1] = PORT_S1_PRIMARY
                self.active_is_port[DPID_S2] = PORT_S2_PRIMARY

                self.blocked_ports[DPID_S1] = {PORT_S1_BACKUP}
                self.blocked_ports[DPID_S2] = {PORT_S2_BACKUP}

                self._reinstall_all_flows()
                duration_ms = (time.time() - t_start) * 1000.0

                self._log_decision_event(
                    "LINK_FAILBACK",
                    f"Primary link restored UP. Failback from backup to primary "
                    f"completed in {duration_ms:.2f} ms"
                )

    def get_system_status(self):
        # returns controller status dictionary for REST API
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
        # updates priority and dynamic modes from REST API
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
