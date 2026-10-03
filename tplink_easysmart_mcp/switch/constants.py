"""Protocol constants for the TP-Link Easy Smart switch.

Paths, page anchors, the read/write code maps and the inline-variable scanner
limits. Values tagged ``# S0: confirm`` are a fact table S0's live capture must
verify, not stubs: they already carry the best-known value.
"""

from __future__ import annotations

from typing import Final

# --- Paths -------------------------------------------------------------------
ROOT: Final = "/"
LOGON: Final = "/logon.cgi"
LOGOUT: Final = "/Logout.htm"
SYSTEM_INFO: Final = "/SystemInfoRpm.htm"
PORT_SETTING: Final = "/PortSettingRpm.htm"
PORT_STATISTICS: Final = "/PortStatisticsRpm.htm"
POE_CONFIG: Final = "/PoeConfigRpm.htm"
VLAN_8021Q: Final = "/Vlan8021QRpm.htm"
VLAN_PVID: Final = "/Vlan8021QPvidRpm.htm"
POE_PORT_CONFIG_CGI: Final = "/poe_port_config.cgi"
PORT_SETTING_CGI: Final = "/port_setting.cgi"

# --- Page anchors (the DATA marker variable for each page) -------------------
ANCHOR_SYSTEM_INFO: Final = "info_ds"
ANCHOR_PORTS: Final = "all_info"
ANCHOR_PORT_STATS: Final = "all_info"
ANCHOR_POE: Final = "portConfig"
ANCHOR_VLAN: Final = "qvlan_ds"
ANCHOR_PVID: Final = "pvid_ds"

# --- Login-page markers ------------------------------------------------------
# Structural signals (see pages.py): `var logonInfo = [...]` declared in the first
# script block, and/or a <form> whose action attribute equals LOGON. The raw form
# action is matched as a parsed attribute value, never as a body substring.
LOGON_INFO_VAR: Final = "logonInfo"
# `submitForm` is the plain page's own form name and is deliberately NOT here.
ENCRYPTED_MARKERS: Final = (
    "encryptType",
    "cryp_new",
    "plain_password",
    "g_tid",
    "securityEncode",
)

# --- PoE read encodings ------------------------------------------------------
POWERLIMIT_AUTO_RAW: Final = 330  # powerlimit value meaning "auto"
# powerlimit raw value -> class preset kind
PRESET_LIMIT_READ: Final = {40: "class1", 70: "class2", 154: "class3", 300: "class4"}
# pdclass raw value -> emitted class string ("--" -> None)
PDCLASS_READ: Final = {40: "1", 70: "2", 154: "3", 300: "4", 330: "0"}
PRIORITY_READ: Final = {0: "high", 1: "middle", 2: "low"}

# --- PoE write encodings -----------------------------------------------------
# name_ppowerlimit code for each limit kind.
POWERLIMIT_WRITE_CODE: Final = {
    "auto": 1,
    "class1": 2,
    "class2": 3,
    "class3": 4,
    "class4": 5,
    "manual": 6,
}
# name_ppowerlimit2 literal for each class preset code (verbatim from [REF]).
PRESET_LIMIT2: Final = {2: "(4w)", 3: "(7w)", 4: "(15.4w)", 5: "(30w)"}
# name_ppowerlimit2 when the limit is auto. S0 may change this to "None" or omit.
AUTO_LIMIT2: Final = ""  # S0: confirm
# name_pstate: 2 enable / 1 disable (differs from the read encoding of 1 = on).
PSTATE_ENABLE: Final = 2
PSTATE_DISABLE: Final = 1

# --- Array layout ------------------------------------------------------------
PAD: Final = 2  # per-port arrays are max_port_num + PAD long  # S0: confirm
POE_STATE_DISABLED: Final = 0  # read value of a disabled PoE port's state  # S0: confirm
POE_STATE_ENABLED: Final = 1

# --- Inline-variable scanner limits -----------------------------------------
MAX_INPUT_BYTES: Final = 256 * 1024
MAX_DEPTH: Final = 8
MAX_ARRAY_ITEMS: Final = 4096
MAX_IDENT_LEN: Final = 64

# Upper bound on any declared port/entry count read from a page (portNum,
# max_port_num, poe_port_num, *_num). The TL-SG1016PE has 16 ports; 128 is a
# generous ceiling that still fences off a hostile page from driving a parser
# loop (e.g. `_ports_from_mask`'s per-port bignum shift) into a CPU DoS. Every
# such count flows through `jsvars.declared_count`, which enforces this.
MAX_DECLARED_PORTS: Final = 128
